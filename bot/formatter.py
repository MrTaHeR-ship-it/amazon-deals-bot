"""Telegram post text in Egyptian Arabic (HTML parse mode).

Every line we write starts with an Arabic word so Telegram keeps it right-to-left,
and product names are wrapped in a Unicode isolate so English titles and numbers
do not scramble the Arabic around them.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date
from urllib.parse import quote

from .amazon_api import Deal
from .links import URL_IN_TEXT, utf16_to_py_index, with_scheme

FSI, PDI = "⁨", "⁩"  # first-strong isolate: keeps a title's own direction

HEADLINES_BIG = [
    "خصم {pct}% على أمازون مصر، الحق قبل ما يخلص 🔥",
    "لقطة النهارده: خصم {pct}% 🔥",
    "نص التمن وأقل! خصم {pct}% 😳",
]
HEADLINES_MID = [
    "عرض حلو على أمازون مصر 💥",
    "خصم {pct}% على حاجة تستاهل 👌",
    "وفّر فلوسك: خصم {pct}% ⚡",
]
HEADLINES_SMALL = [
    "عرض جديد على أمازون مصر 🛒",
    "سعر كويس النهارده على أمازون 👌",
    "لو بتدور عليه، ده وقته 👀",
]

PROMO_LINE = re.compile(r"(انضم|اشترك|تابعونا|تابعوا|قناتنا|جروبنا|join|subscribe)", re.I)
MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{4,32})")
TME_LINK = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>]+", re.I)


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def attr(text: str) -> str:
    return html.escape(text or "", quote=True)


def money(amount: float | None) -> str:
    if amount is None:
        return ""
    if float(amount).is_integer():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def short_title(title: str, limit: int = 110) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    if len(title) <= limit:
        return title
    cut = title[:limit].rsplit(" ", 1)[0].rstrip(" ,-–|،")
    return cut + "…"


def pick(options: list[str], key: str) -> str:
    """Rotate wording so posts do not all open the same way."""
    digest = hashlib.md5(f"{key}{date.today().isoformat()}".encode()).hexdigest()
    return options[int(digest, 16) % len(options)]


def signature(text: str) -> str:
    """Keep handles like @mr.taher.reviews in one piece inside an Arabic line."""
    return re.sub(r"(@[\w.]+|https?://\S+)", lambda m: f"{FSI}{m.group(0)}{PDI}", esc(text))


def currency_word(code: str) -> str:
    return "جنيه" if (code or "EGP").upper() == "EGP" else code


def deal_post(deal: Deal, cfg: dict) -> tuple[str, tuple[str, str]]:
    sig = cfg["posting"]["signature"]
    cur = currency_word(deal.currency)
    pct = deal.saving_pct or 0
    has_discount = bool(deal.price and deal.old_price and deal.old_price > deal.price and pct >= 5)

    if has_discount and pct >= 50:
        headline = pick(HEADLINES_BIG, deal.asin)
    elif has_discount and pct >= 20:
        headline = pick(HEADLINES_MID, deal.asin)
    else:
        headline = pick(HEADLINES_SMALL, deal.asin)
    lines = [headline.format(pct=pct), ""]

    lines.append(f"المنتج: <b>{FSI}{esc(short_title(deal.title))}{PDI}</b>")
    if has_discount:
        lines.append(
            f"السعر دلوقتي: <b>{money(deal.price)} {cur}</b> بدل <s>{money(deal.old_price)}</s> {cur}"
        )
        saving = deal.saving or (deal.old_price - deal.price)
        lines.append(f"هتوفر {money(round(saving))} {cur}، يعني خصم {pct}% 💸")
    elif deal.price:
        lines.append(f"السعر: <b>{money(deal.price)} {cur}</b>")
    if deal.rating and (deal.reviews or 0) >= 20:
        lines.append(f"تقييمه ⭐ {deal.rating:.1f} من 5 من {deal.reviews:,} تقييم")
    if deal.deal_badge or deal.deal_end:
        lines.append("عرض لفترة محدودة ⚡")

    lines += [
        "",
        "اطلبه من هنا 👇",
        esc(deal.url),
        "السعر وقت النشر وممكن يتغير في أي وقت",
        signature(sig),
    ]
    return "\n".join(lines), ("اطلبه من أمازون 🛒", deal.url)


def link_only_post(url: str, cfg: dict, headline: str | None = None) -> tuple[str, tuple[str, str]]:
    """A product link when we have no product data: Telegram's preview shows the item."""
    lines = [
        esc(headline) if headline else "عرض جديد على أمازون مصر 🔥",
        "",
        "التفاصيل والسعر من هنا 👇",
        esc(url),
        signature(cfg["posting"]["signature"]),
    ]
    return "\n".join(lines), ("اطلبه من أمازون 🛒", url)


def collection_post(url: str, cfg: dict, headline: str | None = None) -> tuple[str, tuple[str, str]]:
    """A page with many deals (deals page, event, category) as one post."""
    lines = [
        esc(headline) if headline else "عروض جديدة على أمازون مصر 🔥",
        "",
        "شوف كل العروض من هنا 👇",
        esc(url),
        signature(cfg["posting"]["signature"]),
    ]
    return "\n".join(lines), ("شوف العروض 🛒", url)


# --- Reposting a forwarded message with our links -------------------------------------

FORMAT_TAGS = {
    "bold": ("<b>", "</b>"),
    "italic": ("<i>", "</i>"),
    "underline": ("<u>", "</u>"),
    "strikethrough": ("<s>", "</s>"),
    "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
    "code": ("<code>", "</code>"),
    "pre": ("<pre>", "</pre>"),
    "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
}


