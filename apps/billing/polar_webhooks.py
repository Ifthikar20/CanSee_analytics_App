"""
Polar webhook endpoint.

Signature verification, idempotency via BillingEvent, atomic processing,
and non-retryable errors answered with 200 so Polar stops redelivering.

Every event type we care about collapses to one action — re-sync the
affected user's subscription state from Polar's customer-state API
(polar_billing.sync_from_customer_state is the single writer). That
makes handlers trivially idempotent and immune to event ordering.

Local dev note: Polar cannot deliver webhooks to localhost; the
checkout-confirmation endpoint covers that path. This endpoint is for
deployed environments (configure it in the Polar dashboard under
Settings -> Webhooks and put the signing secret in POLAR_WEBHOOK_SECRET).
"""
from __future__ import annotations

import logging
import time

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.billing.models import BillingEvent
from apps.metering import polar_client

logger = logging.getLogger("billing")

# Everything subscription-affecting funnels through a full state re-sync.
_SYNC_EVENTS = {
    "customer.state_changed",
    "customer.created",
    "customer.updated",
    "subscription.created",
    "subscription.active",
    "subscription.updated",
    "subscription.canceled",
    "subscription.uncanceled",
    "subscription.revoked",
    "subscription.past_due",
    "subscription.cycled",
    "order.paid",
}


def _external_customer_id(payload: dict) -> str | None:
    data = payload.get("data") or {}
    # customer.state_changed carries the customer object itself;
    # subscription/order events nest a customer object.
    for candidate in (data, data.get("customer") or {}):
        ext = candidate.get("external_id")
        if ext:
            return str(ext)
    return None


@method_decorator(csrf_exempt, name="dispatch")
class PolarWebhookView(View):
    def post(self, request):
        started = time.monotonic()

        try:
            event = polar_client.validate_webhook(request.body, dict(request.headers))
        except polar_client.WebhookInvalid as exc:
            logger.warning("Polar webhook rejected: %s", exc)
            return JsonResponse({"error": "invalid_signature"}, status=400)

        payload = event if isinstance(event, dict) else event.model_dump(mode="json")
        event_type = str(payload.get("type", ""))
        # Standard Webhooks delivery id — the idempotency anchor.
        event_id = request.headers.get("webhook-id") or ""
        if not event_id:
            return JsonResponse({"error": "missing_webhook_id"}, status=400)

        # Idempotency: first delivery inserts, replays short-circuit.
        try:
            record = BillingEvent.objects.create(
                event_id=f"polar_{event_id}",
                event_type=f"polar:{event_type}"[:80],
                payload={"type": event_type},
            )
        except IntegrityError:
            return HttpResponse(status=200)

        error = ""
        try:
            if event_type in _SYNC_EVENTS:
                ext_id = _external_customer_id(payload)
                if ext_id:
                    user = get_user_model().objects.filter(id=ext_id).first()
                    if user is not None:
                        from apps.billing.services.polar_billing import (
                            sync_from_customer_state,
                        )

                        # No outer transaction on purpose: the sync makes
                        # an outbound Polar call first and opens its own
                        # atomic block around the local write. Wrapping it
                        # here pinned a DB connection open for the whole
                        # network round-trip.
                        sync_from_customer_state(user)
                    else:
                        logger.warning(
                            "Polar webhook for unknown external customer %s", ext_id
                        )
                else:
                    logger.info("Polar webhook %s carried no external id", event_type)
        except Exception as exc:
            # Sync failures are safe to retry: answer 500 so Polar
            # redelivers (the BillingEvent row blocks double-processing
            # only after success, so clear it for the retry).
            error = str(exc)[:500]
            logger.error("Polar webhook processing failed: %s", exc, exc_info=True)
            BillingEvent.objects.filter(id=record.id).delete()
            return JsonResponse({"error": "processing_failed"}, status=500)
        finally:
            if error == "":
                BillingEvent.objects.filter(id=record.id).update(
                    processed=True,
                    processing_time_ms=int((time.monotonic() - started) * 1000),
                )

        return HttpResponse(status=200)
