# HealthLab Evidence Intelligence

Source, reusable configuration and offline regression fixtures are versioned here.
Credentials, run stores, internal plans and evaluation reports stay local under
`.env`, `outputs/`, `local_docs/` and `docs/`; local report paths below are not
part of the GitHub checkout.

## Signal triage recovery — 19 October 2026

Opt-in `triage propose --resumable --total-call-budget N` pins a **new** triage
run's provider/model, prompt/runtime/input identity, lifetime call budget and
`--max-attempts` (default 3). Invoke the same command with the latest head to
continue. `--max-model-calls` remains an additional per-invocation cap; restarting
does not replenish the lifetime budget. Legacy measured runs cannot silently
enter this mode, and managed runs cannot bypass their budget through legacy mode.

Structured daily quotas, short rate limits and HTTP 500/502/503/504 responses save
a cooldown and stop the invocation. No sleeping, automatic scheduling, fallback
or in-process retry occurs. A later explicit invocation can retry after the saved
lower bound, within both budgets. Completed chunks are reused only with matching
prompt/schema/provider/input/coverage traces and are revalidated. Invalid output,
auth/config errors, ambiguous 429s and unknown transport/interruption outcomes
remain blocked for inspection. The ledger counts calls before dispatch, so a
crash can conservatively consume budget without proving a request was sent.
This is a per-run budget; it does not coordinate project quotas across other jobs.

```bash
uv run python -m healthlab.signal triage propose NEW_TRIAGE_RUN_ID \
  --provider gemini --prompt-version signal-triage-prompt-7 \
  --expected-head none --resumable --total-call-budget 10 \
  --max-model-calls 5 --max-documents 1
uv run python -m healthlab.signal triage status NEW_TRIAGE_RUN_ID
```

The recovery report (`outputs/signal/triage_recovery_20261019_01/report.md`, local only) records
568 passing offline tests, 4 PostgreSQL integration skips, and a separate prepared
continuation of the remaining six Gemini cases. Its first execution correctly
paused without API calls until the prior quota evidence's lower bound, about
07:00 Vietnam time on 20 October; quota availability at that time is not guaranteed.
The 18 October adapter results below are historical; the
19 October continuation (`outputs/signal/gemini_continuation_20261019_01/report.md`, local only)
raised the combined valid decisions to 17/23 before quota exhaustion. Gemini live
draft, human acceptance and cloud operations remain incomplete.

## Signal newsletter pilot (local setup)

Gemini is an explicit opt-in provider (`--provider gemini`); the FPT default and
stable triage prompt v6 are unchanged. Configure `GEMINI_API_KEY` in ignored `.env`
and pin `GEMINI_MODEL_NAME=gemini-3.8-flash`. The direct Google adapter makes one
non-streaming Generate Content request per call, with no retries, model fallback,
tools, or automatic billing changes. Default temperature is 1.0, thinking level
`low`, and the 8192-token output cap includes reasoning. Key/project Free Tier
status must be checked in AI Studio; `check-config` cannot verify billing from a key.
Google Free Tier quotas and availability can change. Pro is never selected as a fallback.

For native JSON Schema triage, explicitly select the v7 evidence contract:

```bash
uv run python -m healthlab.signal triage propose TRIAGE_RUN_ID \
  --provider gemini --prompt-version signal-triage-prompt-7 \
  --expected-head none --max-documents 1 --max-model-calls 5
```

Use the current triage head instead of `none` after any recorded decision. A long
article needs all chunk calls plus its aggregate call within the budget. The
opt-in native-schema draft path is available to all schema-capable providers:

```bash
uv run python -m healthlab.signal draft COLLECTION_RUN_ID \
  --triage-run TRIAGE_RUN_ID --provider gemini --structured-output
```

