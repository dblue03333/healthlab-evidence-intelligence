## Summary

What changed?

## Why

Why is this change required?

## Related issue

Closes #

## Changes

The main implementation changes.

## Testing

Commands run and their results, for example:

- `uv run --locked python scripts/ci_checks.py`
- focused tests: `uv run --locked pytest tests/test_<area>.py`

## Runs and stored data

- [ ] No effect on stored runs, datasets or issues
- [ ] Changes the editorial runtime fingerprint (`editorial/runs.py` `_RUNTIME_FILES`):
      say which finished runs become read-only and whether they stay approvable
- [ ] Changes a stored record, prompt or contract version: say how old records are read

## Safety

Does this change affect:

- live collection, model calls or their cost?
- the human approval gate before an issue is sent?
- email delivery or publication?
- credentials, raw source contents or logs?

Describe if applicable.

## Breaking changes

- [ ] No breaking changes
- [ ] Breaking change documented (CLI command, configuration or stored format)

## Checklist

- [ ] Acceptance criteria satisfied
- [ ] Tests added or updated
- [ ] `scripts/ci_checks.py` passes (ruff lint, ruff format, pytest)
- [ ] New environment variables are in `.env.example`
- [ ] No credentials, raw source contents or private run output committed
- [ ] Ready for review
