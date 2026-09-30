"""Shared vocabulary for security-perception analysis.

One place for the regexes that decide whether a sentence is about
certifications, incidents, encryption, access control, privacy or a
weakness, plus the fear/avoidance phrasings. The Haiku extractor's
heuristic fallback, the Brand Security detectors and the perception
aggregator all read from here so the three cannot drift apart.

Pure Python, no Django imports: safe to import from any app.
"""
from __future__ import annotations

import re

KIND_CERTIFICATION = "certification"
KIND_INCIDENT = "incident"
KIND_ENCRYPTION = "encryption"
KIND_ACCESS_CONTROL = "access_control"
KIND_PRIVACY = "privacy"
KIND_VULNERABILITY = "vulnerability"
KIND_OTHER = "other"

CLAIM_KINDS: tuple[str, ...] = (
    KIND_CERTIFICATION,
    KIND_INCIDENT,
    KIND_ENCRYPTION,
    KIND_ACCESS_CONTROL,
    KIND_PRIVACY,
    KIND_VULNERABILITY,
    KIND_OTHER,
)

POLARITY_AFFIRMS = "affirms"
POLARITY_DENIES = "denies"
POLARITY_UNCERTAIN = "uncertain"
POLARITIES: tuple[str, ...] = (POLARITY_AFFIRMS, POLARITY_DENIES, POLARITY_UNCERTAIN)

SUPPORT_SUPPORTED = "supported"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_UNKNOWN = "unknown"

CERTIFICATION_RE = re.compile(
    r"\b(?:SOC\s?[12](?:\s?type\s?(?:I{1,2}|[12]))?|ISO\s?/?\s?(?:IEC\s?)?2700[0-9]|"
    r"ISO\s?27017|ISO\s?27018|HIPAA|HITRUST|GDPR|CCPA|CPRA|PCI[\s-]?DSS|FedRAMP|"
    r"NIST(?:\s?800-\d+)?|CSA\s?STAR|Cyber\s?Essentials|C5|TISAX|"
    r"certif(?:ied|ication|icate)s?|compliant|compliance|attestation|audited)\b",
    re.IGNORECASE,
)

INCIDENT_RE = re.compile(
    r"\b(?:data\s?breach(?:es)?|breach(?:ed|es)?|hacked|hack(?:s|ers?)?|"
    r"security\s?incident(?:s)?|leak(?:ed|s)?|ransomware|compromised|"
    r"exposed\s+(?:records|data|credentials|customer)|CVE-\d{4}-\d+|"
    r"vulnerabilit(?:y|ies)|exploit(?:ed|s)?|zero[\s-]?day|malware|outage(?:s)?)\b",
    re.IGNORECASE,
)

ENCRYPTION_RE = re.compile(
    r"\b(?:encrypt(?:s|ed|ion|ing)?|AES[\s-]?(?:128|256)|TLS(?:\s?1\.[23])?|"
    r"at\s+rest|in\s+transit|end[\s-]to[\s-]end|key\s+management|HSM)\b",
    re.IGNORECASE,
)

ACCESS_CONTROL_RE = re.compile(
    r"\b(?:single\s+sign[\s-]?on|SSO|SAML|OAuth|two[\s-]factor|2FA|MFA|"
    r"multi[\s-]factor|role[\s-]based|RBAC|access\s+control(?:s)?|audit\s+log(?:s|ging)?|"
    r"least\s+privilege|SCIM)\b",
    re.IGNORECASE,
)

PRIVACY_RE = re.compile(
    r"\b(?:personal\s+data|personal\s+information|PII|privacy|data\s+retention|"
    r"retain(?:s|ed)?\s+(?:your|user|customer)?\s?data|delete\s+(?:your|my|their|user)\s+data|"
    r"sells?\s+(?:your|user|customer|personal)\s+data|third[\s-]part(?:y|ies)|"
    r"train(?:s|ing)?\s+(?:its\s+|their\s+)?(?:AI|models?)|data\s+residency|"
    r"subprocessors?|DPA)\b",
    re.IGNORECASE,
)

