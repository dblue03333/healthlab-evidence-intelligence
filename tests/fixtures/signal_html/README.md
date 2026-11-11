# Captured official HTML fixtures

Fourteen gzip-compressed, unmodified public HTML/JSON responses from the 10 October
bootstrap, 16 October link audit and 17 October 2026 discovery run. `provenance.json` records URLs,
acquisition context and SHA-256 of the uncompressed response. Gzip timestamps
are fixed to zero. No credentials or authenticated browser sessions were used.

These capture real DOM structures that synthetic fixtures missed: HCMC's
separate introduction and update header, population-department navigation,
infographics, embedded PDFs/videos, broad headlines and WHO metadata.

Tests read these offline. Expected labels are structural assertions only;
these files do not certify the articles' factual claims, editorial relevance,
current website state or comprehensive monitoring coverage.

The 17 October fixtures also cover JavaScript relay pagination, actual HTML
archive/pager links, same-host HCMC HTTPS normalization and publication timestamps
inside WHO Vietnam news anchors. Full source URLs, collection IDs and raw hashes
are recorded in provenance.json.

The public population-department posts API fixture preserves 22 publication-window
records with its observed count/page headers. It tests discovery completeness
within that API contract, not coverage of all website content types.

The 21 October correction adds unmodified Ministry portal POST inventory/article
JSON and six WHO listing captures, with request bodies and hashes in provenance.
The legacy population registry is frozen separately for the old fixtures; it is
not the default Ministry source. Portal tests cover migrated null language values,
child categories, article/date identity and raw JSON locators. WHO replay checks
that only the adjacent release page is visited and the duplicate alias is avoided.
## Data pipeline regressions — 26 October 2026

`hcm_sidebar_listing_20261026.html.gz` captures a dated primary news list and an
undated promoted article outside that list. `hcm_updated_only_20261026.html.gz`
captures the promoted article, whose visible timestamp is explicitly an update.
The regression requires bounded chronological traversal while retaining that
article as `needs_review`; it must not infer a publication date from the update.
URLs, capture times and raw hashes are recorded in `provenance.json`.
# Coverage audit captures, 27 October 2026

`hcm_coverage_listing_20261027.html.gz` and `hcm_coverage_page2_20261027.html.gz`
are bounded public category captures. The first includes article 76294, dated
7 October, which is absent from the saved 26 October primary listing. The
regression pairs both versions to verify candidate memory survives a stale
listing. Provenance hashes are in `provenance.json`; these fixtures do not certify
complete archive coverage or editorial truth.

## Ageing widget — 28 October 2026

The landing and JSON widget fixtures preserve the declared top-three news component.
Tests check its three otherwise hidden links, query pinning, dates, cached lineage
and visible failures for changed payloads. They do not establish historical widget
membership. Images/video are never fetched by these tests or the collector.

## HCMC public search — 28 October 2026

Seven unmodified public WebForms result captures preserve the bounded blank-key
search of100 records. Tests enforce c1780 filtering, chronological date boundaries,
visible capped-history/DOM gaps and bounded interrupted-session recovery. Form
state is public rendering state, never an authorization to call other forms.
The search inventory is a same-publisher corroboration, not all-website recall.
