"""Run lint and acceptance without external connections or model/mail credentials.

The lint steps are the same as the GitHub offline job, so a format slip fails here first.
"""

import argparse
import os
import subprocess
import sys

import pytest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres", action="store_true", help="Allow loopback DB connections only"
    )
    args = parser.parse_args()
    blocked = []

    def guard(event, values):
        if event not in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"}:
            return
        if args.postgres:
            address = values[1] if event == "socket.connect" else None
            if event == "socket.getaddrinfo" and values[0] in {"127.0.0.1", "::1", "localhost"}:
                return
            if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                return
        blocked.append(event)
        raise RuntimeError("CI blocked an external connection: " + event)

    if not args.postgres:
        os.environ.pop("HEALTHLAB_TEST_POSTGRES_DSN", None)
    elif not os.environ.get("HEALTHLAB_TEST_POSTGRES_DSN"):
        parser.error("Set HEALTHLAB_TEST_POSTGRES_DSN to a disposable loopback database")
    if not args.postgres:
        for command in (["check"], ["format", "--check"]):
            lint = [sys.executable, "-m", "ruff", *command, "src", "tests", "scripts"]
            if subprocess.run(lint).returncode:
                return 1
        # Files come from [tool.mypy] in pyproject.toml.
        if subprocess.run([sys.executable, "-m", "mypy"]).returncode:
            return 1
    sys.addaudithook(guard)
    options = ["-q"]
    if args.postgres:
        options.append("tests/test_signal_postgres_integration.py")
    code = pytest.main(options)
    print(f"Blocked connection attempts: {len(blocked)}")
    return 1 if blocked else code


if __name__ == "__main__":
    raise SystemExit(main())
