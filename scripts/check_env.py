"""Check that the agent's environment holds no payment-provider secrets.

    uv run python -m scripts.check_env --env .env.mainnet

The claim "the agent never holds a key" should be checkable, not just said.
Wallet and provider credentials live in AgentCore Identity; the settings the
app needs are IDs and ARNs, which aren't secrets. This looks at the process
environment and the settings file and reports variable *names* only, never
values.
"""

from __future__ import annotations

import argparse
import os
import re

from dotenv import dotenv_values

# Names that would mean a provider credential leaked into the agent's environment.
SECRET_NAMES = re.compile(
    r"(COINBASE|CDP_|PRIVY|STRIPE|WALLET_SECRET|API_KEY_SECRET|PRIVATE_KEY|EXA_API_KEY|AUTHORIZATION_KEY)",
    re.IGNORECASE,
)


def secret_names(names: list[str]) -> list[str]:
    return sorted(n for n in names if SECRET_NAMES.search(n))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env", help="Settings file the app runs with")
    args = parser.parse_args()

    found = {
        "process environment": secret_names(list(os.environ)),
        args.env: secret_names(list(dotenv_values(args.env))),
    }
    for where, names in found.items():
        print(f"{where}: {'clean' if not names else 'FOUND ' + ', '.join(names)}")
    if any(found.values()):
        raise SystemExit("Provider credentials belong in AgentCore Identity, not in the agent's environment.")
    print("OK: no payment-provider secrets. Credentials stay in AgentCore Identity.")


if __name__ == "__main__":
    main()
