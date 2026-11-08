"""Brand Security.

Findings are derived from the AI answers the audits and prompt runs
already stored: ``response_auditor`` runs the detector registry in
``detectors`` over each answer and escalates nuanced findings to the LLM
``judge``. There is no separate scan.
"""
