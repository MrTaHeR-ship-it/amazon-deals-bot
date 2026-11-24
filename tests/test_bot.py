from datetime import timedelta

from .conftest import TAG, FakeAmazon, FakeSession, make_deal, private_msg

SHORT = {"https://amzn.eu/d/abc123": "https://www.amazon.eg/dp/B0SHORT001?tag=someone-21"}


def channel_posts(tg):
    return [m for m in tg.sent if m["chat"] == "@channel"]


def replies(tg):
    return [m["text"] for m in tg.sent if m["chat"] == 111]


def test_unknown_admin_gets_their_id(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "hi", user=999)], admins=())
    bot.run()
    assert "999" in tg.sent[0]["text"] and bot.state["tg_offset"] == 1


def test_strangers_are_ignored(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "https://www.amazon.eg/dp/B0AAAAAAAA", user=555)])
    bot.run()
    assert tg.sent == [] and bot.state["queue"] == []


def test_product_link_with_api_becomes_rich_post(make_bot):
    amazon = FakeAmazon(deals=[make_deal("B0SHORT001", 899.0, 1499.0, 40)])
    bot, tg = make_bot(amazon=amazon, session=FakeSession(SHORT),
                       updates=[private_msg(1, "amzn.eu/d/abc123")])
    bot.run()
    posts = channel_posts(tg)
    assert len(posts) == 1 and posts[0]["photo"].endswith("B0SHORT001.jpg")
    assert "خصم 40%" in posts[0]["text"] and f"tag={TAG}" in posts[0]["text"]
    assert "ضفت 1 بوست" in replies(tg)[0]
    assert "B0SHORT001" in bot.state["posted"]


def test_api_closed_falls_back_to_link_posts_and_warns_once(make_bot, clock):
    amazon = FakeAmazon(dead=True)
    bot, tg = make_bot(amazon=amazon, updates=[
        private_msg(1, "https://www.amazon.eg/dp/B0AAAAAAAA https://www.amazon.eg/dp/B0BBBBBBBB")])
    bot.run()
    texts = replies(tg)
    assert any("10 مبيعات" in t for t in texts)
    assert any("من غير سعر" in t for t in texts)
    post = channel_posts(tg)[0]
    assert "photo" not in post and post["preview"] is True
    assert f"https://www.amazon.eg/dp/B0AAAAAAAA?tag={TAG}" in post["text"]
    assert len(bot.state["queue"]) == 1  # second one waits for the next slot

    # next run 30 minutes later: posts the second, does not warn again (no API call needed)
    clock.advance(minutes=30)
    bot.tg.updates = []
    bot.api_dead = False
    bot.run()
    assert len(channel_posts(tg)) == 2
    assert sum("10 مبيعات" in t for t in replies(tg)) == 1


def test_forwarded_group_message_is_reposted_with_our_links(make_bot):
    text = ("عرض نار على باور بانك انكر 20000 مللي أمبير بخصم 35%\n"
            "اطلب من هنا: amzn.eu/d/abc123\n"
            "تابعونا على @otherdeals")
    bot, tg = make_bot(session=FakeSession(SHORT), updates=[private_msg(1, text, photo="PHOTO_ID", caption=True)])
    bot.run()
    post = channel_posts(tg)[0]
    assert post["photo"] == "PHOTO_ID"
    assert f"https://www.amazon.eg/dp/B0SHORT001?tag={TAG}" in post["text"]
    assert "someone-21" not in post["text"] and "otherdeals" not in post["text"]
    assert post["button"][1] == f"https://www.amazon.eg/dp/B0SHORT001?tag={TAG}"


def test_now_flag_posts_everything_immediately(make_bot):
    bot, tg = make_bot(updates=[private_msg(
        1, "/now https://www.amazon.eg/dp/B0AAAAAAAA\nhttps://www.amazon.eg/dp/B0BBBBBBBB")])
    bot.run()
    assert len(channel_posts(tg)) == 2 and bot.state["queue"] == []
    assert bot.state["day"]["count"] == 0  # /now posts do not use up the daily limit
    assert "نزلت 2 بوست" in replies(tg)[0]


def test_search_link_pulls_best_deals_from_it(make_bot):
    results = [make_deal("B0LOW00001", 900, 1000, 10), make_deal("B0HIGH0001", 500, 1000, 50),
               make_deal("B0MID00001", 700, 1000, 30), make_deal("B0NONE0001", 700, None, None)]
    amazon = FakeAmazon(search_results=results)
    bot, tg = make_bot(amazon=amazon, updates=[private_msg(
        1, "https://www.amazon.eg/s?k=power+bank&rh=n%3A123&tag=x-21")])
    bot.process_inbox()
    assert amazon.searches[0]["keywords"] == "power bank" and amazon.searches[0]["node"] == "123"
    asins = [i["deal"]["asin"] for i in bot.state["queue"]]
    assert asins == ["B0HIGH0001", "B0MID00001", "B0LOW00001"]


def test_deals_page_becomes_one_collection_post(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "https://www.amazon.eg/deals?ref_=nav_cs_gb")])
    bot.run()
    post = channel_posts(tg)[0]
    assert "شوف كل العروض" in post["text"] and f"tag={TAG}" in post["text"]


