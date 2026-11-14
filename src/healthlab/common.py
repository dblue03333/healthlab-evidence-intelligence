"""Small helpers shared by collection, data and editorial code."""

import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias

from healthlab.models import IngestionError

# A JSON object as stored in the run store (runs, jobs, checkpoints, records).
# The code that reads one checks the keys it needs; the alias names the shape, not the keys.
JsonObject: TypeAlias = dict[str, Any]


class _CurrentStderrHandler(logging.StreamHandler):
    """Write to whatever sys.stderr is at emit time, so later redirection still works."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass  # Always follow sys.stderr; StreamHandler.__init__ assigns a fixed stream.


def configure_logging() -> None:
    """Send healthlab diagnostics to stderr; stdout stays reserved for each command's JSON result.

    The level comes from HEALTHLAB_LOG_LEVEL (default INFO). Calling it again only updates the level.
    """
    level = os.environ.get("HEALTHLAB_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logger = logging.getLogger("healthlab")
    logger.setLevel(level)
    if not any(isinstance(h, _CurrentStderrHandler) for h in logger.handlers):
        handler = _CurrentStderrHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a text file in one step; a crash leaves the old or the new file, never half."""
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def decode_model_json(content: str) -> "JsonObject":
    """Model output as JSON: one optional code fence, no duplicate keys, finite numbers."""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if (
            len(lines) < 3
            or lines[0].strip().lower() not in {"```", "```json"}
            or lines[-1].strip() != "```"
        ):
            raise IngestionError("invalid_json", "Malformed JSON code fence")
        cleaned = "\n".join(lines[1:-1])

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            cleaned,
            object_pairs_hook=unique_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite number")),
        )
    except ValueError:
        raise IngestionError(
            "invalid_json", "Model output is not an unambiguous JSON object"
        ) from None
    return value
