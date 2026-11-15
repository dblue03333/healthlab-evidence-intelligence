"""Hold a macOS sleep assertion only for the lifetime of a CLI invocation."""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def awake_for_cli() -> Iterator[None]:
    process = None
    if sys.platform == "darwin":
        try:
            process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.warning("Sleep assertion unavailable; run on an awake host.")
    try:
        yield
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