Both paths retain raw responses, requested/actual model, native token metadata,
schema hashes and incomplete/blocked errors. JSON syntax/shape checks do not
certify evidence meaning or editorial accuracy. No malformed JSON is repaired;
unknown references, duplicate keys, incomplete answers and mismatched models are
rejected. Text generation remains available without the explicit schema options.
No human approval, email delivery or cloud deployment is implied by these commands.
The first Gemini integration run passed 3/3 synthetic schema canaries and validated
the first four saved articles (including full long-article chunk coverage), then
stopped on a Google HTTP 503 high-demand response at the fifth article. The
23-article regression and live draft acceptance remain incomplete. See the
adapter report (`outputs/signal/gemini_adapter_20261018_01/report.md`, local only); this is not
production reliability or independent human accuracy acceptance.

The `healthlab.signal` CLI now defaults to bounded official HTML article collection
from the Ministry of Health's population department, Ho Chi Minh City's health
department and WHO Ageing. It keeps source failures, immutable raw snapshots,
publication-date evidence, extraction locators, model input/output and supporting
passages. This is a local pilot; topic relevance and semantic correctness still
require separate triage and review.

Current editorial scope is **Vietnam domestic developments only** under
`signal-triage-rubric-2`. The three collection sources remain unchanged. A foreign
policy/global context article stays outside the newsletter even when reposted by
a Vietnamese official source or potentially useful as a comparison. WHO/UN work
directly concerning Vietnam may qualify. See the
current rubric and 14-article reconciliation (`docs/healthlab_signal_rubric_v2_20261017.md`, local only).
The frozen 22-article editorial reference (`docs/healthlab_signal_editorial_reference_20261017.md`, local only)
records agent-adjudicated labels, reasons, literal evidence and separate unlabelled
source inputs. It has no include examples and is not an independent human gold set;
input limits and supplementary-evidence differences are documented before model testing.

The historical editorial rehearsal (`docs/healthlab_signal_editorial_rehearsal_20261017.md`, local only)
now provides a one-story, evidence-linked draft for content review in the
2–8 June 2026 window. One isolated R02 recovery read all four chunks and produced
a model hold; a separately recorded agent decision then excluded it as background.
The historical draft preserves pilot status, source/event dates, raw model output,
a versioned agent edit log and seven claim groups. Prior evaluation heads and
scores remain unchanged. This is a curated content rehearsal: coverage for all
three required sources is explicitly incomplete, so it is not ready for publication
approval or email. Six provider calls were used; no runtime changes, human labels,
approval or delivery were made in this rehearsal.

The stable triage default remains **prompt v6**. New experiments are explicit:
`triage propose ... --prompt-version signal-triage-prompt-7` requests JSON Schema,
while `--prompt-version signal-triage-prompt-8` requests JSON-object syntax with
the same locally validated evidence contract. The v7 FPT experiment was stopped
after whitespace/token-limit degeneration; a simple schema capability probe was
insufficient to establish support for the full triage contract. No adapter
silently changes modes, repairs malformed JSON or retries a failed output.
Both experiments pin the local response schema and requested mode in chunk/final
coverage traces. V7/V8 identify a status subject and action basis; code maps the
basis to reported status and derives freshness from the selected literal event
date. An undated proposal stays held, while an old issued instrument keeps its
adopted status without becoming a current development. These checks do not certify
the semantic interpretation or establish editorial reliability. See the
v7 hardening results (`outputs/signal/triage_v7_hardening_20261017_01/report.md`, local only).

Experimental v9 uses text transport with the evidence object contract; its
diagnostic pass still had duplicate-key failures. V10 moves chunk/final choices
to fixed-position JSON arrays and builds object keys and explanatory text in code.
Its two live canaries yielded one valid decision and one rejected short vector;
it is not accepted as the default. Both are opt-in through `--prompt-version`.
Malformed/short vectors are not padded or repaired. New interruptions during
provider HTTP calls are audited as `provider_interrupted` and propagated; an
interrupted request may still have consumed provider resources.

