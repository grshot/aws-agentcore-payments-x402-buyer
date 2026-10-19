"""The agent's one and only tool: a paid search.

The model calls `search(company, query)`. Everything about money happens in
here, in application code:

  1. Send the search request. A paid API answers 402 Payment Required (x402)
     with its price.
  2. Price rule (ours): refuse anything above MAX_PRICE_PER_CALL_USD, on the
     wrong network, or in an unknown token. Nothing is signed.
  3. AgentCore Payments checks the session budget and signs the payment we
     approved, and only that one (or refuses: budget exhausted, session
     expired).
  4. Retry the request with the payment proof and return the results.

Every call is written to the tool log, which the app owns. The model sees only
the tool's return value, never the log; the verifier later reads the log. The
UI can watch the log live through `on_event`.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import httpx
from bedrock_agentcore.payments.manager import (
    InsufficientBudget,
    PaymentError,
    PaymentManager,
    PaymentSessionExpired,
)
from strands import tool

# USDC contract per network. A 402 asking for any other token is refused.
USDC = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
}
USDC_DECIMALS = 6

# x402 v1 names networks with labels; v2 uses CAIP-2 ids. Normalize to CAIP-2.
NETWORK_LABELS = {"base": "eip155:8453", "base-sepolia": "eip155:84532"}

# Testnet settlement sometimes lags: the proof is valid but the retry still
# gets 402. Retrying with the same client_token replays the same payment, so
# it can't charge twice.
MAX_SETTLE_ATTEMPTS = 3

MAX_RESULTS = 5


@dataclass
class SearchConfig:
    """What the tool needs, set by app code. The model can't see or change any of it."""

    manager: PaymentManager
    user_id: str
    instrument_id: str
    session_id: str
    endpoint: str
    network: str                 # CAIP-2, e.g. "eip155:8453"
    max_price_usd: Decimal       # the price rule
    log: list[dict[str, Any]] = field(default_factory=list)
    on_event: Callable[[dict[str, Any]], None] | None = None  # live feed for the UI; the model never sees it
    http_transport: httpx.BaseTransport | None = None  # tests swap in a fake server


@dataclass
class Offer:
    """One way to pay, taken from the merchant's 402 response."""

    network: str
    asset: str
    amount_usd: Decimal
    pay_to: str
    raw: dict[str, Any] = field(default_factory=dict)  # the option exactly as the merchant sent it

    def is_plain_usdc_transfer(self) -> bool:
        # A merchant can offer extra schemes on the same network, e.g. Exa also
        # offers Circle's batched "GatewayWalletBatched", which signs for a
        # different contract. We only pay with a standard USDC transfer.
        return self.raw.get("scheme", "exact") == "exact" and "verifyingContract" not in self.raw.get("extra", {})


# ----------------------------------------------------------------------------
# Reading the 402
# ----------------------------------------------------------------------------


def parse_offers(response: httpx.Response) -> list[Offer]:
    """Read the payment options from a 402 (x402 v2 header, or v1 JSON body)."""
    if "payment-required" in response.headers:  # v2
        challenge = json.loads(base64.b64decode(response.headers["payment-required"]))
    else:  # v1
        challenge = response.json()

    offers = []
    for accept in challenge.get("accepts", []):
        network = NETWORK_LABELS.get(accept.get("network"), accept.get("network"))
        atomic = accept.get("amount") or accept.get("maxAmountRequired") or "0"
        offers.append(Offer(
            network=network,
            asset=str(accept.get("asset", "")),
            amount_usd=Decimal(atomic) / 10**USDC_DECIMALS,
            pay_to=str(accept.get("payTo", "")),
            raw=accept,
        ))
    return offers


def price_rule(offers: list[Offer], network: str, max_price_usd: Decimal) -> Offer | str:
    """Our policy, checked before AgentCore signs anything.

    Returns the acceptable offer, or the reason for refusing.
    """
    on_network = [o for o in offers if o.network == network and o.is_plain_usdc_transfer()]
    if not on_network:
        return f"no plain USDC payment option on {network}"
    offer = on_network[0]
    if offer.asset.lower() != USDC[network].lower():
        return "payment asset is not USDC"
    if offer.amount_usd > max_price_usd:
        return f"price ${offer.amount_usd} is above the ${max_price_usd} per-call limit"
    return offer


def only_offer(response: httpx.Response, offer: Offer) -> dict[str, Any]:
    """The 402 as AgentCore will see it: trimmed to the one option we approved.

    AgentCore picks among the 402's options by network. Trimming means it can
    only sign what our price rule accepted.

    Some merchants (Exa) also advertise MPP in a `WWW-Authenticate: Payment`
    header. The SDK prefers MPP when that header is present, which would sign
    a different protocol on a different chain, so we drop it.
    """
    headers = {k: v for k, v in response.headers.items() if k.lower() != "www-authenticate"}
    body = response.text
    if "payment-required" in headers:  # v2
        challenge = json.loads(base64.b64decode(headers["payment-required"]))
        challenge["accepts"] = [offer.raw]
        headers["payment-required"] = base64.b64encode(json.dumps(challenge).encode()).decode()
    else:  # v1
        challenge = response.json()
        challenge["accepts"] = [offer.raw]
        body = json.dumps(challenge)
    return {"statusCode": response.status_code, "headers": headers, "body": body}


def settlement_tx(response: httpx.Response) -> str | None:
    """Transaction hash from the merchant's x402 `payment-response` header, if present."""
    header = response.headers.get("payment-response") or response.headers.get("x-payment-response")
    if not header:
        return None
    try:
        return json.loads(base64.b64decode(header)).get("transaction")
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# The search request itself
# ----------------------------------------------------------------------------


