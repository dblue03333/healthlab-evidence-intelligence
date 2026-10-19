"""JSON-object transport with the same locally enforced evidence contract as v7."""

from healthlab.signal.triage_v7 import INSTRUCTIONS as EVIDENCE_INSTRUCTIONS
from healthlab.signal.triage_v7 import EvidenceProposal, canonical, response_schema

VERSION = "signal-triage-prompt-8"
INSTRUCTIONS = (
    EVIDENCE_INSTRUCTIONS
    + "\nUse concise text values. Finish the JSON object; never pad output with whitespace.\n"
)

__all__ = ["VERSION", "INSTRUCTIONS", "EvidenceProposal", "canonical", "response_schema"]
