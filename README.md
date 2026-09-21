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

## Development benchmark

The example suite in `benchmarks/questions_v0.1.json` contains ten authored development
questions, including a synthetic negative control. It is not a held-out clinical benchmark.
Freeze retrieval before comparing generation configurations:

```bash
uv run python -m healthlab.benchmark freeze --out outputs/benchmark_v01/frozen
uv run python -m healthlab.benchmark run \
  --frozen outputs/benchmark_v01/frozen/frozen.json --out outputs/benchmark_v01/baseline
uv run python -m healthlab.benchmark report \
  --frozen outputs/benchmark_v01/frozen/frozen.json \
  --baseline outputs/benchmark_v01/baseline/baseline.json --out outputs/benchmark_v01/review
```

`freeze` calls PubMed; `run` can call FPT and uses the existing extraction cache.
`report` is offline and produces source/output review packets, unjudged review templates,
and denominator-explicit metrics. Relevance and factual labels require an identified human
reviewer and rationale. Unjudged values remain null; schema or passage validity is not
factual correctness. Recall is unavailable without an independently judged relevant set.
Use a new baseline directory for changed code/model configurations. Run only one process
per benchmark directory and inspect intermediate artifacts before restarting an interrupted
case; benchmark execution is not an exactly-once job scheduler.

An opt-in sample-size prompt experiment runs fresh paired extraction calls on five
predeclared frozen abstracts without changing production prompts or publishing cache entries:

```bash
uv run python -m healthlab.sample_size_experiment \
  --frozen outputs/benchmark_v01/frozen/frozen.json --out outputs/sample_size_trial
```

It makes ten FPT calls, with no automatic retries. Its count/role checks are development
hypotheses, not human factual labels; review results before promoting a prompt change.

## Optional OpenRouter backend

Set `OPENROUTER_API_KEY`, an explicit `OPENROUTER_MODEL` and an endpoint slug in
`OPENROUTER_PROVIDER` in your local `.env`. Keep credentials out of source control.
Provider selection is a global option, before the subcommand:

```bash
uv run python -m healthlab --provider openrouter run --query 'resistance training frailty older adults'
uv run python -m healthlab.benchmark --provider openrouter run \
  --frozen outputs/benchmark_v01/frozen/frozen.json --out outputs/router_baseline
```

Routing pins one endpoint with fallbacks disabled. Requests retain the returned model,
provider, generation ID and reported usage/cost when available. Unexpected returned
model IDs fail explicitly; automatic model selection and routing suffixes are rejected.
No automatic retry is performed. A key spending limit may return HTTP 403; this is a
credit failure, not an empty evidence result. Cache and workflow identities include
provider routing. FPT remains the default. Live access and human quality evaluation
are separate from adapter test coverage; an installed adapter is not a validated backup.

## CP13: local review backend (UI deferred)

Review a validated **synthesis run ID**, not the parent workflow ID. These commands
are offline and require no API key:

```bash
uv run python -m healthlab.review packet SYNTHESIS_RUN_ID
uv run python -m healthlab.review history SYNTHESIS_RUN_ID
uv run python -m healthlab.review submit SYNTHESIS_RUN_ID \
  --expected-head none --action needs_revision \
  --reviewer 'Reviewer name' --rationale 'Explain the review decision'
```

The packet contains the question/scope, original claims, references, passages and
source documents. `submit` supports `accept`, `edit`, `reject`, `needs_revision`.
For subsequent decisions, replace `none` with the current event hash from `history`;
stale submissions are rejected under a local file lock. A human reviewer must provide
their name and rationale. Names are self-declared: this local CLI is not an authenticated
multi-user review service and does not independently establish factual correctness.

To edit Vietnamese wording, use `--action edit --edits path/to/edits.json`, where the
file maps existing claim IDs to nonempty text, for example `{"C1": "Reviewed Vietnamese wording"}`.
Edits are retained separately from the original text and keep the claim's evidence
references. Edits require a subsequent `accept`; they never auto-approve. Omitted claim
IDs retain prior edits. Regeneration creates a new brief version with no inherited approval.

```bash
uv run python -m healthlab.review export SYNTHESIS_RUN_ID
```

Export requires the latest decision to accept that exact version. It produces a hashed
Markdown handoff in the synthesis run directory, with reviewer/event/version identifiers,
editorial text and original evidence. The original draft keeps its original unreviewed
label as historical source; the handoff header records the separate human decision.
Source-object links work within the local store; this is not a standalone attachment.
Exports are historical snapshots and are not recalled after a later rejection; check
`history` before distributing. Keep the object store and review history together.
The roadmap's web UI and nontechnical team usability criteria remain deferred.