def is_exa(endpoint: str) -> bool:
    return "api.exa.ai" in endpoint


def send(client: httpx.Client, endpoint: str, query: str, headers: dict | None = None) -> httpx.Response:
    """Exa search is a POST with a JSON body; the testnet sandbox is a plain GET."""
    if is_exa(endpoint):
        body = {"query": query, "numResults": MAX_RESULTS, "contents": {"text": {"maxCharacters": 500}}}
        return client.post(endpoint, json=body, headers=headers, timeout=30)
    return client.get(endpoint, headers=headers, timeout=30)


def summarize_results(response: httpx.Response) -> list[dict[str, Any]]:
    """Keep what the model needs to write a brief: title, URL, date, a snippet."""
    data = response.json()
    results = []
    for r in data.get("results", [])[:MAX_RESULTS]:
        results.append({
            "title": r.get("title"),
            "url": r.get("url"),
            "published": r.get("publishedDate"),
            "snippet": (r.get("text") or "")[:500],
        })
    if not results and data:
        # The testnet sandbox returns a fixed payload, not search results.
        results.append({"title": "testnet sandbox payload", "url": None, "snippet": json.dumps(data)[:500]})
    return results


# ----------------------------------------------------------------------------
# The tool
# ----------------------------------------------------------------------------


def notify(config: SearchConfig, entry: dict[str, Any]) -> None:
    if config.on_event:
        config.on_event(dict(entry))


def paid_search(config: SearchConfig, company: str, query: str) -> dict[str, Any]:
    """Run one paid search and log it. Returns what the model will see."""
    entry: dict[str, Any] = {
        "call_id": f"c{len(config.log) + 1}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "company": company,
        "query": query,
        "http_status": None,
        "payment_outcome": None,
        "amount_usd": "0",
        "tx_hash": None,
        "result_urls": [],
        "detail": "",
        "stage": "searching",            # searching -> paying -> done, for the live view
    }
    config.log.append(entry)
    notify(config, entry)

    result = _pay_and_search(config, entry, company, query)

    entry["stage"] = "done"
    notify(config, entry)
    return result


def _pay_and_search(config: SearchConfig, entry: dict[str, Any], company: str, query: str) -> dict[str, Any]:
    """Steps 1-4 from the module docstring. Fills in `entry` as it goes."""
    # 1. The search request. A paid API answers 402 with its price.
    with httpx.Client(transport=config.http_transport) as client:
        first = send(client, config.endpoint, query)
    entry["http_status"] = first.status_code
    if first.status_code != 402:
        entry["payment_outcome"] = "error"
        entry["detail"] = f"expected 402 Payment Required, got {first.status_code}"
        return {"status": "error", "reason": "the search service did not respond as expected"}

    # 2. Our price rule, before anything is signed.
    decision = price_rule(parse_offers(first), config.network, config.max_price_usd)
    if isinstance(decision, str):
        entry["payment_outcome"] = "declined_policy"
        entry["detail"] = decision
        return {"status": "payment_declined", "reason": f"payment refused by policy: {decision}"}
    offer = decision
    entry["stage"] = "paying"
    entry["price_usd"] = str(offer.amount_usd)
    notify(config, entry)

    # 3 + 4. AgentCore checks the budget and signs; we retry with the proof.
    client_token = str(uuid.uuid4())  # one per purchase: retries replay the same payment
    for _ in range(MAX_SETTLE_ATTEMPTS):
        try:
            proof = config.manager.generate_payment_header(
                payment_instrument_id=config.instrument_id,
                payment_session_id=config.session_id,
                user_id=config.user_id,
                client_token=client_token,
                network_preferences=[config.network],
                payment_required_request=only_offer(first, offer),
            )
        except InsufficientBudget as e:
            entry["payment_outcome"] = "declined_cap"
            entry["detail"] = str(e)
            return {"status": "payment_declined", "reason": "the session's spending limit has been reached"}
        except PaymentSessionExpired as e:
            entry["payment_outcome"] = "declined_expired"
            entry["detail"] = str(e)
            return {"status": "payment_declined", "reason": "the payment session has expired"}
        except PaymentError as e:
            entry["payment_outcome"] = "error"
            entry["detail"] = str(e)
            return {"status": "error", "reason": "the payment could not be processed"}

        with httpx.Client(transport=config.http_transport) as client:  # fresh client: no cookies carried over from the 402
            paid = send(client, config.endpoint, query, headers=proof)
        entry["http_status"] = paid.status_code
        if paid.status_code != 402:
            break

    if paid.status_code != 200:
        # The payment may still have gone through; the verifier compares with AgentCore.
        entry["payment_outcome"] = "error"
        entry["detail"] = f"paid request returned {paid.status_code}"
        return {"status": "error", "reason": "the search failed after payment"}

    results = summarize_results(paid)
    entry["payment_outcome"] = "settled"
    entry["amount_usd"] = str(offer.amount_usd)
    entry["tx_hash"] = settlement_tx(paid)
    entry["result_urls"] = [r["url"] for r in results if r.get("url")]
    return {"status": "ok", "company": company, "paid_usd": float(offer.amount_usd), "results": results}


def make_search_tool(config: SearchConfig):
    """Wrap `paid_search` as the Strands tool the model sees. Its docstring is the tool description."""

    @tool
    def search(company: str, query: str) -> dict:
        """Search recent public web results about a company. Each call is a paid search.

        Args:
            company: The company this search is for, exactly as written in the account list.
            query: What to search for, e.g. "Acme recent news funding expansion".
        """
        return paid_search(config, company, query)

    return search


def log_as_dicts(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A copy of the tool log, safe to save or hand to the verifier."""
    return [dict(e) for e in log]

