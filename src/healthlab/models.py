"""Small persisted contracts. Query context belongs to a run, never a document."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSER_VERSION = "pubmed-1"
MANIFEST_VERSION = 1


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Question(Contract):
    raw_question: str = Field(min_length=1)
    scope: str | None = None
    query: str = Field(min_length=1)
    sort: Literal["pub_date", "relevance"] = "relevance"
    mindate: date | None = None
    maxdate: date | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.query.strip() or not self.raw_question.strip():
            raise ValueError("question and query must contain text")
        if (self.mindate is None) != (self.maxdate is None):
            raise ValueError("mindate and maxdate must be supplied together")
        if self.mindate and self.mindate > self.maxdate:
            raise ValueError("mindate must not be later than maxdate")
        return self

    def search_params(self) -> dict:
        params = {
            "db": "pubmed",
            "term": self.query,
            "retmode": "json",
            "retstart": 0,
            "retmax": 5,
            "sort": self.sort,
        }
        if self.mindate:
            params.update(
                mindate=self.mindate.strftime("%Y/%m/%d"),
                maxdate=self.maxdate.strftime("%Y/%m/%d"),
                datetype="pdat",
            )
        return params


class AbstractSection(Contract):
    label: str | None = None
    nlm_category: str | None = None
    text: str


class Document(Contract):
    document_id: str
    source: Literal["pubmed"] = "pubmed"
    pmid: str
    title: str | None
    pub_date: dict[str, str] | None
    journal: str | None
    authors: list[str]
    doi: str | None
    publication_types: list[str]
    abstract_sections: list[AbstractSection]
    has_abstract: bool
    extraction_eligibility: Literal["eligible", "skipped"]
    skip_reason: Literal["missing_abstract"] | None


class IngestionError(Exception):
    """Safe, stable errors: never wrap a request URL containing credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConfigurationError(IngestionError, ValueError):
    """Actionable configuration failure with a safe message, never field values."""
