import httpx
import json
import xml.etree.ElementTree as ET
from datetime import datetime

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# REPLACE WITH YOUR REAL EMAIL FOR NCBI COMPLIANCE
CONTACT_EMAIL = "your_email@gmail.com"
TOOL_NAME = "healthlab_evidence_probe"


def search_pubmed(query: str, retstart: int = 0, retmax: int = 5, sort: str = "pub_date", mindate: str = None, maxdate: str = None):
    """Step 1: ESearch - Search PubMed and return parameters and response."""
    url = f"{BASE_URL}/esearch.fcgi"
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retstart": retstart,
        "retmax": retmax,
        "sort": sort,
        "tool": TOOL_NAME,
        "email": CONTACT_EMAIL,
    }
    if mindate and maxdate:
        params["mindate"] = mindate
        params["maxdate"] = maxdate
        params["datetype"] = "pdat"

    timestamp = datetime.utcnow().isoformat()
    print(f"\n[{timestamp}] ESearch: sort={sort}, mindate={mindate}, maxdate={maxdate}")

    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return params, response.json()


def fetch_pubmed_records(pmids: list[str]):
    """Step 2: EFetch - Retrieve raw XML for the given PMIDs."""
    url = f"{BASE_URL}/efetch.fcgi"
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL_NAME,
        "email": CONTACT_EMAIL,
    }

    timestamp = datetime.utcnow().isoformat()
    print(f"[{timestamp}] EFetch: Fetching {len(pmids)} records: {pmids}")

    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return params, response.text


def parse_pubmed_xml(xml_content: str):
    """Step 3: Parse PubMed XML into structured Python dictionaries."""
    root = ET.fromstring(xml_content)
    articles = []

    for article_elem in root.findall(".//PubmedArticle"):
        # 1. Extract PMID
        pmid_elem = article_elem.find(".//MedlineCitation/PMID")
        pmid = pmid_elem.text.strip() if pmid_elem is not None and pmid_elem.text else None

        medline = article_elem.find(".//MedlineCitation/Article")
        if medline is None:
            continue

        # 2. Extract Title preserving inline tags
        title_elem = medline.find("ArticleTitle")
        title = "".join(title_elem.itertext()).strip() if title_elem is not None else None

        # 3. Extract Publication Date as given
        pub_date_elem = medline.find(".//Journal/JournalIssue/PubDate")
        pub_date = {}
        if pub_date_elem is not None:
            for child in pub_date_elem:
                if child.text:
                    pub_date[child.tag.lower()] = child.text.strip()

        # 4. Extract Abstract (capturing Label and NlmCategory)
        abstract_elem = medline.find("Abstract")
        abstract_sections = []
        has_abstract = False

        if abstract_elem is not None:
            for text_elem in abstract_elem.findall("AbstractText"):
                label = text_elem.attrib.get("Label", "").strip()
                category = text_elem.attrib.get("NlmCategory", "").strip()
                section_text = "".join(text_elem.itertext()).strip()

                if section_text:
                    abstract_sections.append({
                        "label": label if label else None,
                        "nlm_category": category if category else None,
                        "text": section_text
                    })

            # Check if abstract actually contains non-empty text
            has_abstract = len(abstract_sections) > 0

        articles.append({
            "pmid": pmid,
            "title": title,
            "pub_date": pub_date if pub_date else None,
            "has_abstract": has_abstract,
            "abstract_sections": abstract_sections,
        })

    return articles


