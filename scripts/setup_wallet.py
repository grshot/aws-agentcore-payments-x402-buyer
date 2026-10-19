"""Create the wallet (payment instrument) the agent will spend from. Run once per network.

    uv run python scripts/setup_wallet.py --user-id you --email you@example.com \
        --manager-arn arn:aws:bedrock-agentcore:... --connector-id coinbase...

This only creates the wallet. It does not create a payment session: sessions
(and their budgets) are created per run by the app, never ahead of time.

After it runs, two things happen in the browser, by the wallet's owner:
  1. Delegation: open the printed Coinbase link and allow AgentCore to sign
     payments from this wallet.
  2. Funding: send USDC to the printed wallet address (testnet: Circle faucet).
"""

import argparse

from bedrock_agentcore.payments import PaymentManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True, help="Label for whose wallet this is; appears in payment records")
    parser.add_argument("--email", required=True, help="Linked to the Coinbase wallet; used to sign in for delegation")
    parser.add_argument("--manager-arn", required=True, help="Payment manager ARN (from `agentcore deploy` outputs)")
    parser.add_argument("--connector-id", required=True, help="Payment connector ID (from `agentcore deploy` outputs)")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    manager = PaymentManager(payment_manager_arn=args.manager_arn, region_name=args.region)
    instrument = manager.create_payment_instrument(
        user_id=args.user_id,
        payment_connector_id=args.connector_id,
        payment_instrument_type="EMBEDDED_CRYPTO_WALLET",
        payment_instrument_details={
            "embeddedCryptoWallet": {
                # ETHEREUM covers EVM chains, including Base and Base Sepolia.
                "network": "ETHEREUM",
                "linkedAccounts": [{"email": {"emailAddress": args.email}}],
            }
        },
    )

    wallet = instrument.get("paymentInstrumentDetails", {}).get("embeddedCryptoWallet", {})
    print(f"PAYMENT_INSTRUMENT_ID={instrument['paymentInstrumentId']}")
    print(f"PAYMENT_USER_ID={args.user_id}")
    print(f"Wallet address : {wallet.get('walletAddress')}")
    print(f"Delegation link: {wallet.get('redirectUrl')}")


if __name__ == "__main__":
    main()
