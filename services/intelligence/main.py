"""
Intelligence service: LLM brand/sentiment extraction and SERP relevance.

Internal-only FastAPI app. Reached from web/celery at
http://intelligence.ftb.internal:8000 on the compose network; never
exposed through nginx or public DNS. Auth is a static bearer token
(INTELLIGENCE_AUTH_TOKEN) shared with the Django callers via .env.prod.

Stateless by design: quota, circuit breaking, and AITokenUsage cost
recording stay in the Django facades. Endpoints return the logic
envelope untouched so the facade can record usage.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.intelligence import logic

app = FastAPI(title="ftb-intelligence", docs_url=None, redoc_url=None)


def require_token(request: Request) -> None:
    expected = os.environ.get("INTELLIGENCE_AUTH_TOKEN", "")
    if not expected:
        # A missing token is a deployment error; refuse rather than fail open.
        raise HTTPException(status_code=503, detail="service token not configured")
    header = request.headers.get("Authorization", "")
    provided = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid token")


class AnalyzeContentBody(BaseModel):
    text: str
    query: str = ""
    target_brand: str = ""


class SerpItem(BaseModel):
    rank: int
    title: str = ""
    url: str = ""


class SerpRelevanceBody(BaseModel):
    query: str
    items: list[SerpItem] = Field(default_factory=list)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/analyze-content", dependencies=[Depends(require_token)])
def analyze_content(body: AnalyzeContentBody) -> dict:
    return logic.analyze_content(
        body.text,
        query=body.query,
        target_brand=body.target_brand,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model_name=logic.resolve_model(os.environ.get("LLM_EXTRACTION_MODEL", "")),
    )


@app.post("/v1/serp-relevance", dependencies=[Depends(require_token)])
def serp_relevance(body: SerpRelevanceBody) -> dict:
    return logic.assess_serp_relevance(
        body.query,
        [item.model_dump() for item in body.items],
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model_name=logic.resolve_model(os.environ.get("LLM_EXTRACTION_MODEL", "")),
    )
