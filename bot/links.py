"""Find Amazon Egypt links in a message, resolve short links, and put the affiliate tag on them.

Nothing here downloads an Amazon page. Short links are resolved by reading the
redirect header only, and stop as soon as they point at amazon.eg.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

AMAZON_HOST = "www.amazon.eg"

# Hosts that are Amazon's own short links.
AMAZON_SHORT_HOSTS = {"amzn.eu", "amzn.to", "a.co", "amzn.com", "amzn.asia",
                      "link.amazon", "amzlinks.in"}

# General shorteners other people use to hide their own Amazon links.
GENERIC_SHORT_HOSTS = {
    "bit.ly", "bitly.com", "tinyurl.com", "cutt.ly", "rb.gy", "shorturl.at",
    "t.ly", "is.gd", "s.id", "rebrand.ly", "ow.ly", "buff.ly", "tiny.cc",
    "shorturl.asia", "lnkd.in", "goo.su", "clck.ru", "v.gd", "short.gy",
}

# Query parameters that belong to someone else's affiliate tracking.
AFFILIATE_PARAMS = {
    "tag", "ascsubtag", "linkcode", "linkid", "camp", "creative", "creativeasin",
    "ref_", "ref", "pd_rd_w", "pd_rd_wg", "pd_rd_r", "pd_rd_i", "pf_rd_p",
    "pf_rd_r", "content-id", "social_share", "starsleft", "skipTwisterOG".lower(),
    "sp_csd", "spla", "psc", "th", "smid_ref",
}

URL_IN_TEXT = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*"
    r"(?:amazon\.eg|amzn\.eu|amzn\.to|a\.co|amzn\.asia|amzn\.com|link\.amazon|amzlinks\.in"
    r"|bit\.ly|bitly\.com|tinyurl\.com|cutt\.ly|rb\.gy|shorturl\.at|t\.ly|is\.gd"
    r"|s\.id|rebrand\.ly|ow\.ly|buff\.ly|tiny\.cc|v\.gd|short\.gy)"
    r"/[^\s<>\"'()\[\]{}،]*",
    re.IGNORECASE,
)

ASIN_IN_PATH = re.compile(
    r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN|o/ASIN|ASIN)/([A-Z0-9]{10})(?=[/?#]|$)",
    re.IGNORECASE,
)
NODE_IN_RH = re.compile(r"(?:^|,)n:(\d+)")


@dataclass
class AmazonLink:
    original: str
    resolved: str
    kind: str  # product | search | node | collection
    asin: str | None = None
    keywords: str | None = None
    node: str | None = None
    tagged: str = ""


@dataclass
class LinkReport:
    amazon: list[AmazonLink] = field(default_factory=list)
    other: list[str] = field(default_factory=list)  # links that are not amazon.eg
    failed: list[str] = field(default_factory=list)  # short links that did not resolve


def host_of(url: str) -> str:
    return (urlsplit(with_scheme(url)).hostname or "").lower()


def with_scheme(url: str) -> str:
    url = url.strip().rstrip(".,;:!?»”’")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def is_amazon_eg(url: str) -> bool:
    host = host_of(url)
    return host == "amazon.eg" or host.endswith(".amazon.eg")


def is_short(url: str) -> bool:
    host = host_of(url)
    host = host[4:] if host.startswith("www.") else host
    return host in AMAZON_SHORT_HOSTS or host in GENERIC_SHORT_HOSTS


def resolve(url: str, session, max_hops: int = 6) -> str | None:
    """Follow redirects of a short link by reading only the Location header.

    Returns the first amazon.eg URL reached, the final non-short URL, or None
    when the short link could not be resolved.
    """
    current = with_scheme(url)
    for _ in range(max_hops):
        if is_amazon_eg(current) or not is_short(current):
            return current
        try:
            resp = session.head(current, allow_redirects=False, timeout=10)
            if resp.status_code not in (301, 302, 303, 307, 308):
                # some shorteners (link.amazon) only redirect a GET; the body is not read
                resp = session.get(current, allow_redirects=False, timeout=10, stream=True)
                resp.close()
        except Exception:  # network trouble: give up on this one link
            return None
        location = resp.headers.get("location") or resp.headers.get("Location")
        if not location or resp.status_code not in (301, 302, 303, 307, 308):
            return None
        current = urljoin(current, location)
    return current if is_amazon_eg(current) else None


def classify(url: str) -> AmazonLink:
    parts = urlsplit(with_scheme(url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    lowered = {k.lower(): v for k, v in query.items()}

    match = ASIN_IN_PATH.search(parts.path)
    if match:
        return AmazonLink(url, url, "product", asin=match.group(1).upper())

    path = parts.path.rstrip("/")
    tail = path.split("/")[-1] if path else ""
    node = lowered.get("node")
    rh_node = NODE_IN_RH.search(lowered.get("rh", ""))

    if tail == "s" or path.endswith("/s"):
        keywords = lowered.get("k") or lowered.get("field-keywords")
        node = node or (rh_node.group(1) if rh_node else None)
        if keywords or node:
            return AmazonLink(url, url, "search", keywords=keywords, node=node)

    if (tail == "b" or "/b/" in parts.path + "/") and node and node.isdigit():
        return AmazonLink(url, url, "node", node=node)

    return AmazonLink(url, url, "collection")


def product_url(asin: str, tag: str) -> str:
    return f"https://{AMAZON_HOST}/dp/{asin}?tag={tag}"


def tag_url(url: str, tag: str) -> str:
    """Put our tag on any amazon.eg URL and drop other people's tracking."""
    parts = urlsplit(with_scheme(url))
    match = ASIN_IN_PATH.search(parts.path)
    if match:
        return product_url(match.group(1).upper(), tag)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in AFFILIATE_PARAMS
    ]
    kept.append(("tag", tag))
    path = re.sub(r"/ref=[^/?]*", "", parts.path) or "/"
    return urlunsplit(("https", AMAZON_HOST, path, urlencode(kept), ""))


