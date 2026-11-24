"""One run of the deals bot: read the inbox, look for deals, publish what is due.

GitHub Actions starts this every 30 minutes; everything it remembers between runs
lives in data/state.json, which the workflow commits back to the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from . import formatter as fmt
from .images import crop_card
from .amazon_api import AmazonClient, ApiThrottled, ApiUnavailable, Deal
from urllib.parse import urlsplit

from .links import URL_IN_TEXT, analyse, find_urls
from .offer import SEPARATOR, Offer
from .offer import parse as parse_offer
from .sources import channel_name
from .sources import fetch as fetch_channel
from .telegram import DryTelegram, Telegram, TelegramError

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"
CONFIG_PATH = ROOT / "config.yaml"

COMMANDS = [
    ("help", "إزاي تستخدم البوت"),
    ("queue", "حالة البوت والطابور"),
    ("now", "اكتبه قبل الرسالة عشان تنزل فوراً"),
    ("raw", "اكتبه قبل الرسالة عشان تنزل زي ما هي"),
    ("rich", "اكتبه قبل الرسالة عشان كل منتج ياخد بوست"),
    ("pause", "وقف النشر"),
    ("resume", "رجع النشر"),
    ("clear", "فضي الطابور"),
    ("wa", "عروض النهارده جاهزة لواتساب"),
    ("test", "شوف شكل البوست"),
]

HELP = """أهلاً يا أبو عبدالله 👋
ابعتلي أي لينك أو رسالة فيها لينكات أمازون، وأنا أحولها للتاج بتاعك وأنزلها على القناة.

لينك منتج لوحده: بعمله بوست بالسعر والخصم والصورة.
لينك فيه عروض كتير (بحث أو قسم): بجيب أحسن العروض اللي فيه.
لينك صفحة عروض أو إيفنت: بنزله بوست واحد باللينك بتاعك.
رسالة فيها كلام ولينكات (زي رسالة الجروب): بنزلها زي ما هي بعد ما أغير كل اللينكات للتاج بتاعك.

البوستات بتدخل طابور وبتنزل واحد واحد عشان القناة متتزحمش.
اكتب /now في أول الرسالة: تنزل فوراً.
اكتب /raw في أول الرسالة: تنزل زي ما هي.
اكتب /rich في أول الرسالة: كل منتج ياخد بوست لوحده بالسعر.
الأمر /queue: حالة البوت والطابور.
الأمر /pause أو /resume: وقف أو رجع النشر.
الأمر /clear: يفضي الطابور.
الأمر /wa: يبعتلك عروض النهارده مكتوبة لواتساب، تنسخها وتنزلها.
الأمر /test: يبعتلك شكل بوست تجريبي.

