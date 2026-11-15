"""Local diagnostics without exception messages, source lines, headers or locals."""

import time
import traceback
from datetime import UTC, datetime

from healthlab.common import JsonObject


class Span:
    """Durations only; absolute monotonic clocks must never cross processes."""

    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self.monotonic = time.perf_counter()
        self.cpu = time.process_time()

    def finish(self) -> JsonObject:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "wall_seconds": round((datetime.now(UTC) - self.started_at).total_seconds(), 6),
            "monotonic_seconds": round(time.perf_counter() - self.monotonic, 6),
            "process_cpu_seconds": round(time.process_time() - self.cpu, 6),
        }


def exception_diagnostic(exc: BaseException) -> JsonObject:
    """Keep call sites and chained types, never potentially secret exception text."""
    chain: list[JsonObject] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(
            {
                "type": type(current).__name__,
                "frames": [
                    {"file": f.filename, "line": f.lineno, "function": f.name}
                    for f in traceback.extract_tb(current.__traceback__)[-24:]
                ],
            }
        )
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return {"exception_chain": chain, "messages_and_locals_omitted": True}
