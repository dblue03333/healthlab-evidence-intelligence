# HealthLab Evidence Intelligence

PubMed-first evidence research pipeline for HealthLab. **Implemented CP6, CP8, CP10 and CP11:** search, fetch, normalized snapshots, per-run audit,
offline replay, FPT structured extraction, passage validation, extraction cache,
versioned draft briefs, citation/context guardrails and resumable orchestration. Semantic correctness evaluation (CP9)
and human review remain separate work;
CP7 retrieval benchmarking is not completed.

Target lineage for the full system:

```text
Claim → EvidenceItem → Passage → DocumentSnapshot → PubMed record
```

## Setup

Python 3.11+ and uv:

```bash
uv sync --extra dev
cp .env.example .env
```

Set `NCBI_EMAIL` to your real contact email for online ingestion. `NCBI_API_KEY` is
optional. No FPT credentials are required for CP6. Keep `.env` out of Git.
The default data store is `outputs/`; override with `HEALTHLAB_STORE_DIR` or `--store`.

## Online ingestion

```bash
uv run python -m healthlab ingest \
  --question "Resistance training có giúp giảm frailty ở người lớn tuổi không?" \
  --query "progressive resistance training frailty older adults" \
  --sort relevance
```

Supply an explicit PubMed query: no automatic translation is performed. Optional
`--scope` records interpreted scope; it is not automatically applied as a filter.
Use both `--mindate YYYY-MM-DD` and `--maxdate YYYY-MM-DD` for publication-date filtering.
The baseline selects at most five unique IDs, does not backfill missing abstracts,
and stores missing-abstract records with extraction eligibility `skipped`.

Each run prints its ID, counts, status and manifest path. Exit code is 0 for completed,
1 for partial/failed. `completed` means ingestion finished, not that evidence has
been clinically reviewed or that every record has an abstract.

## Offline import and replay

Reuse the saved CP3 artifacts without network or credentials:

```bash
uv run python -m healthlab import-probe \
  --artifact experiments/pubmed_pipeline_sample.json \
  --xml experiments/pubmed_efetch_sample.xml
```

This is explicitly recorded as an import of four search candidates plus one manual
edge-case PMID, not a new PubMed search. Use the printed ID for replay:

```bash
uv run python -m healthlab replay RUN_ID
```

Replay verifies cached raw hashes and creates a new run using the current parser;
it never calls PubMed and does not mutate the original run. Keep the entire store
(`runs/` and `objects/`) together. An incomplete cached fetch fails clearly instead
of silently fetching online.

To inspect a normalized snapshot, find its `snapshot_id` in the manifest and read
`outputs/objects/<snapshot_id>` as JSON. That snapshot links to its raw XML record;
the manifest links to the original batch response and search response.

## Verification

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests use synthetic fixtures and mock HTTP, with no API calls.

## Project map

```text
src/healthlab/       Contracts, connector, parser, store, pipeline and CLI
experiments/        CP2/CP3 probes and captured samples
tests/fixtures/     Synthetic parser fixtures (not medical evidence)
outputs/runs/       Per-run manifests (ignored by Git)
outputs/objects/    Shared content-addressed payloads and snapshots (ignored by Git)
```

PubMed coverage is metadata/abstracts only, not full text or comprehensive literature
coverage. The CLI is designed for sequential local use; its request limiter does not
coordinate multiple processes sharing an IP or API key.
NCBI data use is subject to the [NCBI disclaimer and copyright notice](https://www.ncbi.nlm.nih.gov/About/disclaimer.html).

## CP8: extract evidence from saved snapshots

Set `FPT_API_KEY`, `FPT_BASE_URL` and `FPT_MODEL_NAME` in `.env`, then:

```bash
uv run python -m healthlab extract INGESTION_RUN_ID
uv run python -m healthlab extract INGESTION_RUN_ID --cache-only
```

The first command can call FPT for eligible snapshots without cached results. The
second never creates an HTTP client and needs no API key. Use `--max-documents 1`
for a bounded smoke test, or `--refresh` to explicitly bypass cache.
`validated_structure` means schema and exact passages passed; `semantic_support`
remains `not_evaluated`.

## CP10: create a draft evidence brief

```bash
uv run python -m healthlab brief EXTRACTION_RUN_ID
uv run python -m healthlab render-brief SYNTHESIS_RUN_ID
```

`brief` creates a new version using saved evidence (one FPT request when usable evidence
exists). `render-brief` restores an existing Markdown preview offline, after source/reference
checks. The output is at `outputs/runs/<run_id>/brief.md`, with claims, evidence table,
source passages, scope, search date, coverage and an unreviewed label. Empty evidence
does not trigger a model call. Failures keep upstream data and do not publish a valid brief.

Reference validation and conservative language guardrails do not establish semantic
correctness; all briefs remain **AI-generated — not reviewed**.

## CP11: one-command workflow and recovery

```bash
uv run python -m healthlab run --query 'resistance training frailty older adults'
uv run python -m healthlab resume WORKFLOW_RUN_ID
```

The workflow links ingestion, extraction and brief runs, retains attempt history,
reports stage latency/API calls/reported tokens, and resumes compatible saved work.
The CLI prints the workflow ID at startup, then a final JSON summary with per-stage
counts, attempts, errors and metrics. Resume reuses compatible completed stages and
successful extraction cache entries. Changes to workflow code or model configuration
require a new workflow; missing/corrupt saved objects are rejected. A local advisory
lock prevents concurrent execution of the same workflow on macOS/Linux.

Context checks reject missing subgroup qualifiers and unsupported comparator citations
using conservative lexical rules. They can reject valid wording and do not establish
semantic correctness or relevance. A failed synthesis leaves upstream artifacts intact;
its workflow is partial and no validated brief is published. Token counts exclude cache
hits and include only reported usage; transport failures can leave usage unknown.

Checkpoint notes, review reports and live run artifacts are maintained internally and
are not required to use the commands above.