WEAKNESS_RE = re.compile(
    r"\b(?:weakness(?:es)?|weak\s+(?:point|spot)s?|patch(?:es|ed|ing)?|bug\s+bounty|"
    r"responsible\s+disclosure|penetration\s+test(?:s|ing)?|pen\s?test(?:s|ing)?|"
    r"security\s+(?:gap|flaw|hole)s?|misconfigur(?:ed|ation)s?)\b",
    re.IGNORECASE,
)

# Any of the above: the sentence is about security at all.
SECURITY_TOPIC_RE = re.compile(
    "|".join(
        p.pattern for p in (
            CERTIFICATION_RE, INCIDENT_RE, ENCRYPTION_RE,
            ACCESS_CONTROL_RE, PRIVACY_RE, WEAKNESS_RE,
        )
    ),
    re.IGNORECASE,
)

# Fear, uncertainty and doubt framing attached to the brand.
FUD_RE = re.compile(
    r"\b(?:risky|be\s+careful|cannot\s+be\s+trusted|can't\s+be\s+trusted|"
    r"not\s+(?:safe|secure|trustworthy)|unsafe|insecure|red\s+flags?|"
    r"proceed\s+with\s+caution|exercise\s+caution|at\s+your\s+own\s+risk|"
    r"i\s+would\s+be\s+wary|be\s+wary|concerning|questionable\s+security|"
    r"poor\s+security|lax\s+security|security\s+concerns?)\b",
    re.IGNORECASE,
)

# Advice against using the brand.
AVOID_RE = re.compile(
    r"\b(?:avoid|steer\s+clear|stay\s+away|not\s+recommend(?:ed)?|"
    r"would\s+not\s+recommend|wouldn't\s+recommend|do\s+not\s+use|don't\s+use|"
    r"look\s+elsewhere|choose\s+(?:a\s+)?(?:different|another)|should\s+not\s+(?:use|trust))\b",
    re.IGNORECASE,
)

NEGATION_RE = re.compile(
    r"\b(?:not|no|never|neither|nor|hasn't|has\s+not|haven't|have\s+not|isn't|is\s+not|"
    r"aren't|are\s+not|doesn't|does\s+not|didn't|did\s+not|wasn't|was\s+not|without|lacks?)\b",
    re.IGNORECASE,
)

HEDGE_RE = re.compile(
    r"\b(?:may|might|could|possibly|reportedly|allegedly|unclear|uncertain|not\s+sure|"
    r"unsure|cannot\s+(?:verify|confirm)|can't\s+(?:verify|confirm)|unable\s+to\s+(?:verify|confirm)|"
    r"no\s+public\s+(?:information|evidence|record)|i\s+(?:don't|do\s+not)\s+have|"
    r"as\s+of\s+my\s+(?:knowledge|training)|not\s+aware|i\s+couldn't\s+find|"
    r"i\s+could\s+not\s+find|no\s+information)\b",
    re.IGNORECASE,
)


def kind_for(text: str) -> str:
    """Classify a sentence into a claim kind. Incident beats certification
    because "breached despite SOC 2" is an incident claim first."""
    if not text:
        return KIND_OTHER
    if INCIDENT_RE.search(text):
        return KIND_INCIDENT
    if CERTIFICATION_RE.search(text):
        return KIND_CERTIFICATION
    if ENCRYPTION_RE.search(text):
        return KIND_ENCRYPTION
    if ACCESS_CONTROL_RE.search(text):
        return KIND_ACCESS_CONTROL
    if PRIVACY_RE.search(text):
        return KIND_PRIVACY
    if WEAKNESS_RE.search(text):
        return KIND_VULNERABILITY
    return KIND_OTHER


def polarity_for(text: str) -> str:
    """Hedged wording wins over negation: "may not have been breached" is
    uncertain, not a denial."""
    if not text:
        return POLARITY_UNCERTAIN
    if HEDGE_RE.search(text):
        return POLARITY_UNCERTAIN
    if NEGATION_RE.search(text):
        return POLARITY_DENIES
    return POLARITY_AFFIRMS
