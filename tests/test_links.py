from bot.links import analyse, classify, find_urls, resolve, tag_url, utf16_slice

from .conftest import TAG, FakeSession


def test_product_links_become_clean_tagged_links():
    for url in [
        "https://www.amazon.eg/-/ar/Samsung-Galaxy/dp/B0CX123456/ref=sr_1_3?keywords=x&tag=someone-21",
        "amazon.eg/gp/product/b0cx123456?psc=1",
        "https://www.amazon.eg/gp/aw/d/B0CX123456",
    ]:
        assert tag_url(url, TAG) == f"https://www.amazon.eg/dp/B0CX123456?tag={TAG}"


def test_search_and_category_links_keep_filters_and_swap_tag():
    url = "https://www.amazon.eg/s?k=earbuds&rh=n%3A21832883031&ref=nb_sb_noss&tag=other-21&linkCode=ll2"
    link = classify(url)
    assert link.kind == "search" and link.keywords == "earbuds" and link.node == "21832883031"
    tagged = tag_url(url, TAG)
    assert "k=earbuds" in tagged and f"tag={TAG}" in tagged
    assert "other-21" not in tagged and "linkCode" not in tagged and "ref=" not in tagged

    node = classify("https://www.amazon.eg/-/en/b/?node=18018102031&ref=xyz")
    assert node.kind == "node" and node.node == "18018102031"


def test_deals_pages_are_collections():
    url = "https://www.amazon.eg/deals?ref_=nav_cs_gb&deals-widget=%7B%22version%22%3A1%7D"
    assert classify(url).kind == "collection"
    tagged = tag_url(url, TAG)
    assert tagged.startswith("https://www.amazon.eg/deals?") and f"tag={TAG}" in tagged
    assert "deals-widget" in tagged and "ref_" not in tagged


def test_short_links_resolve_by_redirect_header_only():
    session = FakeSession({
        "https://amzn.eu/d/abc123": "https://www.amazon.eg/dp/B0SHORT001?tag=someone-21",
        "https://bit.ly/xyz": "https://amzn.eu/d/abc123",
    })
    assert resolve("https://bit.ly/xyz", session) == "https://www.amazon.eg/dp/B0SHORT001?tag=someone-21"
    assert resolve("https://amzn.eu/d/missing", session) is None


def test_find_urls_reads_hidden_links_and_bare_short_links():
    text = "سماعة حلوة اضغط هنا\nوكمان amzn.eu/d/abc123 وده https://noon.com/x"
    start = len("سماعة حلوة ".encode("utf-16-le")) // 2
    entities = [
        {"type": "text_link", "offset": start, "length": 8, "url": "https://www.amazon.eg/dp/B0HIDDEN01"},
        {"type": "url", "offset": text.index("https://noon"), "length": len("https://noon.com/x")},
    ]
    urls = find_urls(text, entities)
    assert urls == ["https://www.amazon.eg/dp/B0HIDDEN01", "https://noon.com/x", "amzn.eu/d/abc123"]


def test_analyse_maps_links_and_drops_others():
    session = FakeSession({"https://amzn.eu/d/abc123": "https://www.amazon.eg/dp/B0SHORT001"})
    report, mapping = analyse(["amzn.eu/d/abc123", "https://noon.com/x", "https://amzn.eu/d/dead"],
                              TAG, session)
    assert [l.asin for l in report.amazon] == ["B0SHORT001"]
    assert mapping["amzn.eu/d/abc123"] == f"https://www.amazon.eg/dp/B0SHORT001?tag={TAG}"
    assert mapping["https://noon.com/x"] == "" and report.other == ["https://noon.com/x"]
    assert report.failed == ["https://amzn.eu/d/dead"]


def test_utf16_offsets_with_emoji():
    text = "🔥 عرض https://amzn.eu/d/q"
    offset = len("🔥 عرض ".encode("utf-16-le")) // 2
    assert utf16_slice(text, offset, len("https://amzn.eu/d/q")) == "https://amzn.eu/d/q"