The historical triage hardening report (`docs/healthlab_signal_triage_hardening_20261017.md`, local only)
records the frozen prompt v6 contract: the model selects literal date/passage IDs,
while code assigns operational codes and applies independent provenance/freshness
gates. Long articles use bounded chunk evidence plus aggregation with replayable
coverage proofs; `triage propose --max-model-calls` reserves a whole article's call
budget before starting. Verification v2 requires the claimed event date to match
a complete literal date in captured evidence; historical v1 verification remains
readable. See the runtime guide (`docs/healthlab_signal_triage_v5_20261017.md`, local only).
That v6 regression produced 30/31 valid decisions and selected all four
reference positives. One long article failed a chunk's JSON output and remains
explicitly unresolved; raw model recommendations still need gates and review.
465 tests passed, 4 PostgreSQL integration tests skipped. These are agent-reference
regressions, not editorial production acceptance or independent human accuracy.

The historical first independent selection evaluation (`docs/healthlab_signal_selection_eval_20261017.md`, local only)
preserves those 22 cases and adds three verified historical positives, each in its
own seven-day window. One frozen FPT pass made 22 calls for 25 cases: 15 valid
decisions, three input-limit blocks and seven invalid outputs. Two positives were
selected; the third failed an exact-quote check. Two unsupported raw includes
were held by the provenance gate. Fair end-to-end decision agreement was 12/21;
the references remain agent-authored, and all documents are from the population
department. This is not editorial production acceptance. The dated report records
the date-contract, long-input and verification work needed next; runtime code was
not changed during this evaluation.

`config/signal_sources.json` uses registry schema 3. The population department
uses its public WordPress posts API on the same host to inventory publication dates
in the requested window, validating page lengths and API totals before fetching
canonical HTML article bodies. HCMC uses observed sequential category pagination;
WHO uses its Ageing landing and five Vietnam news sections. Global filtered WHO
All news is outside the current domestic monitoring scope. See the
three-part completion and verification report (`docs/healthlab_signal_coverage_completion_20261017.md`, local only).

Listing-page budgets are 6/6/12 and article-fetch budgets are 180/100/100 for
population/HCMC/WHO. The response cap is 2 MB by default, explicitly 4 MB for WHO,
with a validated hard configuration ceiling of 8 MB. There is no automatic cap
increase or redirect following. Native certificate verification uses `truststore`.

When a listing provides a date bound to a single article, configured sources may
record an out-of-window date check without fetching the article. Such checks
explicitly say `not_fetched_outside_window` and retain raw/date evidence; they do
not claim the body was read. Missing/conflicting dates still require fetching or
review. `article_fetch_count` distinguishes article requests from date-only checks.
A configured chronological boundary stops pagination only if every candidate on
the page has a publication date before the window; this assumes chronological
ordering. Other seed branches are still checked.

`all_candidates_checked` means the discovered article inventory was assessed,
including explicit date-only skips. `coverage_complete` requires no deferred
articles, discovery gaps or article errors. `completed` covers the configured API
or chronological listing scope; `website_coverage_complete` remains false. Raw
responses and source gaps remain auditable. Required-source partial status still
blocks issue approval. Topic relevance remains a separate triage decision.

Parser `official-html-4` retains the HCMC introduction (`sapodetail`) alongside the
body and records its `Cập nhật` header as update evidence only. It inventories
video/audio, embedded PDFs and image-only articles without claiming to have read
their contents. In-window media-only pages remain pending review and cannot become
text evidence. Published dates still come from article metadata or a date bound to
that article on the saved listing. Old raw objects and snapshots remain immutable;
fixed extraction creates a new content version when text changes.

Offline regression fixtures under `tests/fixtures/signal_html/` preserve captured
source responses with hashes. Tests check the real DOM cases, missing/conflicting
dates, source budgets and repeat runs: identical input retains snapshot IDs;
changes to an introduction produce an updated snapshot. These checks verify
extraction and provenance, not factual claims or editorial selection accuracy.

HTML URL and extraction rules now live in each source's `html_rules` configuration.
One `ConfiguredHTMLAdapter` handles exact hosts, full-path include/exclude patterns,
query constraints, explicitly dropped tracking parameters, scoped title/body/lead
selectors and publication/update date rules. Each source check and extraction links
to the immutable rules object used for that run. Schema 1 RSS/PubMed and schema 2
legacy HTML profiles still load. New HTML sources do not need a new profile enum or
collector branch when their structure fits these rules. See the disabled, tested
example in `config/signal_sources_html_example.json` and the
source onboarding guide (`docs/healthlab_signal_html_rules.md`, local only). Arbitrary CSS,
JavaScript rendering and media transcription are outside these rules. Declarative
`html_discovery` configures finite listing traversal, pagination selectors, query
filters and optional chronological boundaries. New runs use `signal-collection-7`;
resume requires the same collector/config version. Earlier completed runs remain readable.

