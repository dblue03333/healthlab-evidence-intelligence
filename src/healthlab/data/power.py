"""Hold a macOS sleep assertion only for the lifetime of a CLI invocation."""

import os
import subprocess
import sys
from contextlib import contextmanager


@contextmanager
def awake_for_cli():
    process = None
    if sys.platform == "darwin":
        try:
            process = subprocess.Popen(
                ["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            print("Warning: sleep assertion unavailable; run on an awake host.", file=sys.stderr)
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
