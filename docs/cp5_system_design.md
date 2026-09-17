# System Design: Core Ingestion & Extraction Pipeline (CP5 Minimal Flow)

**Goal:** Establish an end-to-end traceable pipeline connecting Question Formulation, PubMed Retrieval, and FPT AI Evidence Extraction.

**Core Traceability Goal (Full Pipeline):**  
`Claim → EvidenceItem → Supporting Passage (Verbatim) → DocumentSnapshot → PubMed Record`

> CP5–CP6 implements the right half of this chain (DocumentSnapshot → PubMed Record, plus per-paper extraction). Claims, briefs, and synthesis are deferred to later checkpoints.

---

## 1. High-Level Pipeline Architecture

```text
1. Prepare Question & Search Scope
               │
               ▼
2. Retrieve PubMed Documents (ESearch → EFetch → Parse)
               │
               ▼
3. Extract Evidence with FPT (Extract → Validate → Grounding Verification)
               │
               ▼
Structured Evidence with Verbatim Passage & Full Data Lineage
```

---

## 2. Detailed Step Specifications

### Step 1: Prepare Question and Search Scope
* **Name:** Question Normalization & Scope Definition
* **Input:** Raw user question or keywords (optional filters: date range, study type, target population).
* **Operations:**
  - Preserve the user's raw question verbatim to prevent semantic drift.
  - Explicitly define the research scope and construct the canonical query string for PubMed ESearch.
* **Output:**
  - Canonical `Question` object: raw question + interpreted scope + PubMed search query + applied filters.
* **Persistence (Store):**
  - Store original question, scope definition, generated search query, and parameter filters.
* **Architectural Rationale:**
  - Prevents query distortion: ensures clear demarcation between what the user originally intended and how the system translated it for execution.

---

### Step 2: Retrieve PubMed Documents
* **Name:** PubMed Candidate Search, Ingestion & Parsing
* **Input:** Search query + filter parameters + sorting strategy (`pub_date` or `relevance`).
* **Operations:**
  - **ESearch:** Query NCBI Entrez API to obtain total matching count, `QueryTranslation`, and ordered candidate PMIDs.
  - **Selection:** Select top-K candidate PMIDs according to retrieval policy.
  - **EFetch:** Fetch batch raw XML records from NCBI Entrez for selected PMIDs.
  - **XML Parsing:** Parse XML records with `xml.etree.ElementTree` into structured documents (`PMID`, `Title`, `PubDate`, structured `AbstractSections`). Handle edge cases (e.g., articles lacking abstracts).
* **Output:**
  - Parsed document records linked directly to raw XML snapshots.
* **Persistence (Store):**
  - Search run metadata: parameters, API response, `QueryTranslation`, total match count, ordered candidate PMIDs.
  - Actual selected PMIDs and selection justification.
  - Raw XML payload and parsed document snapshots (`DocumentSnapshot`).
* **Architectural Rationale:**
  - **Auditability:** Retain exact reasons why specific documents were selected over others.
  - **Offline Replay:** Allows re-parsing XML data offline without making redundant network calls.
  - **Source Provenance:** Allows verifying parsed text directly against raw upstream XML data.

---

### Step 3: Extract Evidence with FPT
* **Name:** LLM Evidence Extraction & Grounding Verification
* **Input:** A `DocumentSnapshot` containing a verified non-empty abstract (`has_abstract == True`).
* **Operations:**
  - Dispatch abstract to FPT AI (`gpt-oss-120b`) with a prompt that requests structured JSON conforming to the extraction schema. Native schema enforcement by the API has not been verified; application code must validate required fields, types, and allowed values.
  - Clean markdown fences and deserialize JSON payload (`json.loads`).
  - Multi-tier Validation:
    1. *HTTP / Transport Layer:* HTTP status 200, inspect `finish_reason`.
    2. *Schema Layer:* Validate required keys, explicitly distinguish `sample_size: null` from missing keys.
    3. *Grounding Layer:* Perform verbatim substring check (`passage in abstract`) to ensure `supporting_passage` is not hallucinated or altered.
* **Output:**
  - Structured evidence fields: `study_type`, `sample_size`, `population`, `main_finding`.
  - Verbatim `supporting_passage`.
  - Validation audit report with **separate verdicts** (not a single pass/fail):
    - `schema_valid`: All required keys present with correct types.
    - `passage_match`: Quoted passage exists verbatim in the source snapshot.
    - `semantic_support`: `not_evaluated` (requires separate semantic or human review; not automated in CP6).
* **Persistence (Store):**
  - Source `DocumentSnapshot` used as input.
  - Prompt/schema version, model identifier, and runtime parameters (`temperature=0.0`).
  - Raw model response, parsed output, and validation audit logs.
* **Architectural Rationale:**
  - **Traceability:** Links each evidence item to a source passage and verifies that the quoted text exists in the snapshot. Whether the passage semantically supports the finding requires separate evaluation (human review or semantic analysis).
  - **Isolation & Debuggability:** Allows prompt tuning and extraction evaluation without re-triggering upstream search and fetch steps.
  - **Extraction Failure Handling:** If JSON parsing, schema validation, or passage matching fails, the system persists the raw model response alongside the error details. The output is **not** promoted to validated evidence. The source `DocumentSnapshot` and all fetched data are retained so extraction can be retried with corrected prompts or configuration without re-fetching from PubMed.

---

## 3. Minimal Design Decisions & Boundaries (Baseline for CP6)

| # | Agreed Decision | Rationale & Operational Boundary |
|---|---|---|
| **D1** | **Strict Step Ordering** | `Question/Scope` → `PubMed Ingestion` → `FPT Extraction`. No circular dependencies or premature multi-provider orchestration. |
| **D2** | **Full Audit Persistence** | Persist both raw upstream payloads (`raw_search.json`, `raw_fetch.xml`) and parsed models in isolated run directories (`outputs/runs/<run_id>/`). |
| **D3** | **Missing Abstract Handling** | Records without abstracts are valid records and must be saved. Their extraction eligibility is marked as `skipped` (not as an unhandled error or "no evidence"). |
| **D4** | **Fixed Top-5 Selection (v0.1.0)** | Search returns candidate pool; take top 5 directly. Process those with valid abstracts. Do **not** implement speculative backfill/auto-pagination in this milestone. |
| **D5** | **Paper-Level Extraction First** | Extraction focuses strictly on individual `DocumentSnapshot` evidence. Multi-paper question synthesis is deferred to subsequent checkpoints. |

---

## 4. Open Questions (Defer to CP8)

| # | Question | Context | Impact |
|---|---|---|---|
| **Q1** | `sample_size` refers to recruited, randomized, or completed participants? | CP3 abstract (PMID `39024000`) reported 96 recruited, 60 randomized, 55 completed. A single integer field is ambiguous. | Extraction schema must either pick one definition or capture multiple values with roles. Does not block CP6 (ingestion only). |