خلي بالك: البوت بيقرا الرسائل كل نص ساعة تقريباً، فالرد مش هيبقى فوري."""

FLAG = re.compile(r"^\s*/(now|raw|rich)(?:@\w+)?\b\s*", re.I)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    else:
        state = {}
    state.setdefault("tg_offset", None)
    state.setdefault("queue", [])
    state.setdefault("posted", {})
    state.setdefault("day", {"date": None, "count": 0})
    state.setdefault("last_post_at", None)
    state.setdefault("last_auto_search_at", None)
    state.setdefault("api", {"ok": None, "error": None, "checked_at": None})
    state.setdefault("notified", {})
    state.setdefault("paused", False)
    state.setdefault("sources", {})
    return state


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    tmp.replace(path)


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def before_footer(text: str) -> str:
    """A channel post without the footer that follows a separator line."""
    kept = []
    for line in (text or "").splitlines():
        if SEPARATOR.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def link_key(link) -> str:
    """What makes two posts 'the same deal': the product, or the page for a collection."""
    if link.asin:
        return link.asin
    path = re.sub(r"^/-/(?:en|ar)", "", urlsplit(link.resolved).path).rstrip("/")
    return "page:" + (path or "/")


class DealsBot:
    def __init__(self, cfg, state, tg, amazon, channel, admins, session=None,
                 now=None, state_path: Path | None = STATE_PATH):
        self.cfg = cfg
        self.state = state
        self.tg = tg
        self.amazon = amazon
        self.channel = channel
        self.admins = admins
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Mozilla/5.0 (deals-bot link resolver)")
        self._now = now
        self.state_path = state_path
        self.tz = ZoneInfo(cfg["posting"]["timezone"])
        self.tag = cfg["store"]["partner_tag"]
        self.api_dead = amazon is None

    # --- helpers ------------------------------------------------------------------

    def now(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    def save(self):
        if self.state_path:
            save_state(self.state, self.state_path)

    def notify(self, text: str, key: str | None = None, every_hours: float = 24):
        """Message the admins; with a key, at most once per every_hours."""
        if key:
            last = parse_time(self.state["notified"].get(key))
            if last and self.now() - last < timedelta(hours=every_hours):
                return
            self.state["notified"][key] = self.now().isoformat()
        for admin in self.admins:
            try:
                self.tg.send_text(admin, text, preview=False)
            except TelegramError as exc:
                print(f"could not notify {admin}: {exc}", file=sys.stderr)

    def api(self, fn, *args, **kwargs):
        """Call the Amazon API; None when it is closed or busy."""
        if self.api_dead:
            return None
        try:
            result = fn(*args, **kwargs)
        except ApiUnavailable as exc:
            self.api_dead = True
            self.state["api"] = {"ok": False, "error": str(exc)[:300],
                                 "checked_at": self.now().isoformat()}
            self.notify(
                "تنبيه ⚠ أمازون رافضة طلبات الـ Creators API دلوقتي.\n"
                "السبب غالباً إن الحساب محتاج 10 مبيعات مؤهلة في آخر 30 يوم، أو إن بيانات الـ API غلط.\n"
                "البوت شغال عادي في تحويل اللينكات، بس من غير أسعار وبحث أوتوماتيك لحد ما الـ API يفتح.\n"
                f"تفاصيل الخطأ: <code>{fmt.esc(str(exc)[:200])}</code>",
                key="api_down",
            )
            return None
        except ApiThrottled:
            return None
        self.state["api"] = {"ok": True, "error": None, "checked_at": self.now().isoformat()}
        self.state["notified"].pop("api_down", None)
        return result

    def enqueue(self, item: dict, priority: int):
        item.setdefault("id", uuid.uuid4().hex[:10])
        item["priority"] = priority
        item["created_at"] = self.now().isoformat()
        self.state["queue"].append(item)

    def queued_asins(self) -> set[str]:
        return {i["deal"]["asin"] for i in self.state["queue"] if i.get("kind") == "deal"}

    # --- inbox ----------------------------------------------------------------------

    def process_inbox(self):
        if not self.state.get("commands_set"):
            try:
                self.tg.set_commands(COMMANDS)
                self.state["commands_set"] = True
            except TelegramError as exc:
                print(f"setMyCommands failed: {exc}", file=sys.stderr)

        offset = self.state["tg_offset"]
        updates = self.tg.get_updates(offset + 1 if offset is not None else None)
        for update in sorted(updates, key=lambda u: u["update_id"]):
            self.state["tg_offset"] = update["update_id"]
            msg = update.get("message")
            if not msg or msg.get("chat", {}).get("type") != "private":
                self.save()
                continue
            user = msg.get("from", {}).get("id")
            chat = msg["chat"]["id"]
            if not self.admins:
                self.tg.send_text(
                    chat,
                    f"رقمك على تليجرام هو: <code>{user}</code>\n"
                    "حطه في الـ Secret اللي اسمه TELEGRAM_ADMIN_IDS عشان البوت يسمع كلامك.",
                    preview=False,
                )
            elif user in self.admins:
                try:
                    self.handle_message(msg, chat)
                except Exception as exc:  # keep going with the next message
                    self.tg.send_text(chat, f"حصلت مشكلة في الرسالة دي ❌\n<code>{fmt.esc(str(exc)[:300])}</code>",
                                      preview=False)
            self.save()

    def handle_message(self, msg: dict, chat):
        text = msg.get("text") or msg.get("caption") or ""
        entities = msg.get("entities") or msg.get("caption_entities") or []
        photo = msg["photo"][-1]["file_id"] if msg.get("photo") else None

        if not text.strip() and msg.get("media_group_id"):
            return None  # the other photos of an album; the captioned one carries the links
        command = text.strip().split()[0].lower().split("@")[0] if text.strip().startswith("/") else ""
        if command in ("/start", "/help"):
            return self.tg.send_text(chat, HELP, preview=False)
        if command == "/queue":
            return self.tg.send_text(chat, self.status_text(), preview=False)
        if command in ("/pause", "/resume"):
            self.state["paused"] = command == "/pause"
            word = "وقفت النشر ⏸" if self.state["paused"] else "رجعت النشر ▶"
            return self.tg.send_text(chat, f"تمام، {word}", preview=False)
        if command == "/clear":
            count = len(self.state["queue"])
            self.state["queue"] = []
            return self.tg.send_text(chat, f"تمام، فضيت الطابور ({count} بوست) 🧹", preview=False)
        if command == "/wa":
            return self.send_whatsapp_digest(chat)
        if command == "/test":
            sample = Deal(asin="B0TEST0000", title="Anker Soundcore Life P3i Hybrid ANC Earbuds, 36H Playtime",
                          url="https://www.amazon.eg/dp/B0TEST0000?tag=" + self.tag,
                          price=1299.0, old_price=1899.0, saving=600.0, saving_pct=32,
                          rating=4.4, reviews=2345)
            body, button = fmt.deal_post(sample, self.cfg)
            return self.tg.send_text(chat, "ده شكل البوست 👇\n\n" + body, button=button, preview=False)

        flags = set()
        while True:
            m = FLAG.match(text)
            if not m:
                break
            flags.add(m.group(1).lower())
            shift = len(text[:m.end()].encode("utf-16-le")) // 2
            text = text[m.end():]
            entities = [dict(e, offset=e["offset"] - shift) for e in entities
                        if e["offset"] >= shift and e.get("type") != "bot_command"]
        if not text.strip():
            return self.tg.send_text(chat, "ابعت اللينكات أو الرسالة بعد الأمر في نفس الرسالة 🙏", preview=False)

        urls = find_urls(text, entities)
        if not urls:
            return self.tg.send_text(chat, "مش لاقي لينكات أمازون في الرسالة دي 🤔", preview=False)

        report, link_map = analyse(urls, self.tag, self.session)
        if self.cfg["posting"].get("keep_other_links"):
            for url in report.other:
                link_map[url] = url
        if not report.amazon:
            return self.tg.send_text(
                chat, "اللينكات دي مش لأمازون مصر، فمش هقدر أحط عليها التاج بتاعك 🤷", preview=False)

        leftover = URL_IN_TEXT.sub("", text)
        leftover = re.sub(r"https?://\S+", "", leftover).strip()
        has_text = len(re.sub(r"\s+", " ", leftover)) >= 25
        headline = leftover if 0 < len(leftover) < 25 else None

        items: list[dict] = []
        notes: list[str] = []
        text_mode = self.cfg.get("inbox", {}).get("text_messages", "offer")
        raw = "raw" in flags or (has_text and "rich" not in flags and text_mode == "raw")
        as_offer = not raw and has_text and "rich" not in flags and text_mode == "offer"

        if as_offer:
            items.append(self.build_offer_item(text, entities, photo, report, link_map))
        elif raw:
            body = fmt.convert_message(text, entities, link_map, self.cfg)
            only = report.amazon[0].tagged if len(report.amazon) == 1 else None
            items.append({"kind": "html", "html": body, "photo": photo,
                          "button": ["اطلبه من أمازون 🛒", only] if only else None})
        else:
            products = [l for l in report.amazon if l.kind == "product"]
            pages = [l for l in report.amazon if l.kind in ("search", "node")]
            collections = [l for l in report.amazon if l.kind == "collection"]

            if products:
                deals = self.api(self.amazon.get, [p.asin for p in products]) if not self.api_dead else None
                found = {d.asin: d for d in deals or []}
                for p in products:
                    deal = found.get(p.asin)
                    if deal and deal.in_stock:
                        items.append({"kind": "deal", "deal": deal.to_dict(), "chosen": True})
                    else:
                        if deal and not deal.in_stock:
                            notes.append(f"المنتج {p.asin} شكله خلص من المخزون، نزلته لينك بس")
                        items.append({"kind": "link", "url": p.tagged, "headline": headline})

            for page in pages:
                deals = self.search_page(page)
                if deals:
                    items += [{"kind": "deal", "deal": d.to_dict(), "chosen": True} for d in deals]
                else:
                    items.append({"kind": "collection", "url": page.tagged, "headline": headline})

            for col in collections:
                items.append({"kind": "collection", "url": col.tagged, "headline": headline})

        if photo and items and not raw and not items[0].get("photo"):
            items[0]["photo"] = photo  # a picture you send goes with the first post

        if report.other and not self.cfg["posting"].get("keep_other_links"):
            notes.append(f"شلت {len(report.other)} لينك مش أمازون من الرسالة")
        if report.failed:
            notes.append(f"فيه {len(report.failed)} لينك مقصّر مفتحش معايا فشلته")
        fallback = any(i["kind"] == "link" for i in items) or (
            not raw and any(l.kind in ("search", "node") for l in report.amazon)
            and not any(i["kind"] == "deal" for i in items))
        if self.api_dead and fallback:
            notes.append("الـ API مش شغال، فالبوستات نازلة باللينك من غير سعر")

        if "now" in flags:
            posted = sum(1 for item in items if self.publish(item, counted=False))
            summary = f"تمام ✅ نزلت {posted} بوست على القناة دلوقتي"
        else:
            for item in items:
                self.enqueue(item, priority=0)
            ahead = len(self.state["queue"]) - len(items)
            summary = f"تمام ✅ ضفت {len(items)} بوست للطابور، وهينزلوا واحد ورا التاني ⏳"
            if ahead:
                summary += f"\nقبلهم {ahead} بوست في الطابور"
        self.tg.send_text(chat, "\n".join([summary] + notes), preview=False)

    def send_whatsapp_digest(self, chat):
        """Today's posts as WhatsApp text, ready to copy into a channel or group."""
        today = self.now().astimezone(self.tz).date()
        posts = [e["wa"] for e in self.state.get("log", [])
                 if parse_time(e["at"]).astimezone(self.tz).date() == today]
        if not posts:
            return self.tg.send_text(chat, "لسه مفيش بوستات نزلت النهارده 🤷", preview=False)
        self.tg.send_text(chat, f"عروض النهارده جاهزة لواتساب 👇 ({len(posts)} بوست)\n"
                                "انسخ كل رسالة وابعتها في القناة أو الجروب.", preview=False)
        chunk = ""
        for post in posts:
            piece = post if not chunk else "\n\n━━━━━━━━\n\n" + post
            if len(chunk) + len(piece) > 3800:
                self.tg.send_text(chat, fmt.esc(chunk), preview=False)
                chunk = post
            else:
                chunk += piece
        if chunk:
            self.tg.send_text(chat, fmt.esc(chunk), preview=False)

    def build_offer_item(self, text, entities, photo, report, link_map) -> dict:
        """One queue item from a person's deal post: our template, or their text if it lists many products."""
        products = [l for l in report.amazon if l.kind == "product"]
        asins = list(dict.fromkeys(p.asin for p in products))
        if len(asins) > 1:
            body = fmt.convert_message(text, entities, link_map, self.cfg)
            return {"kind": "html", "html": body, "photo": photo, "button": None,
                    "key": "multi:" + ",".join(sorted(asins)), "score": 0}
        primary = products[0] if products else report.amazon[0]
        offer = parse_offer(text)
        return {"kind": "offer", "offer": offer.to_dict(), "url": primary.tagged, "asin": primary.asin,
                "photo": photo, "collection": primary.kind != "product",
                "key": link_key(primary), "score": offer.score}

    def seen_recently(self, key: str, days: int) -> bool:
        record = self.state["posted"].get(key)
        return bool(record) and self.now() - parse_time(record["at"]) < timedelta(days=days)

    def collect_sources(self):
        """New deal posts from the channels in config.yaml go into the queue, best discounts first."""
        src = self.cfg.get("sources", {})
        if not src.get("enabled") or not src.get("channels"):
            return
        days = src.get("repost_after_days", 7)
        queued = {i.get("key") for i in self.state["queue"]}
        for entry in src["channels"]:
            name = channel_name(entry)
            try:
                posts = fetch_channel(name, self.session)
            except Exception as exc:  # one channel down must not stop the rest
                print(f"could not read {name}: {exc}", file=sys.stderr)
                continue
            if not posts:
                continue
            last = self.state["sources"].get(name)
            new = posts[-src.get("first_run_take", 2):] if last is None else [p for p in posts if p.post_id > last]
            self.state["sources"][name] = max(last or 0, posts[-1].post_id)
            for post in new[-src.get("max_new_per_channel", 10):]:
                text = before_footer(post.text)
                urls = find_urls(text)
                for href, label in zip(post.links, post.link_texts):
                    if label.strip() and label.strip() in text and href not in urls:
                        urls.append(href)  # a link hidden behind words in the post
                if not urls:
                    continue
                report, link_map = analyse(urls, self.tag, self.session)
                if not report.amazon:
                    continue
                item = self.build_offer_item(text, None, post.photo, report, link_map)
                min_pct = src.get("min_discount_percent", 0)
                if min_pct and (item.get("offer", {}).get("pct") or 0) < min_pct:
                    continue
                if item["key"] in queued or self.seen_recently(item["key"], days):
                    continue
                item["source"] = f"{name}/{post.post_id}"
                if name in src.get("crop_card", []) and item.get("photo"):
                    item["photo_crop"] = "card"
                self.enqueue(item, priority=1)
                queued.add(item["key"])
        self.trim_source_queue()
        self.save()

    def trim_source_queue(self):
        src = self.cfg.get("sources", {})
        max_age = timedelta(hours=src.get("max_age_hours", 8))
        keep, from_sources = [], []
        for item in self.state["queue"]:
            if not item.get("source"):
                keep.append(item)
            elif self.now() - parse_time(item["created_at"]) <= max_age:
                from_sources.append(item)
        from_sources.sort(key=lambda i: (-i.get("score", 0), i["created_at"]))
        self.state["queue"] = keep + from_sources[: src.get("max_queue", 12)]

    def search_page(self, page) -> list[Deal]:
        if self.api_dead:
            return []
        inbox = self.cfg.get("inbox", {})
        deals = self.api(self.amazon.search, keywords=page.keywords, node=page.node, count=10) or []
        good = [d for d in deals if d.in_stock and d.price and
                (d.saving_pct or 0) >= inbox.get("min_saving_percent", 10)]
        good.sort(key=lambda d: d.saving_pct or 0, reverse=True)
        return good[: inbox.get("max_from_page", 8)]

    def status_text(self) -> str:
        self.roll_day()
        q = self.state["queue"]
        api = self.state["api"]
        api_word = {True: "شغال ✅", False: "مقفول ❌", None: "لسه متجربش"}[api.get("ok")]
        if self.amazon is None:
            api_word = "مش متظبط (مفيش بيانات Creators API)"
        last = parse_time(self.state["last_post_at"])
        last_txt = last.astimezone(self.tz).strftime("%I:%M %p %d/%m") if last else "لسه"
        lines = [
            "حالة البوت 📊",
            f"النشر: {'واقف ⏸' if self.state['paused'] else 'شغال ▶'}",
            f"في الطابور: {len(q)} بوست ({sum(1 for i in q if i['priority'] == 0)} منك، "
            f"{sum(1 for i in q if i['priority'] == 1)} أوتوماتيك)",
            f"منهم من القنوات: {sum(1 for i in q if i.get('source'))}",
            f"اتنشر النهارده: {self.state['day']['count']} من {self.cfg['posting']['max_posts_per_day']}",
            f"آخر بوست: {last_txt}",
            f"الـ Creators API: {api_word}",
        ]
        return "\n".join(lines)

    # --- automatic deals ---------------------------------------------------------------

    def recently_posted(self, deal: Deal, days: int) -> bool:
        record = self.state["posted"].get(deal.asin)
        if not record:
            return False
        if self.now() - parse_time(record["at"]) > timedelta(days=days):
            return False
        last_price = record.get("price")
        return not (last_price and deal.price and deal.price <= last_price * 0.95)

    def auto_search(self):
        auto = self.cfg.get("auto", {})
        if not auto.get("enabled") or self.api_dead:
            return
        last = parse_time(self.state["last_auto_search_at"])
        if last and self.now() - last < timedelta(hours=auto.get("every_hours", 3)):
            return
        if sum(1 for i in self.state["queue"] if i["priority"] == 1) >= auto.get("max_new_per_round", 4):
            return

        min_saving = auto.get("min_saving_percent", 20)
        min_price = int(auto.get("min_price_egp", 0) * 100) or None
        candidates: dict[str, Deal] = {}
        for search in auto.get("searches", []):
            deals = self.api(self.amazon.search,
                             keywords=search.get("keywords"), node=search.get("node"),
                             search_index=search.get("search_index"),
                             min_saving=search.get("min_saving_percent", min_saving),
                             min_rating=auto.get("min_rating") or None,
                             min_price=min_price, count=10)
            if self.api_dead:
                return
            for deal in deals or []:
                if not deal.in_stock or not deal.price or (deal.saving_pct or 0) < min_saving:
                    continue
                if auto.get("min_rating") and deal.rating and deal.rating < auto["min_rating"]:
                    continue
                best = candidates.get(deal.asin)
                if not best or (deal.saving_pct or 0) > (best.saving_pct or 0):
                    candidates[deal.asin] = deal
        self.state["last_auto_search_at"] = self.now().isoformat()

        ranked = sorted(candidates.values(),
                        key=lambda d: (d.saving_pct or 0) + 3 * (d.rating or 0), reverse=True)
        queued = self.queued_asins()
        added = 0
        for deal in ranked:
            if deal.asin in queued or self.recently_posted(deal, auto.get("repost_after_days", 7)):
                continue
            self.enqueue({"kind": "deal", "deal": deal.to_dict(), "chosen": False}, priority=1)
            added += 1
            if added >= auto.get("max_new_per_round", 4):
                break
        self.save()

    # --- publishing ---------------------------------------------------------------------

    def in_active_hours(self) -> bool:
        hour = self.now().astimezone(self.tz).hour
        start, end = self.cfg["posting"]["active_from"], self.cfg["posting"]["active_to"]
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def roll_day(self):
        today = self.now().astimezone(self.tz).date().isoformat()
        if self.state["day"]["date"] != today:
            self.state["day"] = {"date": today, "count": 0}

    def post_due(self):
        self.roll_day()
        posting = self.cfg["posting"]
        if self.state["paused"] or not self.state["queue"] or not self.in_active_hours():
            return
        if self.state["day"]["count"] >= posting["max_posts_per_day"]:
            return
        last = parse_time(self.state["last_post_at"])
        if last and self.now() - last < timedelta(minutes=posting["min_gap_minutes"] - 2):
            return

        self.trim_source_queue()
        self.state["queue"].sort(key=lambda i: (i["priority"], -i.get("score", 0), i["created_at"]))
        while self.state["queue"]:
            item = self.state["queue"][0]
            try:
                posted = self.publish(item)
            except TelegramError as exc:
                self.notify(
                    "مش قادر أنزل على القناة ❌\n"
                    "اتأكد إن البوت أدمن في القناة وإن TELEGRAM_CHANNEL مكتوب صح.\n"
                    f"<code>{fmt.esc(str(exc)[:200])}</code>",
                    key="channel_error", every_hours=6,
                )
                return
            except Exception as exc:  # a broken item must not block the queue
                self.notify(f"بوست في الطابور فيه مشكلة وشلته ❌\n<code>{fmt.esc(str(exc)[:200])}</code>",
                            key=f"item_error_{item.get('id')}")
                posted = False
            self.state["queue"].pop(0)
            self.save()
            if posted:
                return

    def refresh(self, deal: Deal) -> tuple[Deal | None, bool]:
        """Fresh price before posting. Returns (deal or None when gone, price_is_stale)."""
        age = self.now() - parse_time(deal.fetched_at)
        if age < timedelta(minutes=45):
            return deal, False
        fresh = None if self.api_dead else self.api(self.amazon.get, [deal.asin])
        if fresh is None:  # API closed or busy: keep it, but an old price is not shown
            return deal, age > timedelta(hours=3)
        return (fresh[0] if fresh else None), False

    def publish(self, item: dict, counted: bool = True) -> bool:
        if item.get("photo_crop") and str(item.get("photo") or "").startswith("http"):
            item = dict(item, photo=self.cropped_photo(item["photo"]))  # the queue keeps the link
        kind = item["kind"]
        if kind == "deal":
            deal, stale = self.refresh(Deal.from_dict(item["deal"]))
            if deal is None or not deal.in_stock:
                return False
            if not item.get("chosen") and stale:
                return False
            if not item.get("chosen"):
                if (deal.saving_pct or 0) < self.cfg["auto"].get("min_saving_percent", 20) * 0.8:
                    return False
            if stale:
                body, button = fmt.link_only_post(deal.url, self.cfg)
                self.send(body, button, preview=True)
            else:
                body, button = fmt.deal_post(deal, self.cfg)
                self.send(body, button, photo=item.get("photo") or deal.image, preview=False)
            self.state["posted"][deal.asin] = {"at": self.now().isoformat(), "price": deal.price}
        elif kind == "html":
            if item.get("key"):
                self.state["posted"][item["key"]] = {"at": self.now().isoformat(), "price": None}
            button = tuple(item["button"]) if item.get("button") else None
            photo = item.get("photo")
            if photo and fmt.visible_length(item["html"]) > 1024:
                photo = None
            self.send(item["html"], button, photo=photo, preview=True)
        elif kind == "link":
            body, button = fmt.link_only_post(item["url"], self.cfg, item.get("headline"))
            self.send(body, button, photo=item.get("photo"), preview=True, preview_url=item["url"])
        elif kind == "collection":
            body, button = fmt.collection_post(item["url"], self.cfg, item.get("headline"))
            self.send(body, button, photo=item.get("photo"), preview=True, preview_url=item["url"])
        elif kind == "offer":
            if not self.publish_offer(item):
                return False
        else:
            return False

        self.state["last_post_at"] = self.now().isoformat()
        if counted:
            self.roll_day()
            self.state["day"]["count"] += 1
        return True

    def publish_offer(self, item: dict) -> bool:
        """A deal read from a post. With the API open, Amazon's own price replaces the post's."""
        offer = Offer.from_dict(item["offer"])
        deal = None
        if item.get("asin") and not self.api_dead:
            found = self.api(self.amazon.get, [item["asin"]])
            if found is not None and (not found or not found[0].in_stock):
                if item.get("source"):
                    return False  # gone or sold out on Amazon
            elif found:
                deal = found[0]
        if deal:
            body, button = fmt.deal_post(deal, self.cfg)
            self.send(body, button, photo=item.get("photo") or deal.image, preview=False)
        else:
            body, button = fmt.offer_post(offer, item["url"], self.cfg, collection=item.get("collection", False))
            self.send(body, button, photo=item.get("photo"), preview=True, preview_url=item["url"])
        self.state["posted"][item["key"]] = {"at": self.now().isoformat(),
                                            "price": deal.price if deal else offer.price}
        return True

    def send(self, body: str, button, photo: str | None = None, preview: bool = True,
             preview_url: str | None = None):
        wa = fmt.to_whatsapp(body)
        self.state.setdefault("log", []).append({"at": self.now().isoformat(), "wa": wa})
        self.state["log"] = self.state["log"][-60:]
        buttons = [tuple(button)] if button else []
        if self.cfg["posting"].get("whatsapp_button", True):
            buttons.append(fmt.share_button(wa))
        try:
            return self._deliver(body, buttons or None, photo, preview, preview_url)
        except TelegramError as exc:
            if len(buttons) > 1 and any(w in str(exc).lower() for w in ("button", "url", "markup")):
                return self._deliver(body, buttons[:1], photo, preview, preview_url)  # no share button
            raise

    def download_photo(self, url: str):
        """Telegram cannot fetch some picture links itself (like t.me's CDN), so we send the bytes."""
        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                return resp.content
        except Exception as exc:
            print(f"could not download photo: {exc}", file=sys.stderr)
        return url

    def cropped_photo(self, url: str):
        """Only the product from a channel's branded picture; no picture rather than their logo."""
        data = self.download_photo(url)
        if isinstance(data, bytes):
            crop = crop_card(data)
            if crop:
                return crop
        print(f"no product card found, posting without the picture: {url[:80]}", file=sys.stderr)
        return None

    def _deliver(self, body: str, button, photo, preview: bool, preview_url=None):
        if photo:
            if isinstance(photo, str) and photo.startswith("http"):
                photo = self.download_photo(photo)
            try:
                return self.tg.send_photo(self.channel, photo, body, button=button)
            except TelegramError as exc:
                print(f"photo failed, sending text: {exc}", file=sys.stderr)
        try:
            return self.tg.send_text(self.channel, body, button=button, preview=preview,
                                     preview_url=preview_url)
        except TelegramError as exc:
            if "parse" not in str(exc).lower():
                raise
            return self.tg.send_text(self.channel, fmt.strip_html(body), button=button,
                                     preview=preview, html=False)

    # --- housekeeping -----------------------------------------------------------------

    def prune(self):
        cutoff = self.now() - timedelta(days=60)
        self.state["posted"] = {a: r for a, r in self.state["posted"].items()
                                if parse_time(r["at"]) > cutoff}
        stale = self.now() - timedelta(days=3)
        self.state["queue"] = [i for i in self.state["queue"] if parse_time(i["created_at"]) > stale]

    def run(self, loop_minutes: float = 0, every_seconds: int = 60,
            sleep=time.sleep, clock=time.monotonic):
        """One round, or (loop_minutes > 0) a round every minute until the time is up:
        your messages get answered fast and posts go out on time inside one job."""
        end = clock() + loop_minutes * 60
        sources_every = self.cfg.get("sources", {}).get("check_every_minutes", 10) * 60
        last_sources = None
        while True:
            try:
                self.process_inbox()
                if last_sources is None or clock() - last_sources >= sources_every:
                    last_sources = clock()
                    self.collect_sources()
                self.auto_search()
                self.post_due()
            except Exception as exc:
                if not loop_minutes:
                    raise
                print(f"round failed, trying again next minute: {exc}", file=sys.stderr)
            finally:
                self.prune()
                self.save()
            if clock() + every_seconds >= end:
                return
            sleep(every_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Amazon Egypt deals bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="print posts instead of sending them, and do not save state")
    parser.add_argument("--loop-minutes", type=float, default=0,
                        help="keep running this many minutes, one round every minute")
    args = parser.parse_args(argv)

    cfg = load_config()
    state = load_state()
    channel = os.environ.get("TELEGRAM_CHANNEL", "").strip()
    admins = [int(x) for x in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ADMIN_IDS", "")) if x.strip()]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    if args.dry_run:
        tg = DryTelegram()
    else:
        if not token or not channel:
            sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL secrets are required")
        tg = Telegram(token)

    try:
        amazon = AmazonClient.from_env(cfg)
    except Exception as exc:  # a bad credential version raises here
        print(f"Amazon API client not created: {exc}", file=sys.stderr)
        amazon = None

    bot = DealsBot(cfg, state, tg, amazon, channel or "@preview", admins,
                   state_path=None if args.dry_run else STATE_PATH)
    bot.run(loop_minutes=args.loop_minutes)


if __name__ == "__main__":
    main()