```bash
uv run python -m healthlab.signal check-config
uv run python -m healthlab.signal probe-official
uv run python -m healthlab.signal collect --mindate 2026-10-02 --maxdate 2026-10-09
uv run python -m healthlab.signal triage prepare COLLECTION_RUN_ID
uv run python -m healthlab.signal triage packet TRIAGE_RUN_ID --html
# Optional model calls for a bounded proposal batch; reviewer can instead submit decisions offline.
uv run python -m healthlab.signal triage propose TRIAGE_RUN_ID --expected-head none --max-documents 6
uv run python -m healthlab.signal draft COLLECTION_RUN_ID --triage-run TRIAGE_RUN_ID
uv run python -m healthlab.signal preview ISSUE_RUN_ID
uv run python -m healthlab.signal status ISSUE_RUN_ID
uv run python -m healthlab.signal approve ISSUE_RUN_ID \
  --expected-version ISSUE_SHA256 --reviewer 'Reviewer name' --rationale 'Review decision'
uv run python -m healthlab.signal email ISSUE_RUN_ID
```

`probe-official` checks the public candidate pages in
`config/signal_official_probe_targets.json` (Ministry of Health/population,
Ho Chi Minh City health department, WHO Ageing). It saves raw HTML and an access
report under a separate `signal_source_probe` run. It discovers page links and
provides title-only topic hints; it does not fetch articles, filter publication
dates, call a model, create a newsletter or send email. Redirects, non-HTML
responses, connection failures and timeouts remain visible. A partial/failed
probe exits with code 1. Candidate domains still require endpoint verification;
reachable does not establish monitoring readiness. The probe and article collector
are separate commands.

The earlier RSS/PubMed registry is preserved in
`config/signal_sources_legacy_sample.json`. To reproduce that bounded smoke sample:

```bash
uv run python -m healthlab.signal collect \
  --registry config/signal_sources_legacy_sample.json \
  --mindate 2026-10-02 --maxdate 2026-10-09
```

Official collection can return `partial` while retaining usable documents. Required
source gaps block approval. Video/audio transcription, original policy attachments,
policy effectiveness and Vietnam applicability are pending work; collected articles
are not automatically accepted newsletter signals.

The final command only exports individual `.eml` previews. It requires approval
and `HEALTHLAB_GMAIL_USER` / comma-separated `HEALTHLAB_EMAIL_TO` in local `.env`.
Actual Gmail SMTP delivery additionally requires `HEALTHLAB_GMAIL_APP_PASSWORD`
and an explicit `email ISSUE_RUN_ID --send`. Send one message per recipient;
accepted means SMTP accepted the message, not confirmed inbox placement. Accepted,
pending or ambiguous deliveries are not blindly resent. Review the ledger before
resolving an unknown outcome. Do not send passwords in chat or commit `.env`.

`revise ISSUE_RUN_ID --expected-version ISSUE_SHA256 --content draft.json` replaces
the draft with a validated `Draft` JSON object and resets approval. Reviewer names
are local declarations; a multi-user review UI, cloud storage and scheduling remain
future setup. The original PubMed evidence workflow below is unchanged.

### Collector recovery and portable storage

New HTML collections use `signal-collection-5`. Successful HTTP responses and
per-article results are checkpointed as hashed objects. Transient GET errors retry
at most three total attempts per URL, including interrupted starts, with bounded
backoff and a 0.25-second request interval. Redirects, permanent errors, invalid
HTML and oversized responses are not retried. Long `Retry-After` values stop the
request instead of retrying early. The saved policy is reused on resume.

