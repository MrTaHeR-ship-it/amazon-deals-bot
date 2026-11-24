import re

from creatorsapi_python_sdk.models.item import Item

from bot import formatter as fmt
from bot.amazon_api import item_to_deal

from .conftest import TAG, make_deal

ARABIC_START = re.compile(r"^[؀-ۿ]")


def lines_of(body):
    return [l for l in body.split("\n") if l.strip()]


def test_every_line_we_write_starts_with_arabic(cfg):
    deal = make_deal(title="Anker Soundcore Life P3i (Hybrid ANC) 36H Playtime", deal_badge="Limited")
    for body, _ in [fmt.deal_post(deal, cfg),
                    fmt.link_only_post(deal.url, cfg),
                    fmt.collection_post(deal.url, cfg)]:
        for line in lines_of(body):
            assert ARABIC_START.match(line.lstrip("<b>")), line


def test_deal_post_content(cfg):
    deal = make_deal(price=1299.0, old=1899.0, pct=32, title="Redmi Note 14 Pro 5G 256GB")
    body, button = fmt.deal_post(deal, cfg)
    assert "1,299 جنيه" in body and "<s>1,899 جنيه</s>" in body
    assert "هتوفر 600 جنيه، يعني خصم 32%" in body
    assert f"tag={TAG}" in body and button[1] == deal.url
    assert "@mr.taher.reviews" in body
    assert "⁨Redmi Note 14 Pro 5G 256GB⁩" in body


def test_no_discount_shows_plain_price(cfg):
    body, _ = fmt.deal_post(make_deal(price=500.0, old=None, pct=None), cfg)
    assert "السعر: <b>500 جنيه</b>" in body and "هتوفر" not in body


def test_long_titles_are_cut():
    title = "Samsung Galaxy " + "Very Long Title Word " * 10
    assert len(fmt.short_title(title)) <= 111 and fmt.short_title(title).endswith("…")


def test_convert_message_swaps_links_and_removes_competitors(cfg):
    text = ("🔥 عرض سماعة انكر بخصم 40%\n"
            "السعر 899 بدل 1499\n"
            "اطلب من هنا amzn.eu/d/abc123\n"
            "واطلب التانية من هنا\n"
            "انضم لقناتنا @dealsking\n"
            "نون: https://noon.com/x")
    hidden_at = len(text[:text.index("من هنا\nانضم")].encode("utf-16-le")) // 2
    entities = [
        {"type": "bold", "offset": 0, "length": len("🔥 عرض سماعة انكر".encode("utf-16-le")) // 2},
        {"type": "text_link", "offset": hidden_at, "length": 6, "url": "https://www.amazon.eg/dp/B0SECOND01?tag=x-21"},
        {"type": "mention", "offset": len(text[:text.index("@deals")].encode("utf-16-le")) // 2, "length": 10},
        {"type": "url", "offset": len(text[:text.index("https://noon")].encode("utf-16-le")) // 2,
         "length": len("https://noon.com/x")},
    ]
    link_map = {
        "amzn.eu/d/abc123": f"https://www.amazon.eg/dp/B0FIRST001?tag={TAG}",
        "https://www.amazon.eg/dp/B0SECOND01?tag=x-21": f"https://www.amazon.eg/dp/B0SECOND01?tag={TAG}",
        "https://noon.com/x": "",
    }
    out = fmt.convert_message(text, entities, link_map, cfg)
    assert out.startswith("<b>🔥 عرض سماعة انكر</b>")
    assert f"https://www.amazon.eg/dp/B0FIRST001?tag={TAG}" in out
    assert f'<a href="https://www.amazon.eg/dp/B0SECOND01?tag={TAG}">من هنا</a>' in out
    assert "dealsking" not in out and "انضم" not in out and "noon" not in out
    assert "x-21" not in out
    assert out.rstrip().endswith(cfg["posting"]["signature"])


def test_item_to_deal_from_real_sdk_model():
    item = Item.from_dict({
        "asin": "B0REAL0001",
        "detailPageURL": f"https://www.amazon.eg/dp/B0REAL0001?tag={TAG}&linkCode=ogi&th=1&psc=1",
        "images": {"primary": {"large": {"url": "https://m.media-amazon.com/images/I/x.jpg"}}},
        "itemInfo": {"title": {"displayValue": "Anker Power Bank 20000mAh"}},
        "customerReviews": {"count": 812, "starRating": {"value": 4.6}},
        "offersV2": {"listings": [{
            "availability": {"type": "IN_STOCK"},
            "isBuyBoxWinner": True,
            "dealDetails": {"badge": "Limited time deal", "endTime": "2026-11-28T21:59:00Z"},
            "price": {
                "money": {"amount": 849.0, "currency": "EGP", "displayAmount": "EGP 849.00"},
                "savingBasis": {"money": {"amount": 1199.0, "currency": "EGP"}},
                "savings": {"money": {"amount": 350.0, "currency": "EGP"}, "percentage": 29},
            },
        }]},
    })
    deal = item_to_deal(item, TAG)
    assert (deal.price, deal.old_price, deal.saving, deal.saving_pct) == (849.0, 1199.0, 350.0, 29)
    assert deal.rating == 4.6 and deal.reviews == 812 and deal.in_stock
    assert deal.deal_badge == "Limited time deal"
    assert deal.url.startswith("https://www.amazon.eg/dp/B0REAL0001?tag=" + TAG)


def test_item_out_of_stock():
    item = Item.from_dict({
        "asin": "B0OOS00001", "itemInfo": {"title": {"displayValue": "X"}},
        "offersV2": {"listings": [{"availability": {"type": "OUT_OF_STOCK"},
                                   "price": {"money": {"amount": 10.0}}}]},
    })
    assert item_to_deal(item, TAG).in_stock is False


def test_to_whatsapp_and_visible_links(cfg):
    deal = make_deal(price=1299.0, old=1899.0, pct=32)
    body, _ = fmt.deal_post(deal, cfg)
    assert f"اطلبه من هنا 👈 {deal.url}" in body  # link stays when copied out of Telegram
    wa = fmt.to_whatsapp(body)
    assert "*1,299 جنيه*" in wa and "~1,899 جنيه~" in wa and "<" not in wa
    assert fmt.to_whatsapp('اضغط <a href="https://www.amazon.eg/dp/X?tag=t">هنا</a>') == \
        "اضغط هنا https://www.amazon.eg/dp/X?tag=t"


def test_share_button_shortens_long_posts():
    long_text = "عرض " * 800 + "\nhttps://www.amazon.eg/dp/B0AAAAAAAA?tag=" + TAG
    label, url = fmt.share_button(long_text)
    assert len(url) <= fmt.WA_URL_LIMIT and "amazon.eg" in url
