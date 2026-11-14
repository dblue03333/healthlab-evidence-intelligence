"""Small helpers shared by collection, data and editorial code."""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from healthlab.models import IngestionError


def utc_now():
    return datetime.now(UTC).isoformat()


def write_text_atomic(path: Path, text: str):
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


def decode_model_json(content: str):
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
