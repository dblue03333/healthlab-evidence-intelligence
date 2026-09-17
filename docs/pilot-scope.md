# HealthLab Evidence Intelligence — Pilot Scope (v0.1.0)

> **Status:** Draft — awaiting core team review
> **Author:** Kelvin Nguyen
> **Date:** 2026-09-10
> **Deadline:** 2026-09-13 (v0.1.0)

---

## 1. Who is this pilot for?

**Primary users:** HealthLab core team (Research, Mar-Com) who need to find and
summarize academic evidence for health-related questions.

**What they do after reading a brief:**

- **Research/Core team:** Verify claims, decide whether evidence supports a
  product position, feed into internal knowledge base.
- **Mar-Com:** Use reviewed briefs as source material for content creation
  (articles, social posts, marketing claims).

**Key workflow assumption:** Users start with a question or keyword — they do
NOT upload a pre-collected set of papers (Assumption A1, confirmed).

---

## 2. Supported input

Each request consists of:

| Field | Required? | Description |
| :--- | :--- | :--- |
| `question_or_keyword` | **Yes** | The raw question or keyword exactly as the user would type it |
| `purpose` | Optional | Why they need this (e.g., "internal research", "claim verification", "content brief") |
| `population` | Optional | Target population if known (e.g., "older adults", "women 40–60") |
| `geography` | Optional | Geographic scope if relevant (e.g., "Vietnam", "Southeast Asia") |
| `publication_date_range` | Optional | Desired recency (e.g., "last 5 years") |

If the user provides only a broad keyword, the system must display the
interpreted scope and search parameters — never silently narrow it into a
specific clinical question.

### Example questions (from team data template)

1. **"Resistance training có giúp giảm frailty ở người lớn tuổi không?"**
   - Topic: Healthy Ageing
   - Purpose: Internal research
   - Expected output: Evidence brief

2. **"WHO gần đây có recommendation gì về healthy ageing?"**
   - Topic: Healthy Ageing
   - Purpose: Claim verification
   - Note: Needs freshness; but WHO source is out of v0.1.0 scope — system
     should state this limitation clearly, not pretend PubMed covers it.

3. **"Aging population Vietnam workforce"**
   - Topic: Aging Society
   - Purpose: Content / Mar-Com
   - Note: Messy keyword — still valuable as-is. System should not rewrite it.

---

## 3. Output: Evidence Brief

A generated brief contains the following sections:

```
1. Original question and interpreted scope
2. Search date and parameters used
3. Short answer (1–3 sentences)
4. Findings with inline citations
5. Evidence table
   - Paper (PMID, title, authors, year)
   - Study type
   - Population / sample
   - Key finding
   - Supporting passage (exact text from abstract)
6. Gaps and conflicting findings
7. Scope limitations (PubMed-only, abstract-only)
8. Links back to supporting passages and source documents
9. Review status (AI-generated / Reviewed by [name] on [date])
```

### Critical output rules

- Every factual claim must reference a specific paper and passage.
- No fabricated PMIDs, DOIs, or URLs.
- Association ≠ causation — language must reflect this.
- "No evidence found" ≠ "No effect" — the brief must distinguish these.
- Population and conditions must not be silently dropped from findings.
- An unreviewed brief must never be labeled as "HealthLab reviewed."

---

## 4. Scope: PubMed-first

**What v0.1.0 covers:**

- Search PubMed via E-utilities (esearch + efetch).
- Retrieve metadata + abstracts only.
- Use FPT AI model for structured extraction from abstracts.
- Generate evidence brief with traceable claims.
- Support offline replay from cached data.

**The traceability chain that must work end-to-end:**

```
Claim → EvidenceItem → Passage → DocumentSnapshot → PubMed record
```

If any link in this chain is broken, the brief must expose it — not hide it.

---

## 5. What is NOT supported in v0.1.0

- No WHO document search or integration.
- No full-text paper reading (abstract-only).
- Only PubMed metadata + abstract — not a comprehensive literature review.
- No guarantee of finding all existing literature.
- No clinical decision-making support.
- AI-generated output is not considered HealthLab-reviewed.
- No arbitrary PDF upload.
- No multi-user authentication.
- No automatic fallback to OpenRouter.

---

## 6. Review workflow

| Stage | Label | Meaning |
| :--- | :--- | :--- |
| System generates brief | `AI-generated` | Not yet reviewed by any human |
| Core team reviews | `In review` | Under human inspection |
| Core team accepts/edits | `Reviewed by [name], [date]` | Approved for downstream use |
| System re-generates | Reverts to `AI-generated` | Previous review no longer applies |

**Rule:** Regenerating a brief always resets review status. Edits made by a
reviewer are saved separately and never silently overwritten.

---

## 7. Assumptions (confirmed)

| ID | Assumption | Status |
| :--- | :--- | :--- |
| A1 | Users start with a question/keyword, not a paper collection | ✅ Confirmed |
| A2 | Abstract-level evidence is useful for the first research round | ✅ Good starting point |
| A3 | Core team does human review before external use | ✅ Confirmed |
| A4 | A brief with 5–10 papers is more useful than 50 raw results | ✅ Good starting point |
| A5 | Reviewers want to click from finding → supporting passage | ✅ Good starting point |
| A6 | FPT model is good enough for structured extraction | ⚠️ Maybe — needs validation at Checkpoint 2 |

---

## 8. Open questions for core team

1. **Who is the designated reviewer for pilot briefs?** Need a name and
   availability commitment for the 3-day window.
2. **Do we have 3–5 real questions from the team?** The Excel template (Q001–Q050)
   is still empty. We need at least 3 filled rows to test against.
3. **FPT API quota:** How many calls can we make per day? Is there a hard limit
   that would block the pilot?
4. **Vietnamese vs. English output:** Should the brief be generated in English
   (matching PubMed sources) or Vietnamese? If Vietnamese, is translation in
   scope for v0.1.0 or handled manually by Mar-Com?
5. **What does "good enough" look like?** For the pilot, what error rate in
   extraction is acceptable before core team would say "this isn't useful yet"?

---

## 9. Sample brief (hand-drafted)

> **Question:** Resistance training có giúp giảm frailty ở người lớn tuổi không?
>
> **Scope:** PubMed, abstracts only, no date filter, English-language articles.
> Search date: 2026-09-11.
>
> **Short answer:** Multiple systematic reviews and RCTs suggest that resistance
> training programs reduce frailty markers in older adults, though effect sizes
> vary by population and program duration.
>
> **Finding 1:** A 12-week progressive resistance training program significantly
> improved grip strength and gait speed in community-dwelling adults aged 65+
> (n=120). *[PMID: XXXXXXX, Smith et al. 2023]*
>
> **Supporting passage:** "Participants in the intervention group showed a
> significant improvement in grip strength (p<0.01) and gait speed (p<0.05)
> compared to control." — *Abstract, Results section*
>
> **Gaps:** No studies found specifically for Vietnamese population. Most
> evidence from Western cohorts.
>
> **Limitations:** This brief covers PubMed abstracts only. Full-text analysis,
> WHO guidelines, and grey literature were not searched.
>
> **Review status:** AI-generated — not yet reviewed by HealthLab core team.

*(Note: PMIDs above are placeholders. In v0.1.0, all PMIDs will be real and
verifiable.)*
