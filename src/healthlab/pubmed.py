"""NCBI transport and pure parsers. No FPT calls or persistence here."""

import json
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus

import httpx

from healthlab.config import Settings
from healthlab.models import AbstractSection, Document, IngestionError

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def utc_now():
    return datetime.now(UTC).isoformat()


def parse_search(raw: bytes) -> dict:
    try:
        payload = json.loads(raw)
        if payload.get("error") or payload.get("esearchresult", {}).get("ERROR"):
            raise IngestionError("source_error", "ESearch returned an API error")
        result = payload["esearchresult"]
        if result.get("errorlist"):
            raise IngestionError(
                "query_error", "ESearch rejected query terms; inspect raw response"
            )
        count = int(result["count"])
        ids = result["idlist"]
        if (
            count < 0
            or not isinstance(ids, list)
            or any(not isinstance(p, str) or not p.isdigit() for p in ids)
        ):
            raise ValueError("invalid search fields")
        if (count == 0 and ids) or (count > 0 and not ids):
            raise ValueError("inconsistent count and ids for first page")
        if len(ids) > 5 or len(ids) > count:
            raise ValueError("invalid first-page size")
        selected = list(dict.fromkeys(ids))
        return {
            "count": count,
            "ordered_pmids": ids,
            "selected_pmids": selected,
            "duplicate_ids": len(ids) - len(selected),
            "query_translation": result.get("querytranslation"),
            "warnings": result.get("warninglist", {}),
        }
    except IngestionError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise IngestionError("invalid_search_response", "Malformed ESearch JSON/contract") from exc


def text_of(element):
    if element is None:
        return None
    return "".join(element.itertext()).strip() or None


def parse_records(raw: bytes) -> tuple[list[Document], dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise IngestionError("invalid_xml", "EFetch XML could not be parsed") from exc
    if root.tag.upper() == "ERROR" or root.find(".//ERROR") is not None:
        raise IngestionError("source_error", "EFetch returned an API error")
    if root.tag != "PubmedArticleSet":
        raise IngestionError("invalid_xml", "Expected PubmedArticleSet root")
    by_id = {}
    raw_by_id = {}
    duplicate_count = 0
    unsupported = []
    for item in root:
        if item.tag != "PubmedArticle":
            unsupported.append(item.tag)
            continue
        pmid = text_of(item.find("./MedlineCitation/PMID"))
        article = item.find("./MedlineCitation/Article")
        if not pmid or not pmid.isdigit() or article is None:
            raise IngestionError("invalid_record", "PubmedArticle lacks valid PMID or Article")
        item.tail = None
        canonical_record = ET.canonicalize(ET.tostring(item, encoding="unicode"))
        sections = [
            AbstractSection(
                label=node.get("Label") or None,
                nlm_category=node.get("NlmCategory") or None,
                text=text_of(node),
            )
            for node in article.findall("./Abstract/AbstractText")
            if text_of(node)
        ]
        pub_date = article.find("./Journal/JournalIssue/PubDate")
        dates = (
            {node.tag: text_of(node) for node in pub_date if text_of(node)}
            if pub_date is not None
            else {}
        )
        authors = []
        for author in article.findall("./AuthorList/Author"):
            name = text_of(author.find("CollectiveName"))
            if not name:
                name = " ".join(
                    filter(
                        None,
                        [
                            text_of(author.find("ForeName")) or text_of(author.find("Initials")),
                            text_of(author.find("LastName")),
                        ],
                    )
                )
            if name:
                authors.append(name)
        doi = text_of(item.find("./PubmedData/ArticleIdList/ArticleId[@IdType='doi']"))
        if doi is None:
            doi = text_of(article.find("ELocationID[@EIdType='doi']"))
        document = Document(
            document_id=f"pubmed:{pmid}",
            pmid=pmid,
            title=text_of(article.find("ArticleTitle")),
            pub_date=dates or None,
            journal=text_of(article.find("./Journal/Title")),
            authors=authors,
            doi=doi,
            publication_types=[
                text_of(n)
                for n in article.findall("./PublicationTypeList/PublicationType")
                if text_of(n)
            ],
            abstract_sections=sections,
            has_abstract=bool(sections),
            extraction_eligibility="eligible" if sections else "skipped",
            skip_reason=None if sections else "missing_abstract",
        )
        # Different revisions in one batch must not silently overwrite each other.
        if pmid in by_id:
            if by_id[pmid] != document or raw_by_id[pmid] != canonical_record:
                raise IngestionError(
                    "conflicting_duplicate", f"Conflicting records for PMID {pmid}"
                )
            duplicate_count += 1
        by_id[pmid] = document
        raw_by_id[pmid] = canonical_record
    return list(by_id.values()), {
        "duplicate_records": duplicate_count,
        "unsupported_tags": unsupported,
    }


class PubMedClient:
    """Sequential CLI limiter; not a shared cross-process/IP rate coordinator."""

    def __init__(self, settings: Settings, *, client=None, sleep=time.sleep, clock=time.monotonic):
        settings.validate_online()
        self.settings = settings
        self.client = client or httpx.Client(timeout=settings.timeout)
        self.owns_client = client is None
        self.sleep, self.clock = sleep, clock
        self.last_start = None

    def close(self):
        if self.owns_client:
            self.client.close()

    def request(self, endpoint: str, params: dict, audit) -> bytes:
        public = {**params, "tool": self.settings.tool, "email": self.settings.email}
        wire = dict(public)
        api_key = self.settings.api_key.get_secret_value()
        if api_key:
            wire["api_key"] = api_key
        interval = 0.11 if api_key else 0.35
        for attempt in range(1, self.settings.max_attempts + 1):
            if self.last_start is not None:
                self.sleep(max(0, interval - (self.clock() - self.last_start)))
            self.last_start = self.clock()
            started = utc_now()
            status, body, retry_after, error = None, None, None, None
            try:
                response = self.client.get(f"{BASE_URL}/{endpoint}", params=wire)
                status, body = response.status_code, response.content
                retry_after = response.headers.get("Retry-After")
            except httpx.TimeoutException:
                error = "timeout"
            except httpx.TransportError:
                error = "transport_error"
            redacted = False
            if body is not None and api_key:
                for secret in {
                    api_key,
                    quote(api_key, safe=""),
                    quote_plus(api_key),
                }:
                    if secret.encode() in body:
                        body = body.replace(secret.encode(), b"[REDACTED_API_KEY]")
                        redacted = True
            audit(
                {
                    "endpoint": endpoint,
                    "params": public,
                    "attempt": attempt,
                    "started_at": started,
                    "finished_at": utc_now(),
                    "http_status": status,
                    "error_code": error,
                    "body_redacted": redacted,
                },
                body,
            )
            if status == 200:
                return body
            transient = error is not None or status in {429, 500, 502, 503, 504}
            if not transient or attempt == self.settings.max_attempts:
                code = error or ("rate_limited" if status == 429 else "http_error")
                raise IngestionError(
                    code,
                    f"NCBI request failed ({code}, status={status}, attempts={attempt})",
                )
            delay = 2 ** (attempt - 1)
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(
                            delay,
                            (
                                parsedate_to_datetime(retry_after) - datetime.now(UTC)
                            ).total_seconds(),
                        )
                    except (ValueError, TypeError, OverflowError):
                        pass
            if delay > 60:
                raise IngestionError(
                    "retry_deferred",
                    "NCBI requests a wait over 60 seconds; retry later",
                )
            self.sleep(delay)
        raise AssertionError("max_attempts must be positive")