def test_queue_respects_gap_hours_and_daily_limit(make_bot, clock, cfg):
    bot, tg = make_bot()
    for i in range(3):
        bot.enqueue({"kind": "link", "url": f"https://www.amazon.eg/dp/B0Q000000{i}?tag={TAG}"}, priority=0)
    bot.post_due()
    bot.post_due()  # same time: gap not passed
    assert len(channel_posts(tg)) == 1
    clock.advance(minutes=30)
    bot.post_due()
    assert len(channel_posts(tg)) == 2

    clock.t = clock.t.replace(hour=1)  # 3 AM Cairo: outside posting hours
    bot.post_due()
    assert len(channel_posts(tg)) == 2

    clock.t = clock.t.replace(hour=12)
    bot.state["day"] = {"date": clock.t.date().isoformat(), "count": cfg["posting"]["max_posts_per_day"]}
    bot.post_due()
    assert len(channel_posts(tg)) == 2


def test_auto_search_picks_new_good_deals_and_skips_recent(make_bot, clock):
    results = [make_deal("B0AUTO0001", 500, 1000, 50), make_deal("B0AUTO0002", 800, 1000, 20),
               make_deal("B0AUTO0003", 950, 1000, 5), make_deal("B0POSTED01", 600, 1000, 40)]
    amazon = FakeAmazon(search_results=results)
    bot, tg = make_bot(amazon=amazon)
    bot.state["posted"]["B0POSTED01"] = {"at": clock.t.isoformat(), "price": 600}
    bot.auto_search()
    asins = [i["deal"]["asin"] for i in bot.state["queue"]]
    assert asins == ["B0AUTO0001", "B0AUTO0002"]
    calls = len(amazon.searches)
    bot.auto_search()  # too soon for another round
    assert len(amazon.searches) == calls


def test_stale_deal_is_refreshed_and_dropped_when_gone(make_bot, clock):
    amazon = FakeAmazon(deals=[])
    bot, tg = make_bot(amazon=amazon)
    deal = make_deal("B0GONE0001")
    deal.fetched_at = (clock.t - timedelta(hours=2)).isoformat()
    bot.enqueue({"kind": "deal", "deal": deal.to_dict(), "chosen": False}, priority=1)
    bot.post_due()
    assert channel_posts(tg) == [] and bot.state["queue"] == []


def test_paused_bot_does_not_post(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "/pause"),
                                private_msg(2, "https://www.amazon.eg/dp/B0AAAAAAAA")])
    bot.run()
    assert channel_posts(tg) == [] and len(bot.state["queue"]) == 1
    assert "وقفت النشر" in replies(tg)[0]


def test_status_and_test_commands(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "/queue"), private_msg(2, "/test")])
    bot.run()
    out = replies(tg)
    assert "حالة البوت" in out[0] and "مش متظبط" in out[0]
    assert "ده شكل البوست" in out[1]


def test_album_photos_without_caption_are_ignored(make_bot):
    upd = private_msg(1, "", photo="P2")
    upd["message"].pop("text")
    upd["message"]["media_group_id"] = "g1"
    bot, tg = make_bot(updates=[upd])
    bot.run()
    assert tg.sent == []


def test_short_headline_text_is_escaped_in_link_posts(make_bot, cfg):
    bot, tg = make_bot(amazon=None, updates=[private_msg(1, "سماعة <جامدة> https://www.amazon.eg/dp/B0AAAAAAAA")])
    bot.run()
    assert "سماعة &lt;جامدة&gt;" in channel_posts(tg)[0]["text"]


def test_photo_you_send_with_a_link_is_used(make_bot):
    bot, tg = make_bot(updates=[private_msg(1, "https://www.amazon.eg/dp/B0AAAAAAAA",
                                            photo="MY_SCREENSHOT", caption=True)])
    bot.run()
    post = channel_posts(tg)[0]
    assert post["photo"] == "MY_SCREENSHOT" and f"tag={TAG}" in post["text"]


def test_posts_have_whatsapp_share_button_and_wa_digest(make_bot):
    amazon = FakeAmazon(deals=[make_deal("B0SHORT001", 899.0, 1499.0, 40, title="Anker Soundcore P3i")])
    bot, tg = make_bot(amazon=amazon, session=FakeSession(SHORT),
                       updates=[private_msg(1, "amzn.eu/d/abc123"), private_msg(2, "/wa")])
    bot.process_inbox()
    bot.post_due()
    post = channel_posts(tg)[0]
    labels = [b[0] for b in post["buttons"]]
    assert labels == ["اطلبه من أمازون 🛒", "شير على واتساب 📲"]
    assert post["buttons"][1][1].startswith("https://wa.me/?text=") and len(post["buttons"][1][1]) <= 1500

    bot.tg.updates = [private_msg(3, "/wa")]
    bot.process_inbox()
    digest = replies(tg)[-1]
    assert "*899 جنيه*" in digest and "~1,499 جنيه~" in digest
    assert f"https://www.amazon.eg/dp/B0SHORT001?tag={TAG}" in digest
    assert "<b>" not in digest


def test_post_still_goes_out_if_telegram_rejects_the_share_button(make_bot):
    from bot.telegram import TelegramError

    bot, tg = make_bot()
    real = tg.send_text

    def picky(chat, text, button=None, preview=True, html=True):
        if isinstance(button, list) and len(button) > 1:
            raise TelegramError("sendMessage: Bad Request: BUTTON_URL_INVALID")
        return real(chat, text, button=button, preview=preview, html=html)

    tg.send_text = picky
    bot.enqueue({"kind": "link", "url": f"https://www.amazon.eg/dp/B0AAAAAAAA?tag={TAG}"}, priority=0)
    bot.post_due()
    post = channel_posts(tg)[0]
    assert [b[0] for b in post["buttons"]] == ["اطلبه من أمازون 🛒"]
