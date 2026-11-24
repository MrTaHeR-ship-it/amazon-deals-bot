import copy
from datetime import datetime, timedelta, timezone

import pytest

from bot.amazon_api import ApiUnavailable, Deal
from bot.main import DealsBot, load_config, load_state
from bot.telegram import DryTelegram

TAG = "tahergalaleld-21"


class FakeResponse:
    def __init__(self, status, location=None):
        self.status_code = status
        self.headers = {"location": location} if location else {}

    def close(self):
        pass


class FakeSession:
    """Short links resolve through this table; nothing touches the network."""

    def __init__(self, table=None):
        self.table = table or {}
        self.headers = {}
        self.calls = []

    def head(self, url, allow_redirects=False, timeout=10):
        self.calls.append(url)
        if url in self.table:
            return FakeResponse(301, self.table[url])
        return FakeResponse(404)

    def get(self, url, allow_redirects=False, timeout=10, stream=True):
        return self.head(url)


def make_deal(asin="B0AAAAAAAA", price=1000.0, old=1500.0, pct=33, **kw):
    return Deal(asin=asin, title=kw.pop("title", f"Product {asin}"),
                url=f"https://www.amazon.eg/dp/{asin}?tag={TAG}",
                price=price, old_price=old, saving=(old - price) if old else None,
                saving_pct=pct, image=f"https://m.media-amazon.com/images/{asin}.jpg",
                rating=kw.pop("rating", 4.5), reviews=kw.pop("reviews", 300), **kw)


class FakeAmazon:
    def __init__(self, deals=None, search_results=None, dead=False):
        self.deals = {d.asin: d for d in (deals or [])}
        self.search_results = search_results or []
        self.dead = dead
        self.searches = []
        self.gets = []

    def get(self, asins):
        if self.dead:
            raise ApiUnavailable("AccessDeniedError: AssociateNotEligible")
        self.gets.append(list(asins))
        return [copy.deepcopy(self.deals[a]) for a in asins if a in self.deals]

    def search(self, **kw):
        if self.dead:
            raise ApiUnavailable("AccessDeniedError: AssociateNotEligible")
        self.searches.append(kw)
        return copy.deepcopy(self.search_results)


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def clock():
    # 2 PM Cairo time, inside posting hours
    return Clock(datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def make_bot(cfg, clock, tmp_path):
    def factory(amazon=None, updates=None, session=None, admins=(111,)):
        state = load_state(tmp_path / "state.json")
        tg = DryTelegram(updates)
        bot = DealsBot(cfg, state, tg, amazon, "@channel", list(admins),
                       session=session or FakeSession(), now=clock,
                       state_path=tmp_path / "state.json")
        return bot, tg
    return factory


def private_msg(update_id, text, user=111, entities=None, photo=None, caption=False):
    msg = {"message_id": update_id, "from": {"id": user},
           "chat": {"id": user, "type": "private"}}
    if caption:
        msg["caption"] = text
        if entities:
            msg["caption_entities"] = entities
    else:
        msg["text"] = text
        if entities:
            msg["entities"] = entities
    if photo:
        msg["photo"] = [{"file_id": "small"}, {"file_id": photo}]
    return {"update_id": update_id, "message": msg}
