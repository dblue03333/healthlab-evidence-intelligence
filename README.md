# HealthLab Evidence Intelligence

HealthLab collects public health articles and PubMed records, preserves the source
evidence, and prepares drafts a human editor can check against the original text.
The newsletter pilot focuses on healthy ageing in Vietnam.

| Workflow | Entry point | Purpose |
| --- | --- | --- |
| Public-source data | `python -m healthlab.data` | Collect text from the Ministry of Health, HCMC Department of Health and WHO. Keep raw responses and publish versioned, replayable datasets. |
| Editorial review | `python -m healthlab.editorial` | Classify relevance, assess scope, review facts and dates, and draft from evidence that passes. |
| Editorial issue | `python -m healthlab.editorial_issue` | Preview the result, approve an exact version, and export or explicitly send an email. |
| Signal pilot | `python -m healthlab.signal` | Run the original collection, triage and newsletter workflow. |
| PubMed evidence | `python -m healthlab` | Search and preserve abstracts, extract evidence, and draft briefs. |

The newsletter supports supervised runs. Coverage is assessed within the configured
source branches. Editorial accuracy still needs evaluation against human labels.

## Run and verify

Install Python 3.11 and [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --locked --extra dev --extra cloud
uv run --locked python scripts/ci_checks.py
```

Use `uv run --locked python -m healthlab.data --help` or `--help` on any entry
point above. To check a previously exported dataset offline:

```bash
uv run --locked python -m healthlab.data verify /path/to/dataset-bundle
```

Online collection contacts the public sources. Editorial inference uses the
configured model provider. Put runtime credentials in a local `.env` using
[.env.example](.env.example). Keep credentials, saved runs and reports local.

## Repository guide

- [src/healthlab](src/healthlab) contains the application.
- [config](config) contains source registries and storage examples.
- [tests](tests) and [scripts/ci_checks.py](scripts/ci_checks.py) contain the offline checks.
- [Dockerfile](Dockerfile) packages the batch worker. [GitHub Actions](.github/workflows/ci.yml)
  runs lint, offline tests, disposable PostgreSQL tests and a container smoke test.

Local workspace references include `STATUS_NEXT.md` for the latest checkpoint,
`DATA_PIPELINE.md` for data contracts, and `docs/` and `reports/` for runbooks
and verification evidence.

For local development with Codex and Antigravity, see the
[reusable MCP handoff template](tools/codex-agy/README.md).
