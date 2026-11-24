"""Read new posts from public Telegram channels through their web preview (t.me/s/<name>)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

PHOTO_URL = re.compile(r"url\(['\"]?([^'\")]+)['\"]?\)")


@dataclass
class SourcePost:
    channel: str
    post_id: int
    text: str
    links: list[str] = field(default_factory=list)
    link_texts: list[str] = field(default_factory=list)
    photo: str | None = None
    date: str | None = None


def channel_name(value: str) -> str:
    """'https://t.me/HotDealsAQ', '@HotDealsAQ' or 'HotDealsAQ' -> 'HotDealsAQ'."""
    value = value.strip().rstrip("/")
    value = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?", "", value)
    return value.lstrip("@")


def parse_page(html: str, channel: str) -> list[SourcePost]:
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        try:
            post_id = int(msg["data-post"].split("/")[-1])
        except ValueError:
            continue
        body = msg.select_one(".tgme_widget_message_text")
        text, links, link_texts = "", [], []
        if body is not None:
            anchors = body.select("a[href]")
            links = [a["href"] for a in anchors]
            link_texts = [a.get_text("") for a in anchors]
            for br in body.find_all("br"):
                br.replace_with("\n")
            text = body.get_text("")
        photo = None
        wrap = msg.select_one(".tgme_widget_message_photo_wrap")
        if wrap is not None:
            m = PHOTO_URL.search(wrap.get("style", ""))
            if m:
                photo = m.group(1)
                if photo.startswith("//"):
                    photo = "https:" + photo
        when = msg.select_one("time[datetime]")
        posts.append(SourcePost(channel, post_id, text.strip(), links, link_texts, photo,
                                when["datetime"] if when else None))
    posts.sort(key=lambda p: p.post_id)
    return posts


def fetch(channel: str, session, timeout: int = 20) -> list[SourcePost]:
    resp = session.get(f"https://t.me/s/{channel}", timeout=timeout,
                       headers={"Accept-Language": "ar,en;q=0.8"})
    resp.raise_for_status()
    return parse_page(resp.text, channel)
