"""Product data from Amazon's official Creators API, turned into plain Deal records.

The API only opens for Associates accounts with enough recent sales. When it is
closed, the bot keeps working in link-only mode and this module reports why.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .links import product_url


@dataclass
class Deal:
    asin: str
    title: str
    url: str
    price: float | None = None
    old_price: float | None = None
    saving: float | None = None
    saving_pct: int | None = None
    currency: str = "EGP"
    image: str | None = None
    rating: float | None = None
    reviews: int | None = None
    in_stock: bool = True
    deal_badge: str | None = None
    deal_end: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Deal":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class ApiUnavailable(Exception):
    """The API refused us for a reason that will not fix itself this run."""


class ApiThrottled(Exception):
    pass


def _get(obj, *path):
    for name in path:
        if obj is None:
            return None
        obj = getattr(obj, name, None)
    return obj


def item_to_deal(item, tag: str) -> Deal | None:
    asin = _get(item, "asin")
    title = _get(item, "item_info", "title", "display_value")
    if not asin or not title:
        return None

    listings = _get(item, "offers_v2", "listings") or []
    listing = next((l for l in listings if getattr(l, "is_buy_box_winner", False)), None)
    listing = listing or (listings[0] if listings else None)

    price = _get(listing, "price", "money", "amount")
    old_price = _get(listing, "price", "saving_basis", "money", "amount")
    saving = _get(listing, "price", "savings", "money", "amount")
    pct = _get(listing, "price", "savings", "percentage")
    currency = _get(listing, "price", "money", "currency") or "EGP"

    if price is not None and old_price and old_price > price:
        saving = saving if saving is not None else round(old_price - price, 2)
        pct = pct if pct is not None else round((old_price - price) / old_price * 100)
    stock_type = (_get(listing, "availability", "type") or "").upper()
    in_stock = listing is not None and price is not None and not (
        "OUT" in stock_type or "UNAVAILABLE" in stock_type
    )

    url = _get(item, "detail_page_url") or product_url(asin, tag)
    if f"tag={tag}" not in url:
        url = product_url(asin, tag)

    image = (
        _get(item, "images", "primary", "large", "url")
        or _get(item, "images", "primary", "hi_res", "url")
        or _get(item, "images", "primary", "medium", "url")
    )

    return Deal(
        asin=asin,
        title=title.strip(),
        url=url,
        price=float(price) if price is not None else None,
        old_price=float(old_price) if old_price else None,
        saving=float(saving) if saving else None,
        saving_pct=int(round(pct)) if pct else None,
        currency=currency,
        image=image,
        rating=_get(item, "customer_reviews", "star_rating", "value"),
        reviews=int(_get(item, "customer_reviews", "count") or 0) or None,
        in_stock=in_stock,
        deal_badge=_get(listing, "deal_details", "badge"),
        deal_end=_get(listing, "deal_details", "end_time"),
    )


class AmazonClient:
    """Thin wrapper over python-amazon-paapi's Creators API client."""

    def __init__(self, credential_id: str, credential_secret: str, version: str,
                 tag: str, marketplace: str, language: str | None = None):
        from amazon_creatorsapi import AmazonCreatorsApi  # imported lazily

        self.tag = tag
        self.language = language
        self._api = AmazonCreatorsApi(
            credential_id=credential_id,
            credential_secret=credential_secret,
            version=version,
            tag=tag,
            marketplace=marketplace,
        )

    @classmethod
    def from_env(cls, cfg: dict) -> "AmazonClient | None":
        cid = os.environ.get("AMAZON_CREDENTIAL_ID", "").strip()
        secret = os.environ.get("AMAZON_CREDENTIAL_SECRET", "").strip()
        version = os.environ.get("AMAZON_CREDENTIAL_VERSION", "").strip() or "3.2"
        if not cid or not secret:
            return None
        store = cfg["store"]
        return cls(cid, secret, version, store["partner_tag"], store["marketplace"],
                   store.get("language") or None)

    def _wrap(self, fn, *args, **kwargs):
        from amazon_creatorsapi import errors

        try:
            return fn(*args, **kwargs)
        except errors.ItemsNotFoundError:
            return None
        except errors.TooManyRequestsError as exc:
            raise ApiThrottled(str(exc)) from exc
        except (errors.AssociateValidationError, errors.AuthenticationError,
                errors.AccessDeniedError) as exc:
            raise ApiUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except ValueError as exc:  # bad credential version and similar
            raise ApiUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def _langs(self):
        return [self.language] if self.language else None

    def search(self, keywords: str | None = None, node: str | None = None,
               search_index: str | None = None, min_saving: int | None = None,
               min_rating: int | None = None, min_price: int | None = None,
               max_price: int | None = None, count: int = 10, page: int = 1) -> list[Deal]:
        kwargs = dict(
            keywords=keywords,
            browse_node_id=node,
            search_index=search_index,
            item_count=count,
            item_page=page,
            min_saving_percent=min_saving or None,
            min_reviews_rating=min_rating or None,
            min_price=min_price or None,
            max_price=max_price or None,
            languages_of_preference=self._langs(),
        )
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        result = self._wrap(self._api.search_items, **kwargs)
        items = getattr(result, "items", None) or []
        return [d for d in (item_to_deal(i, self.tag) for i in items) if d]

    def get(self, asins: list[str]) -> list[Deal]:
        if not asins:
            return []
        result = self._wrap(self._api.get_items, list(asins),
                            languages_of_preference=self._langs())
        return [d for d in (item_to_deal(i, self.tag) for i in (result or [])) if d]
