# HealthLab Evidence Intelligence

HealthLab collects public health articles, preserves the source evidence, and
prepares drafts a human editor can check against the original text.
The newsletter pilot focuses on healthy ageing in Vietnam.

| Workflow | Entry point | Purpose |
| --- | --- | --- |
| Public-source data | `python -m healthlab.data` | Collect text from the Ministry of Health, HCMC Department of Health and WHO. Keep raw responses and publish versioned, replayable datasets. |
| Editorial review | `python -m healthlab.editorial` | Classify relevance, assess scope, review facts and dates, and draft from evidence that passes. |
| Recall check | `python -m healthlab.editorial.recall` | Have a second model reread every held-back article and flag any that look on topic for the approver. |
| Editorial issue | `python -m healthlab.editorial.issue` | Preview the result, approve an exact version, and export or explicitly send an email. |
| Source collection | `python -m healthlab.signal` | Check the source registry, probe sources, and collect, resume, export or import a collection. The container runs this entry point. |

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

- [src/healthlab](src/healthlab) contains the application:
  - [signal](src/healthlab/signal) collects and stores public-source articles.
  - [data](src/healthlab/data) turns collections into versioned Bronze, Silver and Gold datasets.
  - [editorial](src/healthlab/editorial) runs the model steps, the code gates, the recall
    check and the issue (preview, approval, email).
  - `common.py`, `models.py`, `store.py`, `provider.py` and `config.py` are shared by all three.
- [config](config) contains source registries and storage examples.
- [tests](tests) and [scripts/ci_checks.py](scripts/ci_checks.py) contain the offline checks
  (ruff lint, ruff format and pytest, the same steps as the CI offline job).
- [Dockerfile](Dockerfile) packages the batch worker. [GitHub Actions](.github/workflows/ci.yml)
  runs lint, offline tests, disposable PostgreSQL tests and a container smoke test.

Local workspace references include `STATUS_NEXT.md` for the latest checkpoint,
`DATA_PIPELINE.md` for data contracts, and `docs/` and `reports/` for runbooks
and verification evidence.
