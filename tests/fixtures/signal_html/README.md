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
