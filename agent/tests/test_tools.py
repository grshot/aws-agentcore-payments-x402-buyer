"""Tests for the paid search tool, with a fake merchant and a fake AgentCore.

No network, no AWS. The fake merchant behaves like an x402 v2 API: 402 with a
price until a payment proof arrives, then 200 with results.
"""

import base64
import json
from decimal import Decimal

import httpx
import pytest
from bedrock_agentcore.payments.manager import InsufficientBudget, PaymentSessionExpired

from agent.tools import SearchConfig, paid_search, parse_offers, price_rule

BASE = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ENDPOINT = "https://api.exa.ai/search"


def _b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def merchant(amount="7000", network=BASE, asset=USDC_BASE):
    """A fake x402 v2 search API."""
    challenge = {"x402Version": 2, "accepts": [
        {"scheme": "exact", "network": network, "amount": amount, "asset": asset, "payTo": "0xmerchant"},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            return httpx.Response(402, headers={"payment-required": _b64(challenge)}, json={})
        return httpx.Response(
            200,
            headers={"payment-response": _b64({"success": True, "transaction": "0xabc"})},
            json={"results": [{"title": "Acme expands", "url": "https://news.example.com/acme", "text": "..."}]},
        )

    return httpx.MockTransport(handler)


class FakeAgentCore:
    """Stands in for PaymentManager: signs, or raises like the real SDK does."""

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    def generate_payment_header(self, **kwargs):
        self.calls += 1
        self.last_request = kwargs["payment_required_request"]
        if self.error:
            raise self.error
        return {"PAYMENT-SIGNATURE": "proof"}


def _config(manager, transport, max_price="0.01"):
    return SearchConfig(
        manager=manager, user_id="u", instrument_id="i", session_id="s",
        endpoint=ENDPOINT, network=BASE, max_price_usd=Decimal(max_price), http_transport=transport,
    )


def test_settled_search_returns_results_and_price_and_logs_it():
    config = _config(FakeAgentCore(), merchant())
    result = paid_search(config, "Acme", "Acme news")

    assert result["status"] == "ok"
    assert result["paid_usd"] == 0.007
    assert result["results"][0]["url"] == "https://news.example.com/acme"

    [entry] = config.log
    assert entry["payment_outcome"] == "settled"
    assert entry["amount_usd"] == "0.007"
    assert entry["tx_hash"] == "0xabc"
    assert entry["result_urls"] == ["https://news.example.com/acme"]


def test_budget_exhausted_is_a_clean_decline_not_a_crash():
    config = _config(FakeAgentCore(InsufficientBudget("Insufficient budget")), merchant())
    result = paid_search(config, "Acme", "Acme news")

    assert result == {"status": "payment_declined", "reason": "the session's spending limit has been reached"}
    assert config.log[0]["payment_outcome"] == "declined_cap"
    assert config.log[0]["amount_usd"] == "0"


def test_expired_session_is_declined():
    config = _config(FakeAgentCore(PaymentSessionExpired("expired")), merchant())
    assert paid_search(config, "Acme", "q")["status"] == "payment_declined"
    assert config.log[0]["payment_outcome"] == "declined_expired"


def test_price_rule_refuses_before_agentcore_is_asked():
    agentcore = FakeAgentCore()
    config = _config(agentcore, merchant(amount="50000"))  # $0.05 > $0.01 limit
    result = paid_search(config, "Acme", "q")

    assert result["status"] == "payment_declined"
    assert config.log[0]["payment_outcome"] == "declined_policy"
    assert agentcore.calls == 0  # nothing was signed


def test_wrong_network_is_refused():
    config = _config(FakeAgentCore(), merchant(network="solana:mainnet"))
    assert paid_search(config, "Acme", "q")["status"] == "payment_declined"
    assert config.log[0]["payment_outcome"] == "declined_policy"


def test_wrong_token_is_refused():
    config = _config(FakeAgentCore(), merchant(asset="0x0000000000000000000000000000000000000bad"))
    assert paid_search(config, "Acme", "q")["status"] == "payment_declined"


def test_v1_body_challenge_is_read():
    body = {"x402Version": 1, "accepts": [
        {"network": "base", "maxAmountRequired": "7000", "asset": USDC_BASE, "payTo": "0xm"},
    ]}
    [offer] = parse_offers(httpx.Response(402, json=body))
    assert offer.network == BASE
    assert offer.amount_usd == Decimal("0.007")


@pytest.mark.parametrize("amount,ok", [("10000", True), ("10001", False)])
def test_price_limit_is_inclusive(amount, ok):
    challenge = {"accepts": [{"network": BASE, "amount": amount, "asset": USDC_BASE, "payTo": "0xm"}]}
    offers = parse_offers(httpx.Response(402, headers={"payment-required": _b64(challenge)}))
    decision = price_rule(offers, BASE, Decimal("0.01"))
    assert (not isinstance(decision, str)) == ok


def test_only_the_approved_offer_is_passed_to_agentcore():
    # Like Exa: a plain USDC option plus a batched option on the same network.
    batched = {"scheme": "exact", "network": BASE, "amount": "7000", "asset": USDC_BASE, "payTo": "0xbatch",
               "extra": {"name": "GatewayWalletBatched", "verifyingContract": "0x7777"}}
    plain = {"scheme": "exact", "network": BASE, "amount": "7000", "asset": USDC_BASE, "payTo": "0xmerchant"}
    challenge = {"x402Version": 2, "accepts": [batched, plain]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            # Like Exa, it also offers MPP, which the SDK would otherwise prefer.
            mpp = 'Payment id="x", realm="api.example.com", method="tempo", intent="charge"'
            return httpx.Response(402, headers={"payment-required": _b64(challenge), "www-authenticate": mpp}, json={})
        return httpx.Response(200, json={"results": []})

    agentcore = FakeAgentCore()
    paid_search(_config(agentcore, httpx.MockTransport(handler)), "Acme", "Acme news")

    sent_headers = agentcore.last_request["headers"]
    assert "www-authenticate" not in {k.lower() for k in sent_headers}
    sent = json.loads(base64.b64decode(sent_headers["payment-required"]))
    assert sent["accepts"] == [plain]


def test_live_feed_reports_each_stage_and_never_reaches_the_model():
    events = []
    config = _config(FakeAgentCore(), merchant())
    config.on_event = events.append
    result = paid_search(config, "Acme", "Acme news")

    assert [e["stage"] for e in events] == ["searching", "paying", "done"]
    assert events[-1]["payment_outcome"] == "settled"
    assert "stage" not in result and "tx_hash" not in result  # the model sees only the tool result
