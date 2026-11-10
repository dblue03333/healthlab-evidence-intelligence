"""Build the 2026-10-27 audit reference from saved public responses, offline.

This intentionally does not import collector discovery, date or DOM parsers.
It is an inspectable audit extractor, not another production connector.
"""

import argparse
import hashlib
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin


class Tree(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = ["root", {}, [], None]
        self.current = self.root
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = [tag, dict(attrs), [], self.current]
        self.current[2].append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node[3] is not None:
            if node[0] == tag:
                self.current = node[3]
                return
            node = node[3]

    def handle_data(self, value):
        self.current[2].append(value)


def nodes(root):
    yield root
    for child in root[2]:
        if isinstance(child, list):
            yield from nodes(child)


def text(root):
    return " ".join(c if isinstance(c, str) else text(c) for c in root[2]).strip()


def cards(raw, base, hcm=False):
    tree = Tree(raw.decode()).root
    rows = {}
    for node in nodes(tree):
        if ("time" if hcm else "timestamp") not in node[1].get("class", "").split():
            continue
        try:
            day = (
                datetime.strptime(text(node), "%d/%m/%Y" if hcm else "%d %B %Y").date().isoformat()
            )
        except ValueError:
            continue
        parent = node[3]
        for _ in range(6):
            if parent is None:
                break
            links = {}
            foreign_article = False
            for a in nodes(parent):
                href = a[1].get("href", "") if a[0] == "a" else ""
                if (
                    hcm
                    and re.search(r"-c\d+-\d+\.aspx$", href)
                    and not re.search(r"-c1780-\d+\.aspx$", href)
                ):
                    foreign_article = True
                if hcm and re.search(r"-c1780-(\d+)\.aspx$", href):
                    links[re.search(r"-(\d+)\.aspx$", href)[1]] = urljoin(base, href)
                elif not hcm and re.match(
                    r"/(news/item/|vietnam/news/(detail/|feature-stories/item/|commentaries/detail/|speeches/speech/))",
                    href,
                ):
                    links[href] = urljoin(base, href)
            if foreign_article:
                break
            if len(links) == 1:
                url = next(iter(links.values()))
                rows[(url, day)] = {
                    "url": url,
                    "date": day,
                    "locator": f"visible timestamp {text(node)!r} in unique-article container",
                }
                break
            if len(links) > 1:
                break
            parent = parent[3]
    return list(rows.values())


def build(directory):
    def evidence(name, records):
        raw = (directory / (name + ".raw")).read_bytes()
        return [
            dict(r, raw_file=name + ".raw", raw_sha256=hashlib.sha256(raw).hexdigest())
            for r in records
        ]

    moh, hcm, who = [], [], []
    for category in ["8358", "8351", "8360", "8356", "8355", "8338"]:
        name = "moh_" + category
        inventory = json.loads((directory / (name + ".raw")).read_bytes())[0]
        assert str(inventory["MA"]) == category
        assert len(inventory["ITEMS"]) == min(1000, inventory["TOTAL_RECORD"])
        records = []
        for index, row in enumerate(inventory["ITEMS"]):
            records.append(
                {
                    "url": f"https://moh.gov.vn/index.jsp?pageId=5803&aid={row['MA']}&cid={category}",
                    "date": datetime.strptime(row["THOI_GIAN_XUAT_BAN"], "%d/%m/%Y %I:%M %p")
                    .date()
                    .isoformat(),
                    "locator": f"ITEMS[{index}].THOI_GIAN_XUAT_BAN",
                }
            )
        assert min(r["date"] for r in records) < "2026-09-28"
        moh.extend(evidence(name, records))
    for name in ["hcm_page1", "hcm_page2"]:
        hcm.extend(
            evidence(
                name,
                cards(
                    (directory / (name + ".raw")).read_bytes(),
                    "https://medinet.hochiminhcity.gov.vn",
                    True,
                ),
            )
        )
    for name in [
        "who_releases",
        "who_feature-stories",
        "who_commentaries",
        "who_speeches",
        "who_ageing",
    ]:
        who.extend(
            evidence(name, cards((directory / (name + ".raw")).read_bytes(), "https://www.who.int"))
        )
    # Alternative channel for Vietnam news releases. Validate server count, and
    # do not count global news as Vietnam merely because it mentions the country.
    value = json.loads((directory / "who_site_api.raw").read_bytes())
    assert value["@odata.count"] == len(value["value"])
    who.extend(
        evidence(
            "who_site_api",
            [
                {
                    "url": "https://www.who.int/vietnam/news/detail" + r["ItemDefaultUrl"],
                    "date": r["PublicationDateAndTime"][:10],
                    "locator": f"value[{i}].PublicationDateAndTime; sf_site=bad9ef31-b8f9-4c48-b98f-345320b3367a",
                }
                for i, r in enumerate(value["value"])
            ],
        )
    )
    sources = [
        {
            "source_id": "moh_portal",
            "method": "Fresh API inventory, independently enumerated IDs/dates",
            "independence": "same_upstream_inventory",
            "scope": "Six configured Ministry categories",
            "complete": False,
            "limitations": [
                "Same upstream endpoint as collector; no independent completeness oracle."
            ],
            "records": moh,
        },
        {
            "source_id": "hcm_health",
            "method": "Fresh first two category pages, separately parsed visible date cards; no date-based early stop",
            "independence": "separate_extractor_same_website",
            "scope": "HCMC c1780 news/events",
            "complete": False,
            "limitations": [
                "Two bounded pages do not prove archive completeness; listings differ between captures."
            ],
            "records": hcm,
        },
        {
            "source_id": "who_ageing",
            "method": "Vietnam-site REST API plus separately parsed configured HTML seeds",
            "independence": "alternative_api_for_releases_only",
            "scope": "Ageing landing and Vietnam releases/features/commentaries/speeches",
            "complete": False,
            "limitations": [
                "API cross-check covers releases; other branches use the same website listings, with no independent archive oracle."
            ],
            "records": who,
        },
    ]
    for source in sources:
        source.update(mindate="2026-09-28", maxdate="2026-10-20")
        source["date_policy"] = (
            "source_displayed" if source["source_id"] == "hcm_health" else "publication"
        )
    return {"schema_version": "coverage-reference-1", "sources": sources}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.directory)
    with args.out.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