```bash
uv run python -m healthlab.signal collection-status COLLECTION_RUN_ID
uv run python -m healthlab.signal resume-collection COLLECTION_RUN_ID
uv run python -m healthlab.signal export-collection COLLECTION_RUN_ID --out /path/to/new-bundle
uv run python -m healthlab.signal verify-bundle /path/to/new-bundle
```

The collector prints its run ID to stderr before work starts. Resume supports new
interrupted HTML collections, retaining their listing, rules, date window and
successful responses. Finished collections are returned unchanged; start a new
collection for fresh discovery or a finished partial run. Older collections remain
readable/exportable, but cannot be resumed with the new checkpoint implementation.
Local run/index commits use a recoverable journal under the existing process lock.
Status reads an atomic manifest snapshot and may lag an active writer.

A bundle contains one collection, its index and all referenced objects, including
raw/extractions/rules/checkpoints/previous snapshots. Keep the original bundle
unchanged; copy it to a working directory before resuming. It is not a backup of
all issues, approvals or email delivery history. Export verifies references and
hashes before publishing a new directory and never overwrites an existing bundle.

Storage contracts separate immutable objects from mutable collection state.
Optional adapters provide GCS create-only hash objects and PostgreSQL transactional
run/index state with a namespace writer lock. Local storage remains the default.

```bash
uv sync --extra dev --extra cloud
# Copy/edit config/signal_storage_cloud.example.json; credentials stay outside JSON.
uv run python -m healthlab.signal --storage-config /path/to/cloud.json init-storage
uv run python -m healthlab.signal --storage-config /path/to/cloud.json import-collection /path/to/bundle
uv run python -m healthlab.signal --storage-config /path/to/cloud.json collect --mindate 2026-10-12 --maxdate 2026-10-18
```

These cloud commands require an existing private bucket, GCS Application Default
Credentials and `HEALTHLAB_SIGNAL_DATABASE_URL` (or the configured environment
variable). Use a direct/session PostgreSQL connection, not transaction pooling;
losing the writer connection aborts work. `init-storage` explicitly creates the
version-1 tables/namespace; `collect` never provisions resources or initializes DB
schema. Namespace setup binds the expected bucket/prefix, and imports reject run
conflicts. GCS/DB operations fail explicitly, with no fallback to local storage.

Cloud support currently covers HTML collection/status/resume/import/export and
core triage prepare/propose/submit/packet commands (JSON; HTML rendering is local).
Bounded listing traversal uses the same collection storage/checkpoint contracts.
Drafting, review, email, research, backlog processing, scheduler and
cloud operational acceptance remain separate work. See the
storage/recovery handoff (`docs/healthlab_signal_collector_storage_recovery_20261016.md`, local only)
for verification and deployment prerequisites.

### T1.3: triage before drafting

Every new issue requires `--triage-run`. `triage prepare` binds an immutable source
collection, monitoring window, rubric and snapshot inventory. It performs no model
calls: lexical hints and date proposals leave human labels null. Supply both
`--mindate` and `--maxdate` to evaluate another monitoring window; old background
articles cannot become current updates through their modified date or first fetch.

`triage propose` optionally calls FPT/OpenRouter/Gemini for at most six unattempted eligible
documents (configurable up to 30). Each document is attempted once; failures or
interrupted calls are not blindly retried. Articles over 20,000 characters use bounded
chunks and aggregation; over-budget input remains explicitly unresolved. Model
input/output, actual model, usage, schema and coverage proofs, decisions and
authorship are saved. Current contracts select supplied passage/date IDs;
code resolves exact quote/offsets and rejects
unknown IDs. It requires a concrete development summary, freshness explanation and
event date with an exact supporting excerpt when known. Publication date is not an
event date. Literal footer attribution is supplied separately; a publisher homepage
is not a traced original article. Unverified repost recommendations, background or
unclear development, and model-reported events outside the window are held as
`needs_review`, preserving the raw AI recommendation beside the effective decision.
Malformed output and fabricated passage references still fail validation. Older
v1–v10 prompt histories remain readable under their original contracts; no prior run
or human label is rewritten. These
checks do not verify date interpretation, factual accuracy or legal effect.
After a validator repair, `triage revalidate TRIAGE_RUN_ID SNAPSHOT_ID
--expected-head HEAD --prompt-version signal-triage-prompt-3` rechecks one saved
invalid response without a provider call. It appends a recovery record, preserves
the original failure, and refuses to overwrite any existing decision. The prompt
version must match the audited attempt; legacy attempts without version metadata
require the operator to identify their original version explicitly.
Valid model `include` proposals can feed an unapproved draft; they are never human
labels. Unknown provenance/status/relevance stays `needs_review`. This is a
development triage path, with semantic quality still requiring live evaluation.
The domestic rubric additionally requires `jurisdiction=vietnam`, direct Vietnam
relevance and a supporting passage marked `vietnam_relevance` for every include,
including human and researched-agent submissions. Prompt v4 requires an explicit
jurisdiction; international proposals become `geography_out_of_scope` exclusions.
Older rubric-1 inputs remain readable for historical previews, while new drafting,
approval, revision and delivery require reassessment under the current rubric.