if __name__ == "__main__":
    query = "progressive resistance training frailty older adults"
    print("=== CHECKPOINT 3 EXPERIMENTS: PUBMED PIPELINE ===")
    print(f"Target Query: '{query}'")

    # -------------------------------------------------------------
    # Experiment A: Sort by pub_date vs relevance
    # -------------------------------------------------------------
    print("\n--- EXPERIMENT A: Comparing Sort Strategies ---")
    search_params_date, search_data_date = search_pubmed(query, retmax=5, sort="pub_date")
    pmids_date = search_data_date["esearchresult"]["idlist"]
    count_total = search_data_date["esearchresult"]["count"]

    search_params_rel, search_data_rel = search_pubmed(query, retmax=5, sort="relevance")
    pmids_rel = search_data_rel["esearchresult"]["idlist"]

    print(f"Total Matches in PubMed: {count_total}")
    print(f"Top 5 (pub_date):  {pmids_date}")
    print(f"Top 5 (relevance): {pmids_rel}")
    overlap = set(pmids_date).intersection(set(pmids_rel))
    print(f"Overlap between newest and most relevant: {len(overlap)} / 5 items")

    # -------------------------------------------------------------
    # Experiment B: Date Filtering (Last 5 years: 2021/09/16 -> 2026/09/16)
    # -------------------------------------------------------------
    print("\n--- EXPERIMENT B: Date Filtering ---")
    search_params_filtered, search_data_filtered = search_pubmed(
        query, retmax=5, sort="pub_date", mindate="2021/09/16", maxdate="2026/09/16"
    )
    count_filtered = search_data_filtered["esearchresult"]["count"]
    print(f"Total count (All time): {count_total}")
    print(f"Total count (2021-2026): {count_filtered} (Filtered out {int(count_total) - int(count_filtered)} records outside the selected date range)")


    # -------------------------------------------------------------
    # Experiment C: Fetching + Edge Case Testing (Includes missing abstract)
    # -------------------------------------------------------------
    print("\n--- EXPERIMENT C: EFetch & Parsing with Edge Case ---")
    # We take top 4 from relevance + 1 known editorial/comment without abstract (PMID: 21535087)
    test_pmids = pmids_rel[:4] + ["21535087"]

    fetch_params, xml_content = fetch_pubmed_records(test_pmids)

    # 1. Save raw XML
    xml_path = "experiments/pubmed_efetch_sample.xml"
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml_content)
    print(f"✅ Saved raw XML to: {xml_path}")

    # 2. Parse all articles
    parsed_articles = parse_pubmed_xml(xml_content)
    print(f"✅ Parsed {len(parsed_articles)} articles.")

    # 3. Save consolidated JSON artifact (preserving Data Lineage)
    lineage_artifact = {
        "timestamp": datetime.utcnow().isoformat(),
        "query": query,
        "experiment_a_sort_pub_date": {
            "params": search_params_date,
            "count": search_data_date["esearchresult"]["count"],
            "query_translation": search_data_date["esearchresult"].get("querytranslation"),
            "pmids": pmids_date,
            "raw_response": search_data_date,
        },
        "experiment_a_sort_relevance": {
            "params": search_params_rel,
            "count": search_data_rel["esearchresult"]["count"],
            "query_translation": search_data_rel["esearchresult"].get("querytranslation"),
            "pmids": pmids_rel,
            "raw_response": search_data_rel,
        },
        "experiment_b_date_filter": {
            "params": search_params_filtered,
            "count": search_data_filtered["esearchresult"]["count"],
            "query_translation": search_data_filtered["esearchresult"].get("querytranslation"),
            "pmids": search_data_filtered["esearchresult"]["idlist"],
            "raw_response": search_data_filtered,
        },
        "experiment_c_fetch": {
            "search_candidates_pmids": pmids_rel[:4],
            "manual_test_pmids": ["21535087"],  # Edge case: article without abstract
            "total_requested": test_pmids,
            "fetch_params": fetch_params,
        },
        "parsed_articles": parsed_articles,
    }


    json_path = "experiments/pubmed_pipeline_sample.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(lineage_artifact, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved consolidated pipeline artifact to: {json_path}")

    # -------------------------------------------------------------
    # Detailed Inspection: Full Print (No Truncation)
    # -------------------------------------------------------------
    print("\n" + "=" * 60)
    print("=== MANUAL VERIFICATION: ARTICLE WITH STRUCTURED ABSTRACT ===")
    print("=" * 60)
    first = parsed_articles[0]
    print(f"PMID: {first['pmid']}")
    print(f"Title: {first['title']}")
    print(f"Pub Date: {first['pub_date']}")
    print(f"Has Abstract: {first['has_abstract']}")
    for idx, sec in enumerate(first['abstract_sections'], 1):
        print(f"\n--- Section [{idx}] (Label: {sec['label']} | Category: {sec['nlm_category']}) ---")
        print(sec['text'])  # Print FULL text without truncation!

    print("\n" + "=" * 60)
    print("=== EDGE CASE VERIFICATION: ARTICLE WITHOUT ABSTRACT ===")
    print("=" * 60)
        # Find the specific test article by PMID and assert expectations
    missing_ab = next((a for a in parsed_articles if a["pmid"] == "21535087"), None)
    assert missing_ab is not None, "Test article 21535087 was not found in parsed articles!"
    assert missing_ab["has_abstract"] is False, f"Expected False, got {missing_ab['has_abstract']}"
    assert len(missing_ab["abstract_sections"]) == 0, f"Expected 0 sections, got {len(missing_ab['abstract_sections'])}"
    print("✅ ASSERTION PASSED: Edge case (missing abstract) strictly verified!")

    print(f"PMID: {missing_ab['pmid']}")
    print(f"Title: {missing_ab['title']}")
    print(f"Has Abstract: {missing_ab['has_abstract']} (Expected: False)")
    print(f"Abstract Sections Count: {len(missing_ab['abstract_sections'])} (Expected: 0)")
