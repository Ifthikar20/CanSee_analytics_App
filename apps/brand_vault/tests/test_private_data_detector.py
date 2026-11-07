"""BS-PRIV-001: private / non-public data exposure in AI answers.

The detector's job is NOT accuracy — a correct home address or a live
API key is worse than a wrong one. These tests pin the two tiers
(credentials always flag; contact data flags for the judge to
adjudicate) and the brand-relevance guard that keeps generic answers
quiet.
"""
from types import SimpleNamespace

from apps.brand_vault.models import SafetyAlert
from apps.brand_vault.services.security import detectors as D

DET = D.DETECTOR_INDEX["BS-PRIV-001"]


def _ctx(text, brand="Acme", mentioned=True):
    return D.DetectionContext(
        result=SimpleNamespace(pk=1, is_mentioned=mentioned, mention_rank=None,
                               sentiment="", competitors_mentioned=[]),
        text=text, brand=brand, brand_terms=[brand], competitors=[],
    )


class TestPrivateDataDetector:
    def test_flags_api_key_as_high(self):
        text = ("Acme's integration guide shows the key "
                "sk-live_9fJ2kQxZ7bTn4WpL0aVrD8mE for authentication.")
        findings = D._detect_private_data(_ctx(text), DET)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "high"
        assert f.issue == SafetyAlert.ISSUE_PRIVATE_DATA
        # The exact secret is pinpointed as a span, not just described.
        assert any("API secret key" == s.label for s in f.spans)
        assert "credential or identifier" in f.detail

    def test_flags_government_id(self):
        text = "The Acme filing lists the owner SSN 123-45-6789 in the appendix."
        f = D._detect_private_data(_ctx(text), DET)[0]
        assert f.severity == "high"
        assert any("Government ID" in s.label for s in f.spans)

    def test_contact_data_is_medium_for_the_judge(self):
        text = "You can reach the Acme founder at jane.doe@gmail.com or 415-555-0142."
        f = D._detect_private_data(_ctx(text), DET)[0]
        # Contact data alone stays at the detector default; the judge
        # decides whether it is personal or published business contact.
        assert f.severity == DET.default_severity
        labels = {s.label for s in f.spans}
        assert "Email address" in labels
        assert "privacy exposure" in f.detail

    def test_detail_quotes_the_actual_data(self):
        text = "Acme support is jane.doe@gmail.com for escalations."
        f = D._detect_private_data(_ctx(text), DET)[0]
        assert "jane.doe@gmail.com" in f.detail

    def test_silent_when_brand_absent(self):
        # A generic answer that happens to contain contact-shaped text
        # must not raise an alert for this brand.
        text = "Contact support@example.com or call 415-555-0142 for help."
        assert D._detect_private_data(_ctx(text, brand="Acme"), DET) == []

    def test_silent_when_no_private_data(self):
        text = "Acme is a payments company used by developers worldwide."
        assert D._detect_private_data(_ctx(text), DET) == []

    def test_registered_in_pipeline(self):
        # The detector must actually run inside run_detectors, not just
        # exist in the registry.
        assert DET.category == "privacy"
        assert ("privacy", "Privacy") in D.CATEGORIES
        text = "Acme's key is sk-live_9fJ2kQxZ7bTn4WpL0aVrD8mE for the API."
        codes = {f.detector.code for f in D.run_detectors(_ctx(text))}
        assert "BS-PRIV-001" in codes
