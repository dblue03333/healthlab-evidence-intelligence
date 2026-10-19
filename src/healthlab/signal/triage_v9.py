"""Evidence contract over explicit text transport; native JSON modes remain experimental."""

from healthlab.signal.triage_v7 import INSTRUCTIONS as EVIDENCE_INSTRUCTIONS
from healthlab.signal.triage_v7 import EvidenceProposal, canonical, response_schema

VERSION = "signal-triage-prompt-9"
INSTRUCTIONS = (
    EVIDENCE_INSTRUCTIONS
    + """
Output serialization: start with { and end with }. Use double-quoted JSON keys and strings.
Write each required key once. Write null as the JSON literal null, arrays as JSON arrays.
Never put **, backticks, comments, reasoning, ellipses or decoration around keys/values.
Before finishing, check balanced braces, commas and the required keys. Keep text values short.
"""
)

__all__ = ["VERSION", "INSTRUCTIONS", "EvidenceProposal", "canonical", "response_schema"]
