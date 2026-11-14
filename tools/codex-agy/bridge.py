# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp==2.3.0"]
# ///

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

from mcp.server import MCPServer

TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}")
PERMISSIONS = {
    "permissions": {
        "deny": [
            "command(*)",
            "mcp(*)",
            "read_url(*)",
            "execute_url(*)",
            "download_url(*)",
            "write_file(.git)",
            "write_file(.git/)",
        ],
    },
}


def safe_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return (
        path.as_posix() == value
        and not path.is_absolute()
        and all(part not in {".", "..", ".git"} for part in path.parts)
        and bool(path.parts)
    )


def build_server(workspace: Path, task_dir: Path, agy: Path, model: str) -> MCPServer:
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", "core.fsmonitor=false", "-C", str(workspace), *args],
            env=git_env,
            text=True,
            timeout=15,
        )

    def changes() -> list[dict[str, str]]:
        raw = git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored",
            "--no-renames",
            "--ignore-submodules=none",
        )
        return [{"status": item[:2], "path": item[3:]} for item in raw.split("\0") if item]

    if Path(git("rev-parse", "--show-toplevel").strip()).resolve() != workspace:
        raise ValueError("The workspace must be the root of the task checkout.")
    if not task_dir.is_dir() or task_dir.is_relative_to(workspace):
        raise ValueError("Store task manifests outside the agent workspace.")
    if not MODEL_ID.fullmatch(model) or not agy.is_file() or not os.access(agy, os.X_OK):
        raise ValueError("Configure an executable agy and an explicit model slug.")
    lock_path = Path(git("rev-parse", "--absolute-git-dir").strip()) / "codex-agy.lock"

    server = MCPServer(
        "codex_agy",
        instructions=(
            "Preview a local task first. Show the owner the workspace, task, model, and "
            "allowed paths. Call implement_task with the same digest after their approval. "
            "Independently verify the actual diff and tests before review."
        ),
    )

    def load_task(task_id: str) -> dict:
        if not TASK_ID.fullmatch(task_id):
            raise ValueError("Invalid task ID.")
        manifest = (task_dir / f"{task_id}.toml").resolve(strict=True)
        if manifest.parent != task_dir or manifest.suffix != ".toml":
            raise ValueError("Task manifest must be a TOML file in the configured directory.")
        if not manifest.is_file() or manifest.stat().st_size > 32_768:
            raise ValueError("Expected a regular task manifest of at most 32 KiB.")
        raw = manifest.read_bytes()
        task = tomllib.loads(raw.decode())
        base = task.get("base")
        if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
            raise ValueError("base must be a full Git commit hash.")
        paths = task.get("allowed_paths")
        if not isinstance(paths, list) or not paths or not all(map(safe_path, paths)):
            raise ValueError("allowed_paths must contain exact, repo-relative file paths.")
        for path in paths:
            target = workspace / path
            if target.resolve() != target or (target.exists() and not target.is_file()):
                raise ValueError(f"Expected an ordinary file path: {path}")
        for field in ("brief", "verification"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValueError(f"{field} must be nonempty text.")
        context = json.dumps([str(workspace), model, task_id]).encode()
        digest = hashlib.sha256(context + b"\x00" + raw).hexdigest()
        return {
            "task_id": task_id,
            "digest": digest,
            "workspace": str(workspace),
            "model": model,
            "base": base,
            "allowed_paths": paths,
            "brief": task["brief"],
            "verification": task["verification"],
        }

    @server.tool()
    def preview_task(task_id: str) -> dict:
        """Read a local task and return the exact details and digest for human approval."""
        try:
            return load_task(task_id)
        except (OSError, ValueError) as error:
            return {"status": "BLOCKED", "detail": str(error)}

    @server.tool()
    def implement_task(task_id: str, approved_digest: str) -> dict:
        """Dispatch the previewed task after human approval. Review and test the diff separately."""
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {
                    "status": "BLOCKED",
                    "detail": "The bridge is already running in this checkout.",
                }
            task = preview_task(task_id)
            if task.get("digest") != approved_digest or "base" not in task:
                return {
                    "status": "BLOCKED",
                    "detail": "Preview the task again and get fresh approval.",
                }
            if git("rev-parse", "HEAD").strip() != task["base"] or changes():
                return {"status": "BLOCKED", "detail": "Use a clean checkout at the approved base."}
            return run_agy(task)

    def run_agy(task: dict) -> dict:
        paths = task["allowed_paths"]
        prompt = (
            "Implement the task below using file tools. Edit only the exact listed paths. "
            "Leave shell commands, verification, Git history, and external tools to the owner. "
            "Report any blocker and stop.\n"
            f"Allowed paths: {json.dumps(paths)}\n\n"
            f"Task and acceptance criteria:\n{task['brief']}"
        )
        # Forward only the basics needed by agy, including its separately managed login.
        environment = {
            key: os.environ[key]
            for key in ("HOME", "USER", "LOGNAME", "LANG", "TMPDIR")
            if key in os.environ
        }
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        try:
            run = subprocess.run(
                [
                    str(agy),
                    "--model",
                    model,
                    "--sandbox",
                    "--mode",
                    "accept-edits",
                    "--settings",
                    json.dumps(PERMISSIONS),
                    "--disable-slash-commands",
                    "--print-timeout",
                    "3m",
                    "--output-format",
                    "json",
                    "-p",
                    prompt,
                ],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=190,
            )
        except subprocess.TimeoutExpired:
            return {"status": "TIMEOUT", "detail": "Inspect the checkout before retrying."}
        try:
            result = json.loads(run.stdout)
        except json.JSONDecodeError:
            result = {}
        if not isinstance(result, dict):
            result = {}

        changed = changes()
        allowed = {" M", " D", "??"}
        in_scope = bool(changed) and all(
            item["status"] in allowed
            and item["path"] in paths
            and (workspace / item["path"]).resolve() == workspace / item["path"]
            and (item["status"] == " D" or stat.S_ISREG((workspace / item["path"]).lstat().st_mode))
            for item in changed
        )
        base_unchanged = git("rev-parse", "HEAD").strip() == task["base"]
        completed = run.returncode == 0 and result.get("status") == "SUCCESS"
        return {
            "status": "DIFF_READY" if in_scope and base_unchanged and completed else "INSPECT_RUN",
            "model_requested": model,
            "base_unchanged": base_unchanged,
            "changes": changed,
            "exit_code": run.returncode,
            "agy_status": result.get("status"),
            "conversation_id": result.get("conversation_id"),
            "stderr": run.stderr[-2000:],
            "summary": str(result.get("response") or result.get("error") or "")[-3000:],
        }

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Codex to Antigravity MCP bridge")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--agy", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    build_server(
        args.workspace.resolve(strict=True),
        args.tasks.resolve(strict=True),
        args.agy.resolve(strict=True),
        args.model,
    ).run()
