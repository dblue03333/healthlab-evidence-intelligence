# Immutable data-contract fixture

`dataset_v1.json.gz` contains a complete synthetic version-1 Medallion object
closure and its fixed dataset SHA-256. The test reconstructs it in a clean store
and requires the current reader to accept the original ID and membership.

This guards against changing old release meanings through new Source defaults,
normalization changes or schema evolution. Add a new versioned fixture/reader
when meanings change; do not regenerate this fixture to hide a compatibility
failure. Its HTML is authored test text, not health evidence; no network or
credentials were used to generate it.