`triage verify TRIAGE_RUN_ID --evidence assessments.json --expected-head HEAD`
records an agent's researched reassessment separately (`reviewer_type=agent`).
Each assessment links audited external HTML captures, SHA256 objects and exact
excerpts to the original triage input/head. A traced repost must link an article
on the attributed publisher's host with a shared passage in both snapshots;
publisher homepages do not qualify. Includes additionally require an event date
inside the window and publication/event/status evidence. Literal quote checks do
not verify the agent's interpretation, legal effect or implementation claims.
Agent verification cannot replace human decisions, create human labels or approve
an issue. Prior events and previews remain readable; new drafts pin the new head.
The local investigation captures are separate from the three-source listing scope
and do not expand collector coverage. Capture scripts do not follow redirects,
read embedded documents, render JavaScript or retry automatically.

For manual review or resolving proposals, edit a list of `TriageDecision` objects
using the schema, full source text and offset/quote candidates in the packet:

```bash
uv run python -m healthlab.signal triage status TRIAGE_RUN_ID
uv run python -m healthlab.signal triage submit TRIAGE_RUN_ID \
  --expected-head CURRENT_EVENT_SHA256_OR_none --decisions decisions.json \
  --reviewer 'Reviewer name' --rationale 'Explain the editorial decision'
```

Decisions record topic, freshness, reported status, source origin, Vietnam relevance,
event grouping, reason codes and exact source passages. Include requires publication
evidence in the monitoring window and concrete topic/development/status passages.
Reposts need source tracing; models cannot claim external verification. Hash checks,
quote offsets and status wording heuristics do not certify semantic correctness.

Drafting receives only `include` records; excluded, unresolved and unjudged documents
stay outside model input. An empty selection needs no provider/key and creates an
empty unapproved preview. Drafts pin the triage event: later changes block approval,
revision and sending of the old issue; its historical preview remains readable.
Required-source coverage gaps still block approval. Older untriaged issue previews
remain readable but require a new triaged issue before approval or delivery.

See the T1.3 runtime report (`docs/healthlab_signal_t13_triage_runtime_20261016.md`, local only).

PubMed-first evidence research pipeline for HealthLab. **Implemented CP6, CP8, CP10 and CP11:** search, fetch, normalized snapshots, per-run audit,
offline replay, FPT structured extraction, passage validation, extraction cache,
versioned draft briefs, citation/context guardrails and resumable orchestration. Semantic correctness evaluation (CP9)
and human review remain separate work;
CP7 retrieval benchmarking is not completed.

Target lineage for the full system:

```text
Claim → EvidenceItem → Passage → DocumentSnapshot → PubMed record
```

## Container and CI (T2 preparation)

Build the batch worker and exercise the installed package without network access:

```bash
docker build --tag healthlab-signal:local .
sh scripts/container_smoke.sh healthlab-signal:local
```

