"""One paid call on testnet, end to end. Proves the payment setup works before any agent exists.

    uv run python scripts/smoke_test.py --user-id you \
        --manager-arn arn:aws:bedrock-agentcore:... --instrument-id payment-instrument-...

Steps:
  1. App code creates a payment session with a small budget cap.
  2. GET the sandbox endpoint -> 402 Payment Required (x402).
  3. AgentCore checks the session budget, signs the payment, and returns a proof header.
  4. Retry with the proof -> 200 and the paid content.
  5. Read the session back from AgentCore: what it says was spent.

Testnet only: the sandbox endpoint charges $0.002 in Base Sepolia test USDC.
"""

import argparse
import base64
import json
import uuid

import httpx
from bedrock_agentcore.payments import PaymentManager

SANDBOX_URL = "https://sandbox.node4all.com/v1/x402-test"


def describe_price(response: httpx.Response) -> str:
    """Read the price out of the x402 v2 `payment-required` header, for display only."""
    challenge = json.loads(base64.b64decode(response.headers["payment-required"]))
    offer = challenge["accepts"][0]
    return f"{int(offer['amount']) / 1_000_000} USDC on {offer['network']} to {offer['payTo']}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--manager-arn", required=True)
    parser.add_argument("--instrument-id", required=True)
    parser.add_argument("--budget-usd", default="1.00")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    manager = PaymentManager(payment_manager_arn=args.manager_arn, region_name=args.region)

    # 1. The budget lives in AgentCore, set by app code. Nothing downstream can raise it.
    session = manager.create_payment_session(
        user_id=args.user_id,
        expiry_time_in_minutes=15,
        limits={"maxSpendAmount": {"value": args.budget_usd, "currency": "USD"}},
    )
    session_id = session["paymentSessionId"]
    print(f"1. Session {session_id}, cap ${args.budget_usd}")

    # 2. Unpaid request: the merchant answers with its price.
    first = httpx.get(SANDBOX_URL, timeout=30)
    print(f"2. GET -> {first.status_code}: {describe_price(first)}")
    if first.status_code != 402:
        raise SystemExit("Expected 402 from the sandbox endpoint")

    # 3. AgentCore signs the payment (or refuses, if it would exceed the cap).
    #    One client_token per purchase keeps the payment idempotent across retries.
    proof_header = manager.generate_payment_header(
        payment_instrument_id=args.instrument_id,
        payment_session_id=session_id,
        user_id=args.user_id,
        client_token=str(uuid.uuid4()),
        payment_required_request={
            "statusCode": first.status_code,
            "headers": dict(first.headers),
            "body": first.text,
        },
    )
    print(f"3. Proof header received: {', '.join(proof_header)} (value not printed)")

    # 4. Paid request.
    paid = httpx.get(SANDBOX_URL, headers=proof_header, timeout=30)
    print(f"4. Paid GET -> {paid.status_code}")
    print(f"   body: {paid.text[:300]}")
    for name, value in paid.headers.items():
        if "payment-response" in name.lower():
            try:
                value = json.loads(base64.b64decode(value))
            except ValueError:
                pass
            print(f"   {name}: {value}")

    # 5. The record the model can't write: AgentCore's own view of the session.
    record = manager.get_payment_session(payment_session_id=session_id, user_id=args.user_id)
    record.pop("ResponseMetadata", None)
    print("5. AgentCore session record:")
    print(json.dumps(record, indent=2, default=str))


if __name__ == "__main__":
    main()
