"""Our wrapper against the real python-amazon-paapi client, with only the HTTP call replaced."""

import pytest
from creatorsapi_python_sdk.models.get_items_response_content import GetItemsResponseContent
from creatorsapi_python_sdk.models.search_items_response_content import SearchItemsResponseContent

from bot.amazon_api import AmazonClient, ApiUnavailable

from .conftest import TAG

ITEM = {
    "asin": "B0REAL0001",
    "detailPageURL": f"https://www.amazon.eg/dp/B0REAL0001?tag={TAG}&linkCode=ogi",
    "itemInfo": {"title": {"displayValue": "Xiaomi Redmi Buds 6"}},
    "offersV2": {"listings": [{
        "availability": {"type": "IN_STOCK"}, "isBuyBoxWinner": True,
        "price": {"money": {"amount": 999.0, "currency": "EGP"},
                  "savingBasis": {"money": {"amount": 1499.0, "currency": "EGP"}},
                  "savings": {"money": {"amount": 500.0}, "percentage": 33}},
    }]},
}


@pytest.fixture
def client(cfg):
    return AmazonClient("id", "secret", "3.2", TAG, cfg["store"]["marketplace"])


def test_search_sends_our_filters_to_amazon_eg(client, monkeypatch):
    seen = {}

    def fake_call(operation, **kwargs):
        seen.update(kwargs)
        return SearchItemsResponseContent.from_dict({"searchResult": {"items": [ITEM]}})

    monkeypatch.setattr(client._api, "_call", fake_call)
    deals = client.search(keywords="earbuds", min_saving=20, min_rating=4, min_price=15000, count=10)
    request = seen["search_items_request_content"]
    assert client._api.marketplace == "www.amazon.eg"
    assert request.partner_tag == TAG and request.keywords == "earbuds"
    assert request.min_saving_percent == 20 and request.min_reviews_rating == 4
    assert request.min_price == 15000 and request.item_count == 10
    assert deals[0].asin == "B0REAL0001" and deals[0].saving_pct == 33


def test_get_items(client, monkeypatch):
    monkeypatch.setattr(client._api, "_call", lambda op, **kw: GetItemsResponseContent.from_dict(
        {"itemsResult": {"items": [ITEM]}}))
    assert client.get(["B0REAL0001"])[0].price == 999.0


def test_not_eligible_becomes_api_unavailable(client, monkeypatch):
    from amazon_creatorsapi import errors

    def refuse(op, **kw):
        raise errors.AccessDeniedError("AssociateNotEligible")

    monkeypatch.setattr(client._api, "_call", refuse)
    with pytest.raises(ApiUnavailable):
        client.search(keywords="x")


def test_no_results_is_empty_list(client, monkeypatch):
    monkeypatch.setattr(client._api, "_call",
                        lambda op, **kw: SearchItemsResponseContent.from_dict({}))
    assert client.search(keywords="nothing") == []
