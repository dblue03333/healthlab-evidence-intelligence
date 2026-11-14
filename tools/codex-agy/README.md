# Codex → Antigravity MCP template

Codex plans and reviews. Antigravity edits a local task checkout after the owner
approves the handoff. This is a development tool that runs on your computer.
Run it on macOS or Linux. The example task is a website copy change.

```text
Task TOML → Codex previews it → you approve → bridge.py runs agy
                ↑                                  ↓
             you review ← Codex checks Git diff and local tests
```

## Set up once for a project

1. Install [Antigravity CLI](https://antigravity.google/docs/getting-started)
   and [uv](https://docs.astral.sh/uv/). Sign into Antigravity with interactive
   `agy`, then use `agy models` to choose an exact model slug for your account.
   The example uses Hydrogen High.
2. Keep this template in a trusted local checkout. Choose a separate clean
   checkout for implementation, such as a Git worktree for a website project.
   Include any required work in the starting commit. Create a local task
   directory outside the implementation checkout to store the TOML files.
3. From the folder containing `bridge.py`, run `uv sync --locked --script bridge.py` to
   install the MCP dependency. This uses the script's separate environment.
4. Add the block below to your **local** `~/.codex/config.toml`. Replace every
   example path with its absolute path. The command path comes from
   `command -v uv` and the Antigravity path comes from `command -v agy`.
   Pick a unique server name if you configure this for multiple projects.

```toml
[mcp_servers.codex_agy]
command = "/absolute/path/to/uv"
args = [
    "run", "--offline", "--locked", "--script", "/absolute/path/to/bridge.py",
    "--workspace", "/absolute/path/to/website-checkout",
    "--tasks", "/absolute/path/to/local-task-files",
    "--agy", "/absolute/path/to/agy",
    "--model", "gemini-4-hydrogen-high",
]
enabled_tools = ["preview_task", "implement_task"]
default_tools_approval_mode = "prompt"
tool_timeout_sec = 240

[mcp_servers.codex_agy.tools.preview_task]
approval_mode = "approve"
```

The config tells Codex how to launch the Python script over MCP. The script
starts `agy` only when the implementation tool is called. Restart Codex, open
the implementation checkout and use `/mcp` to check for `codex_agy`. The bridge
uses the same configured checkout and model regardless of which chat calls it.
See [Codex MCP settings](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Run the first task

1. Copy [task.example.toml](task.example.toml) into the local task directory as
   `WEBSITE-001.toml`. In the task checkout, run `git rev-parse HEAD` and put the
   full hash in `base`. Replace the sample paths, brief and verification text.
   Start with one approved copy change and the site's existing checks.
2. Ask Codex to call `preview_task` for `WEBSITE-001`. Review the exact checkout,
   model, base, allowed paths and brief it returns. Approve the task when those
   match your intent.
3. Tell Codex to call `implement_task` for the same task and preview digest,
   then approve the tool call. The digest ties approval to the manifest,
   workspace, and model. A bridge lock prevents concurrent dispatches to this
   checkout through the bridge. Keep one writer across your other tools too.
4. Inspect the returned status and Git changes. Use `git status --short` to
   include new files, inspect `git diff` and any new files, then run the checks in
   the manifest. Review the result in a fresh Codex chat and decide what to keep.

`DIFF_READY` means the CLI reported success, the base stayed the same and Git
reports only allowed unstaged edits, deletions or new files. It does not mean
tests passed. For `BLOCKED`, `INSPECT_RUN` or a timeout, see the returned details
and inspect the checkout first. The bridge expects a clean checkout (including ignored files) at the
approved base; use a fresh checkout or commit an accepted result before the
next dispatch. Code changes and Git history are yours to review and manage.

The bridge requests Antigravity's terminal sandbox and passes a per-run policy
that denies shell commands, MCP and web tools. File scope in the prompt is
audited through Git **after execution**. The Git audit is not a file-access
sandbox, and Antigravity can send repository context to its model provider.
Use an isolated checkout without secrets or data you cannot send. Keep actual
task files, local paths and credentials out of Git. The website's tests and
deployment remain part of its existing development workflow.

The original slug lab and its local settings are separate from this template.
