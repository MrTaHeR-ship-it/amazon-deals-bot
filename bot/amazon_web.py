"""Deals read straight from amazon.eg search pages, for when the Creators API is closed."""
from __future__ import annotations

import random
import re
import time
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .amazon_api import Deal
from .links import product_url

SEARCH_URL = "https://www.amazon.eg/s?k={k}&language=ar_AE"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
}
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
IMAGE_SIZE = re.compile(r"\._[^/]*?_\.(jpg|jpeg|png|webp)$", re.I)


class Blocked(Exception):
    """Amazon answered with a captcha or an error page instead of results."""


def number(text: str | None) -> float | None:
    if not text:
        return None
    m = NUMBER.search(text.replace("٬", ",").replace("٫", "."))
    return float(m.group().replace(",", "")) if m else None


def full_image(src: str | None) -> str | None:
    """Search shows a 320px thumbnail; drop the size code to get Amazon's full picture."""
    return IMAGE_SIZE.sub(r".\1", src) if src else None


def parse_search(html: str, tag: str) -> list[Deal]:
    soup = BeautifulSoup(html, "html.parser")
    deals = []
    for card in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = card.get("data-asin", "").strip()
        if not asin or "AdHolder" in (card.get("class") or []):
            continue
        if card.select_one(".puis-sponsored-label-text, .s-sponsored-label-text"):
            continue
        h2 = card.select_one("h2")
        title = (h2.get("aria-label") or h2.get_text(" ", strip=True)) if h2 else ""
        price = number(_text(card, ".a-price:not(.a-text-price) .a-offscreen"))
        old = number(_text(card, ".a-price.a-text-price .a-offscreen"))
        if not title or not price:
            continue
        if old and old <= price:
            old = None
        pct = round((old - price) / old * 100) if old else None
        rating = number(_text(card, "span.a-icon-alt"))
        reviews = None
        for a in card.select("a[aria-label]"):
            if "تقييم" in a["aria-label"] or "rating" in a["aria-label"].lower():
                reviews = number(a["aria-label"])
                break
        img = card.select_one("img.s-image")
        deals.append(Deal(
            asin=asin, title=title, url=product_url(asin, tag),
            price=price, old_price=old, saving=(old - price) if old else None, saving_pct=pct,
            image=full_image(img.get("src") if img else None),
            rating=rating if rating and rating <= 5 else None,
            reviews=int(reviews) if reviews else None,
            deal_badge=_text(card, ".a-badge-text"),
        ))
    return deals


def _text(node, selector):
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else None


class WebStore:
    """Reads amazon.eg search pages the way a browser does, slowly and politely."""

    def __init__(self, session, tag: str, pause=(2.0, 5.0), sleep=time.sleep):
        self.session, self.tag, self.pause, self.sleep = session, tag, pause, sleep
        self.requests = 0

    def search(self, keywords: str) -> list[Deal]:
        if self.requests:
            self.sleep(random.uniform(*self.pause))
        self.requests += 1
        resp = self.session.get(SEARCH_URL.format(k=quote_plus(keywords)), headers=HEADERS, timeout=25)
        html = getattr(resp, "text", "") or ""
        if resp.status_code != 200 or "validateCaptcha" in html or "/errors/validateCaptcha" in html:
            raise Blocked(f"HTTP {resp.status_code}")
        deals = parse_search(html, self.tag)
        if not deals and 's-search-result' not in html:
            raise Blocked("no results block on the page")
        return deals
