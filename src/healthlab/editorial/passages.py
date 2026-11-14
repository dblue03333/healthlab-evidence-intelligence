"""Versioned editorial text slices with exact offsets; no extraction or inference."""

import re

POLICY = "editorial-passages-2"
MAX_PASSAGE_CHARS = 1800

# Nonterminal city/administrative abbreviations and personal titles. Do not
# suppress generic initials or sentence-final abbreviations such as "etc.".
_NONTERMINAL = {
    "tp",
    "tx",
    "tt",
    "gs",
    "pgs",
    "ts",
    "ths",
    "bs",
    "bscki",
    "bsckii",
    "cki",
    "ckii",
    "dr",
    "prof",
    "mr",
    "mrs",
    "ms",
}
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
_TOKEN = re.compile(r"(?:^|[\s(\[“\"'])([\w.]+)\.$")


def _nonterminal_period(text, boundary):
    if "\n" in boundary.group() or text[boundary.start() - 1] != ".":
        return False
    # Titles such as PGS.TS. are one token; every component must be recognized.
    token = _TOKEN.search(text[max(0, boundary.start() - 80) : boundary.start()])
    return bool(token) and all(part.casefold() in _NONTERMINAL for part in token[1].split("."))


def editorial_passages(text):
    """Keep abbreviation continuations together, preserving all nonspace text.

    Explicit newlines still end a unit. Long sentences are bounded by the same
    limit as legacy passages and may therefore still need reviewed date links.
    No paragraph structure is invented from flattened source text.
    """
    spans, start = [], 0
    for boundary in _BOUNDARY.finditer(text):
        if _nonterminal_period(text, boundary):
            continue
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    units = []
    for start, end in spans:
        while start < end:
            stop = min(start + MAX_PASSAGE_CHARS, end)
            if stop < end:
                split = text.rfind(" ", start, stop)
                if split > start:
                    stop = split
            if text[start:stop].strip():
                units.append(
                    {
                        "passage_id": f"P{len(units) + 1}",
                        "start": start,
                        "end": stop,
                        "quote": text[start:stop],
                    }
                )
            start = stop
            while start < end and text[start].isspace():
                start += 1
    return units


def source_passages(document):
    """Deterministic sentence-like slices. IDs resolve to exact Unicode offsets."""
    text = document.text
    spans, start = [], 0
    for boundary in re.finditer(r"(?<=[.!?])\s+|\n+", text):
        spans.append((start, boundary.start()))
        start = boundary.end()
    spans.append((start, len(text)))
    units = []
    for start, end in spans:
        while start < end:
            stop = min(start + 1800, end)
            if stop < end:
                split = text.rfind(" ", start, stop)
                if split > start:
                    stop = split
            if text[start:stop].strip():
                units.append(
                    {
                        "passage_id": f"P{len(units) + 1}",
                        "start": start,
                        "end": stop,
                        "quote": text[start:stop],
                    }
                )
            start = stop
            while start < end and text[start].isspace():
                start += 1
    return units
