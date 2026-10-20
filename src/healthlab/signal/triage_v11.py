"""Versioned compact triage with conservative status-evidence checks.

Checks are lexical gates, not semantic verification. V6 stays replayable unchanged.
"""

import re

from healthlab.evidence import decode_model_json
from healthlab.signal.triage_v6 import INSTRUCTIONS as BASE_INSTRUCTIONS
from healthlab.signal.triage_v6 import CompactProposal
from healthlab.signal.triage_v6 import canonical as canonical_v6

VERSION = "signal-triage-prompt-11"
INSTRUCTIONS = (
    BASE_INSTRUCTIONS
    + """
Status concerns the specific development selected, not the publisher or whole policy field.
An explicitly piloted/trial service remains pilot even when inaugurated or currently serving people.
Choose status_passage_id containing that explicit pilot/trial description, not only the opening ceremony.
Operational means explicitly routine/non-pilot service operation; inauguration alone is insufficient.
Do not narrow 'other localities' into districts or imply a planned replication already occurred.
"""
)
PILOT = re.compile(r"\b(?:pilot|trial|thí điểm|thử nghiệm)\b", re.I)
ROUTINE = re.compile(
    r"\b(?:routine (?:service|operation)|regular service|dịch vụ thường quy|hoạt động thường quy)\b",
    re.I,
)


def canonical(content, document, window):
    from healthlab.signal.triage import source_passages

    result = canonical_v6(content, document, window)
    p = CompactProposal.model_validate(decode_model_json(content))
    units = {u["passage_id"]: u for u in source_passages(document)}
    quote = units.get(p.status_passage_id, {}).get("quote", "")
    uncertain = (
        p.reported_status == "operational" and (PILOT.search(quote) or not ROUTINE.search(quote))
    ) or (p.reported_status == "pilot" and not PILOT.search(quote))
    if p.decision == "include" and uncertain:
        return result.model_copy(
            update=dict(
                decision="needs_review",
                reported_status="unknown",
                reason_codes=["status_unclear"],
                rationale=result.rationale
                + " [Status evidence does not establish the selected pilot/routine status.]",
            )
        )
    return result
