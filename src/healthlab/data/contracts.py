"""Medallion contracts. A ready dataset certifies its captured scope only."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from healthlab.models import Contract

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Outcome = Literal[
    "included", "outside_window", "needs_review", "failed", "excluded_media", "deferred"
]


class Window(Contract):
    mindate: date
    maxdate: date
    basis: Literal["publisher_publication_calendar_date", "configured_source_calendar_date"] = (
        "publisher_publication_calendar_date"
    )
    inclusive: Literal[True] = True

    @model_validator(mode="after")
    def ordered(self):
        if self.mindate > self.maxdate:
            raise ValueError("Invalid publication window")
        return self


class Observation(Contract):
    document_id: str
    source_id: str
    url: str
    outcome: Outcome
    title: str | None = None
    body: str | None = None
    publication_date: date | None = None
    modified_date_values: list[str] = Field(default_factory=list)
    publication_date_evidence: list[dict] = Field(default_factory=list)
    passages: list[dict] = Field(default_factory=list)
    raw_sha256: Digest | None = None
    extraction_sha256: Digest | None = None
    snapshot_id: Digest | None = None
    content_version: Digest | None = None
    error_code: str | None = None
    content_coverage: str | None = None
    recovered_from_history: bool = False
    previous_snapshot_id: Digest | None = None


class NewsletterObservation(Observation):
    selection_date: date | None
    selection_date_basis: Literal["publication", "listing_displayed", "article_modified"]
    selection_date_evidence: list[dict]


class SourceQuality(Contract):
    source_id: str
    organization: str
    url: str
    scope: str
    required: bool
    status: str
    coverage_complete: bool
    website_coverage_complete: Literal[False] = False
    outcomes: dict[str, int]
    candidates: int
    recovered_from_history: int
    listing_pages: int
    http_attempts: int
    response_bytes: int
    issues: list[dict]


class Bronze(Contract):
    schema_version: Literal["healthlab-bronze-1"] = "healthlab-bronze-1"
    collection_manifest_sha256: Digest
    collection_id: str
    capture_kind: Literal["live", "replay", "repair"]
    window: Window
    registry: dict
    collector_version: str
    parser_version: str


class Silver(Contract):
    schema_version: Literal["healthlab-silver-1", "healthlab-silver-2"] = "healthlab-silver-1"
    bronze_sha256: Digest
    records: list[NewsletterObservation | Observation]
    sources: list[SourceQuality]


class Gold(Contract):
    schema_version: Literal["healthlab-gold-1"] = "healthlab-gold-1"
    silver_sha256: Digest
    window: Window
    status: Literal["ready", "partial"]
    document_ids: list[str]
    semantic_review: Literal["not_evaluated"] = "not_evaluated"
    coverage_basis: Literal["configured_captured_inventory"] = "configured_captured_inventory"
    independent_recall: None = None


class Dataset(Contract):
    schema_version: Literal["healthlab-dataset-1"] = "healthlab-dataset-1"
    bronze_sha256: Digest
    silver_sha256: Digest
    gold_sha256: Digest