def _own(handle: str, own_handles: list[str]) -> bool:
    handle = handle.lower().lstrip("@")
    return any(h.lower().lstrip("@") in handle for h in own_handles)


TRAILING = ".,;:!?»”’"


def swap_link(token: str, link_map: dict[str, str], own: list[str]) -> str:
    """Our tagged version of a link, the link itself if it is ours, else nothing."""
    core = token.rstrip(TRAILING)
    trail = token[len(core):]
    new = link_map.get(token) or link_map.get(core)
    if new is None:
        normal = {with_scheme(k): v for k, v in link_map.items()}
        new = normal.get(with_scheme(core))
    if not new and _own(core, own):
        new = core
    return (new + trail) if new else ""


def _plain(segment: str, link_map: dict[str, str], own: list[str]) -> str:
    """Escape a stretch of text, swapping any links and dropping other people's handles."""
    pieces, pos = [], 0
    pattern = re.compile(f"{URL_IN_TEXT.pattern}|{TME_LINK.pattern}|{MENTION.pattern}", re.I)
    for m in pattern.finditer(segment):
        pieces.append(esc(segment[pos:m.start()]))
        token = m.group(0)
        if token.startswith("@"):
            pieces.append(esc(token) if _own(token, own) else "")
        elif TME_LINK.fullmatch(token):
            pieces.append(esc(token) if _own(token, own) else "")
        else:
            pieces.append(esc(swap_link(token, link_map, own)))
        pos = m.end()
    pieces.append(esc(segment[pos:]))
    return "".join(pieces)


def convert_message(text: str, entities: list[dict] | None, link_map: dict[str, str],
                    cfg: dict) -> str:
    """Rebuild a Telegram message as HTML with every Amazon link carrying our tag."""
    own = cfg["posting"].get("own_handles", [])
    text = text or ""
    index = utf16_to_py_index(text)
    events = []
    for n, ent in enumerate(entities or []):
        try:
            start = index[ent["offset"]]
            end = index[ent["offset"] + ent["length"]]
        except (IndexError, KeyError):
            continue
        kind = ent.get("type")
        if kind in ("url", "mention"):
            events.append((start, 2, 0, n, "atomic", (end, ent)))
        elif kind in FORMAT_TAGS or kind == "text_link":
            events.append((start, 1, -(end - start), n, "open", ent))
            events.append((end, 0, end - start, -n, "close", ent))
    events.sort(key=lambda e: (e[0], e[1], e[2], e[3]))

    out, cursor = [], 0
    link_open: dict[int, bool] = {}
    for pos, _, _, n, kind, payload in events:
        if pos > cursor:
            out.append(_plain(text[cursor:pos], link_map, own))
            cursor = pos
        if kind == "atomic":
            end, ent = payload
            token = text[pos:end]
            if ent["type"] == "mention":
                out.append(esc(token) if _own(token, own) else "")
            elif TME_LINK.fullmatch(token):
                out.append(esc(token) if _own(token, own) else "")
            else:
                out.append(esc(swap_link(token, link_map, own)))
            cursor = max(cursor, end)
        elif kind == "open":
            if payload["type"] == "text_link":
                url = payload.get("url", "")
                new = swap_link(url, link_map, own)
                link_open[id(payload)] = bool(new)
                if new:
                    out.append(f'<a href="{attr(new)}">')
            else:
                out.append(FORMAT_TAGS[payload["type"]][0])
        else:
            if payload["type"] == "text_link":
                if link_open.get(id(payload)):
                    out.append("</a>")
            else:
                out.append(FORMAT_TAGS[payload["type"]][1])
    out.append(_plain(text[cursor:], link_map, own))

    body = "".join(out)
    kept = []
    for line in body.split("\n"):
        if PROMO_LINE.search(line) and "amazon.eg" not in line:
            continue
        kept.append(line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return f"{body}\n\n{signature(cfg['posting']['signature'])}"


def visible_length(html_text: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def strip_html(html_text: str) -> str:
    text = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2 \1", html_text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# --- WhatsApp: the same post as plain text, with WhatsApp's own formatting --------------

WA_SHARE = "https://wa.me/?text="
WA_URL_LIMIT = 1500


def to_whatsapp(html_text: str) -> str:
    """Telegram HTML -> WhatsApp text (*bold*, _italic_, ~strike~, links written out)."""
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>',
                  lambda m: m.group(2) if html.unescape(m.group(2)) == html.unescape(m.group(1))
                  else f"{m.group(2)} {m.group(1)}", html_text, flags=re.S)
    for tag, mark in (("b", "*"), ("strong", "*"), ("i", "_"), ("em", "_"), ("s", "~"), ("del", "~")):
        text = re.sub(rf"<{tag}>(.*?)</{tag}>", lambda m: f"{mark}{m.group(1).strip()}{mark}", text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text).replace(FSI, "").replace(PDI, "")
    return html.unescape(text).strip()


def share_button(wa_text: str) -> tuple[str, str]:
    """A button that opens WhatsApp with the post ready to send."""
    url = WA_SHARE + quote(wa_text)
    if len(url) > WA_URL_LIMIT:  # too long for a button: first line plus the links
        lines = wa_text.split("\n")
        links = [l for l in lines if "amazon.eg" in l]
        url = WA_SHARE + quote("\n".join([lines[0], ""] + links[:3]))
        if len(url) > WA_URL_LIMIT:
            url = WA_SHARE + quote(links[0] if links else lines[0][:200])
    return ("شير على واتساب 📲", url)
