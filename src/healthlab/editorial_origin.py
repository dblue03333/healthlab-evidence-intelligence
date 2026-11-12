"""Footer attribution on reposted articles: trusted publishers versus unresolved origin.

An external "Nguồn:" or "Theo:" footer normally forces origin review. A footer that names
only publishers on this allowlist is accepted as a repost from a known outlet; the
newsletter then cites both the original publisher and the host that was collected. Any
other footer, or one without a readable host, keeps the existing block.
"""

import re

TRUSTED_FOOTER_HOSTS = (
    "suckhoedoisong.vn",
    "moh.gov.vn",
    "who.int",
    "medinet.hochiminhcity.gov.vn",
    "baochinhphu.vn",
    "nhandan.vn",
    "tuoitre.vn",
    "qdnd.vn",
    "hanoimoi.vn",
    "daidoanket.vn",
    "baocantho.com.vn",
)
_FOOTER = re.compile(r"(?:Nguồn|Theo)\s*:\s*[^\n]+", re.I)
_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I)


def _trusted(host):
    host = host.lower()
    return any(host == t or host.endswith("." + t) for t in TRUSTED_FOOTER_HOSTS)


def footer_attributions(body):
    """Return (has_footer, trusted_hosts, all_trusted) for the last 2500 characters."""
    matches = list(_FOOTER.finditer(body[-2500:]))
    if not matches:
        return False, [], True
    hosts, all_trusted = [], True
    for match in matches:
        snippet = match[0][:120].split(")")[0]
        found = _HOST.findall(snippet)
        if not found or not all(_trusted(h) for h in found):
            all_trusted = False
        hosts.extend(h.lower() for h in found if _trusted(h))
    return True, sorted(set(hosts)), all_trusted
