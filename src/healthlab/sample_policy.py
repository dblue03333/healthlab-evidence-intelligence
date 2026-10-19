"""Conservative sample-count checks, not general semantic verification."""

import re

from healthlab.models import IngestionError

SAMPLE_INSTRUCTIONS = (
    "For each sample_sizes entry, verify three independent facts: it counts HUMAN people; "
    "the exact value is explicitly written in its supporting_passage; and the quoted text "
    "explicitly supports the role. Counts of articles, reviews, meta-analyses, studies, trials, "
    "statements, sessions or measurements are never human sample sizes. Use [] if no human "
    "count is stated. Do not add country/group counts, subtract dropouts or infer an unstated "
    "total. Preserve an explicit overall total AND explicit arms/subgroups, with group labels "
    "that distinguish them. Experts in a Delphi study are people: label their panel and phase, "
    "not patients. 'Participated' alone means unspecified, not recruited. Recruitment does not "
    "imply randomization; randomization does not imply completion. A statement of study design "
    "does not establish a count's role. Use unspecified if the stage is not explicit. "
    "For example, 'We reviewed 42 papers' gives []; '24 experts participated in round 2' gives "
    "value 24, role unspecified, group 'expert panel, round 2'. '50 enrolled; 8 withdrew; "
    "the remaining participants were randomized' does not state a randomized count: never "
    "derive 42. Each distinct stage requires its own explicit number and support. "
    "Copy supporting text exactly, preserving Unicode whitespace; do not rewrite it. "
)

_SMALL = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen"
).split()
_TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()


def _words(value):
    if value < 20:
        return _SMALL[value]
    if value < 100:
        return _TENS[value // 10] + (" " + _SMALL[value % 10] if value % 10 else "")
    if value < 1000:
        return (
            _SMALL[value // 100] + " hundred" + (" " + _words(value % 100) if value % 100 else "")
        )
    return None


def count_pattern(value):
    forms = [str(value), f"{value:,}"]
    words = _words(value)
    parts = [re.escape(v) for v in set(forms)]
    if words:
        parts.append(re.escape(words).replace(r"\ ", r"[\s-]+"))
        if "hundred " in words:
            parts.append(
                re.escape(words.replace("hundred ", "hundred and ")).replace(r"\ ", r"[\s-]+")
            )
    # Do not match 14 inside 114, 14.5, 14%, 14,000 or 14-year-old.
    return rf"(?<![\w.,])(?:{'|'.join(parts)})(?![\w]|[.,]\d|\s*%|[-\s]+year)"


_NONHUMAN = (
    r"(?:systematic\s+)?reviews?|meta[-\s]?analyses|articles?|papers?|studies|trials?|"
    r"recommendations?|statements?|sessions?|measurements?"
)
_ROLE = {
    "recruited": r"\b(?:recruit\w*|enrol\w*)\b",
    "randomized": r"\b(?:randomi[sz]\w*|randomly\s+(?:assign\w*|allocat\w*))\b",
    "analyzed": r"\b(?:analy[sz]\w*|analys[ei]s)\b",
    "completed": r"\bcomplet\w*\b",
}


def verify_sample_counts(evidence):
    """Reject explicit unsupported counts; never rewrite/drop an entry to make it pass.

    Role cues are checked in a sentence containing the number, not full entailment.
    Complex clauses and unfamiliar number spellings still require human review.
    """
    for index, sample in enumerate(evidence.sample_sizes):
        quote = sample.supporting_passage
        number = count_pattern(sample.value)
        if not re.search(number, quote, re.I):
            raise IngestionError(
                "sample_count_not_explicit",
                f"sample_sizes.{index}: quoted text does not explicitly state the count",
            )
        # A second human/ambiguous occurrence needs review, not automatic rejection.
        occurrences = list(re.finditer(number, quote, re.I))
        if all(
            re.match(rf"\s+(?:{_NONHUMAN})\b", quote[match.end() :], re.I) for match in occurrences
        ):
            raise IngestionError(
                "sample_nonhuman_count",
                f"sample_sizes.{index}: quote counts documents or other non-human entities",
            )
        role_supported = True
        if sample.role in _ROLE:
            cue = _ROLE[sample.role]
            sentences = re.split(r"(?<=[.!?;])\s+", quote)
            role_supported = any(
                re.search(number, sentence, re.I)
                and re.search(
                    cue,
                    re.sub(
                        rf"\b(?:not|never)\s+(?:\w+\s+){{0,2}}?{cue}", " ", sentence, flags=re.I
                    ),
                    re.I,
                )
                for sentence in sentences
            )
        if not role_supported:
            raise IngestionError(
                "sample_role_unsupported",
                f"sample_sizes.{index}: quote lacks an explicit cue for role {sample.role}",
            )
