"""Chat replies: every URL a reply cites must have come back from a paid search."""

from app.verifier import check_reply

LOG = [
    {"call_id": "c1", "company": "Acme", "payment_outcome": "settled", "http_status": 200,
     "result_urls": ["https://news.example.com/acme-expands", "https://acme.example.com/press"]},
    {"call_id": "c2", "company": "Globex", "payment_outcome": "declined_cap", "http_status": 402,
     "result_urls": []},
]


def test_reply_citing_paid_results_is_verified():
    r = check_reply("Acme is expanding (https://news.example.com/acme-expands).", LOG)
    assert r["status"] == "verified"
    assert r["sources"] == [{"url": "https://news.example.com/acme-expands", "backed": True}]


def test_invented_source_is_unbacked():
    r = check_reply("Globex raised $50M: https://globex.example.com/news", LOG)
    assert r["status"] == "unbacked"
    assert r["reasons"] == ["source never returned by any paid search: https://globex.example.com/news"]


def test_one_bad_source_is_enough():
    text = "See https://acme.example.com/press and https://made-up.example.com/x"
    assert check_reply(text, LOG)["status"] == "unbacked"


def test_reply_with_no_urls_cant_be_checked():
    assert check_reply("Globex has about 52,000 employees.", LOG)["status"] == "no_sources"


def test_markdown_links_and_trailing_punctuation():
    text = "Read [this](https://news.example.com/acme-expands/). Also **https://acme.example.com/press**."
    r = check_reply(text, LOG)
    assert r["status"] == "verified"
    assert [s["url"] for s in r["sources"]] == ["https://news.example.com/acme-expands/", "https://acme.example.com/press"]


def test_declined_search_returns_nothing_to_cite():
    log = [{**LOG[1], "result_urls": ["https://globex.example.com/news"]}]  # even if something was logged
    assert check_reply("https://globex.example.com/news", log)["status"] == "unbacked"