def find_urls(text: str, entities: list[dict] | None = None) -> list[str]:
    """Every Amazon or short link in a Telegram message, in order, without duplicates."""
    found: list[str] = []
    for ent in entities or []:
        if ent.get("type") == "text_link" and ent.get("url"):
            found.append(ent["url"])
        elif ent.get("type") == "url":
            found.append(utf16_slice(text, ent["offset"], ent["length"]))
    found.extend(m.group(0) for m in URL_IN_TEXT.finditer(text or ""))
    seen, ordered = set(), []
    for url in found:
        key = with_scheme(url)
        if key not in seen:
            seen.add(key)
            ordered.append(url)
    return ordered


def analyse(urls: list[str], tag: str, session) -> tuple[LinkReport, dict[str, str]]:
    """Resolve and classify links. Returns the report and a map original -> tagged URL
    (or "" for links that should be removed from a reposted message)."""
    report, mapping = LinkReport(), {}
    for url in urls:
        target = with_scheme(url)
        if is_short(target):
            resolved = resolve(target, session)
            if resolved is None:
                report.failed.append(url)
                mapping[url] = ""
                continue
            target = resolved
        if not is_amazon_eg(target):
            report.other.append(url)
            mapping[url] = ""
            continue
        link = classify(target)
        link.original = url
        link.resolved = target
        link.tagged = tag_url(target, tag)
        report.amazon.append(link)
        mapping[url] = link.tagged
    return report, mapping


# --- UTF-16 helpers: Telegram counts entity offsets in UTF-16 code units -------------

def utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def utf16_to_py_index(text: str) -> list[int]:
    """Map each UTF-16 position (0..n) to a Python string index."""
    mapping = []
    for i, ch in enumerate(text):
        mapping.append(i)
        if ord(ch) > 0xFFFF:
            mapping.append(i)
    mapping.append(len(text))
    return mapping