The image installs the locked runtime and cloud extras, runs as UID/GID 10001,
and defaults to CLI help. Its build context allows only source, package metadata
and the five reusable source/storage configurations. Credentials, reports, saved
runs, Git metadata and tests are excluded. The runtime contains the installed
package and source configuration; uv and development tools remain outside it.
The multi-stage, non-editable installation follows the
[uv Docker integration guide](https://docs.astral.sh/uv/guides/integration/docker/).

Use a named volume to retain the complete local object/run store across containers:

```bash
docker volume create healthlab-signal-data
docker run --rm --network none \
  --mount type=volume,src=healthlab-signal-data,dst=/data \
  healthlab-signal:local check-config
```

All Signal commands use `/data/signal` by default inside the image. For intentional
online collection/model calls, omit `--network none` and supply runtime credentials
with `--env-file .env`; do not put them in build arguments or the image. Approval
and sending remain separate CLI actions. A Docker volume here is local storage;
Cloud Run's temporary filesystem does not replace GCS/PostgreSQL persistence.

GitHub Actions runs locked lint/format and offline tests, the four real PostgreSQL
integration tests against a disposable service, and Docker build/smoke checks.
The smoke test disables networking, uses a read-only root filesystem, verifies the
packaged HTML rules and reads saved objects from a second container. The workflow
builds locally on the runner and does not publish images, provision resources,
call models or send email. Running the PostgreSQL job is distinct from cloud
storage acceptance. For equivalent local tests:

```bash
uv run --locked python scripts/ci_checks.py
# Disposable loopback PostgreSQL only:
uv run --locked python scripts/ci_checks.py --postgres
```

The latter requires `HEALTHLAB_TEST_POSTGRES_DSN`; it blocks external connections.
T1 human review/inbox acceptance and T2 full-workflow persistence are still pending.

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
Add `--refresh` to `run` to bypass extraction cache for a fresh-call baseline; this
policy is part of the baseline identity. Use a new output directory when changing it.
Fresh successful extractions still publish cache entries; use a separate store with
the frozen source objects/manifests copied in when existing cache must be preserved.
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

Extraction prompt `paper-extract-2` and validator `passage-sample-2` add conservative
sample-count checks after exact passage matching. New extractions reject explicitly
non-human counts, values not found as supported number spellings in the quote, and
roles lacking a non-negated cue in a sentence containing the number. Rejected raw
outputs remain auditable; no evidence/cache entry is published for them. Prompts ask
for explicit totals, arms and human expert panels without adding/subtracting counts.

These checks are heuristics, not semantic verification: complex clauses, entity
ambiguity and unfamiliar number spellings can need manual review. Supported number
forms include digits, comma-grouped digits and common English words below 1,000.
New cache identities use the new versions; existing artifacts retain their original
validator for offline rendering and must be re-extracted to receive the new checks.

## Telehealth abstract screening pilot

Option 1 keeps papers whose abstracts report any concrete barrier to telehealth adoption,
implementation or clinical delivery, even when barriers are not the primary objective.
This is relevance screening, separate from study-design eligibility and quality appraisal.

```bash
uv run python -m healthlab.screening --store outputs \
  --source-run SAVED_PUBMED_RUN_ID --out outputs/telehealth_screen_review
```

The input is a saved PubMed ingestion run (or an audited telehealth PMID probe); no new
search is made. The source XML/snapshots are verified before calls. A pilot accepts 1–30
saved documents, makes at most one FPT call per available abstract, and stops further
calls after auth/quota failure or two consecutive transport failures. Use a new output
directory; there is no automatic retry, resume or extraction-cache reuse.

Outputs propose `keep`, `maybe` or `not_relevant`, with barrier direction, Vietnamese
rationale and verbatim abstract passages. Specific barriers are distinguished from access
barriers overcome by telehealth; generic mentions without details remain uncertain.
The model selects IDs of supplied sentence-like abstract passages; code resolves them
to exact text and section offsets. Unknown IDs and inconsistent decisions fail validation.
Passage splitting is a deterministic heuristic, not a linguistic sentence parser. These checks
establish text identity and schema consistency, not semantic correctness. No paper is
automatically excluded, including `not_relevant` proposals; missing abstracts and failed
responses remain visible for review. Human labels stay null in the review packets and
are never inferred from AI suggestions. This command does not generate an evidence brief
or decide final systematic-review eligibility.
