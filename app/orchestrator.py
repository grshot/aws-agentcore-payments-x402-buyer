"""App layer: everything around the agent that the model must not control.

    uv run python -m app.orchestrator --companies "Acme, Globex" \
        --offering "AI lease abstraction for CRE teams" --budget 0.015 --prompt pressure

One run:
  1. Create a payment session with a budget cap (AgentCore enforces it).
  2. Run the agent with its single paid `search` tool.
  3. Read the session back from AgentCore: the record the model can't write.
  4. Verify the agent's claim against the tool log and that record.
  5. Save everything to runs/<timestamp>.json.

Settings come from .env (see .env.example). Keep one file per network and
pick it with --env, e.g. `--env .env.mainnet`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from bedrock_agentcore.payments import PaymentManager
from dotenv import dotenv_values

from agent.agent import build_agent, research
from agent.tools import SearchConfig, log_as_dicts, make_search_tool
from app.verifier import gate, verify

NETWORKS = {"mainnet": "eip155:8453", "testnet": "eip155:84532"}
CHAINS = {"mainnet": "BASE", "testnet": "BASE_SEPOLIA"}  # names AgentCore uses for balance reads
# Sent with every payment call, so CloudWatch metrics and spans name the agent.
AGENT_NAME = "gtm-research-agent"
EXPLORERS = {"mainnet": "https://basescan.org", "testnet": "https://sepolia.basescan.org"}
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


def settings(env_file: str = ".env") -> dict[str, str]:
    # Read the file itself rather than load_dotenv(): load_dotenv never overrides
    # variables already set, so a long-running UI switching between .env (testnet)
    # and .env.mainnet would silently keep the first file's values.
    env = {**os.environ, **{k: v for k, v in dotenv_values(env_file).items() if v is not None}}
    required = ["PAYMENT_MANAGER_ARN", "PAYMENT_INSTRUMENT_ID", "PAYMENT_USER_ID", "BEDROCK_MODEL_ID", "SEARCH_ENDPOINT"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing settings in {env_file}: {', '.join(missing)}")
    return {
        "network": env.get("NETWORK", "testnet"),
        "region": env.get("AWS_REGION", "us-east-1"),
        "manager_arn": env["PAYMENT_MANAGER_ARN"],
        "instrument_id": env["PAYMENT_INSTRUMENT_ID"],
        "connector_id": env.get("PAYMENT_CONNECTOR_ID", ""),
        "user_id": env["PAYMENT_USER_ID"],
        "model_id": env["BEDROCK_MODEL_ID"],
        "endpoint": env["SEARCH_ENDPOINT"],
        "max_price_usd": env.get("MAX_PRICE_PER_CALL_USD", "0.01"),
        "budget_usd": env.get("DEFAULT_BUDGET_USD", "0.015"),
        # Operator ceiling: no session gets a bigger cap, whoever asks (see operator_ceiling).
        "max_session_cap_usd": env.get("MAX_SESSION_CAP_USD", ""),
        "max_session_cap_ssm_parameter": env.get("MAX_SESSION_CAP_SSM_PARAMETER", ""),
        "expiry_minutes": env.get("SESSION_EXPIRY_MINUTES", "15"),
        # A chat waits on a person typing, so its session needs longer than a run's.
        "chat_expiry_minutes": env.get("CHAT_SESSION_EXPIRY_MINUTES", "60"),
    }


# ----------------------------------------------------------------------------
# AgentCore payment session: created and read by app code only
# ----------------------------------------------------------------------------


def operator_ceiling(s: dict[str, str]) -> str | None:
    """The most any session may hold, set by whoever operates the app, not by its users.

    Read on every session, so changing it takes effect on the next session with no
    code change or restart. Deployed, keep it in SSM Parameter Store and edit it in
    the console; locally, MAX_SESSION_CAP_USD in the settings file is enough.
    """
    if s["max_session_cap_ssm_parameter"]:
        import boto3  # only needed when the ceiling lives in SSM
        ssm = boto3.client("ssm", region_name=s["region"])
        return ssm.get_parameter(Name=s["max_session_cap_ssm_parameter"])["Parameter"]["Value"].strip()
    return s["max_session_cap_usd"] or None


def clamp_budget(requested_usd: str, ceiling_usd: str | None) -> tuple[str, bool]:
    """The cap a session actually gets: what was asked for, but never above the ceiling."""
    if ceiling_usd is not None and Decimal(requested_usd) > Decimal(ceiling_usd):
        return ceiling_usd, True
    return requested_usd, False


def session_budget(requested_usd: str | None, s: dict[str, str]) -> dict[str, Any]:
    requested = requested_usd or s["budget_usd"]
    ceiling = operator_ceiling(s)
    budget, clamped = clamp_budget(requested, ceiling)
    return {"budget_usd": budget, "requested_usd": requested, "ceiling_usd": ceiling, "clamped": clamped}


def create_session(manager: PaymentManager, user_id: str, budget_usd: str, expiry_minutes: int) -> str:
    session = manager.create_payment_session(
        user_id=user_id,
        expiry_time_in_minutes=expiry_minutes,  # AgentCore allows 15-480
        limits={"maxSpendAmount": {"value": budget_usd, "currency": "USD"}},
    )
    return session["paymentSessionId"]


def get_session(manager: PaymentManager, user_id: str, session_id: str) -> dict[str, Any]:
    record = manager.get_payment_session(payment_session_id=session_id, user_id=user_id)
    record.pop("ResponseMetadata", None)
    return json.loads(json.dumps(record, default=str))  # datetimes -> strings, so it saves cleanly


def wallet(env_file: str = ".env") -> dict[str, Any]:
    """Wallet address and USDC balance, read from AgentCore (for the UI header)."""
    s = settings(env_file)
    manager = PaymentManager(payment_manager_arn=s["manager_arn"], region_name=s["region"])
    instrument = manager.get_payment_instrument(payment_instrument_id=s["instrument_id"], user_id=s["user_id"])
    instrument = instrument.get("paymentInstrument", instrument)
    address = instrument["paymentInstrumentDetails"]["embeddedCryptoWallet"]["walletAddress"]
    balance = manager.get_payment_instrument_balance(
        payment_connector_id=s["connector_id"] or instrument["paymentConnectorId"],
        payment_instrument_id=s["instrument_id"],
        chain=CHAINS[s["network"]],
        token="USDC",
        user_id=s["user_id"],
    )["tokenBalance"]
    return {
        "network": s["network"],
        "address": address,
        "balance_usd": str(Decimal(balance["amount"]) / 10 ** balance["decimals"]),
        "explorer": EXPLORERS[s["network"]],
    }


# ----------------------------------------------------------------------------
# One run
# ----------------------------------------------------------------------------


def run(companies: list[str], offering: str, prompt_variant: str = "neutral",
        budget_usd: str | None = None, env_file: str = ".env",
        on_event: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """One run. `on_event` gets live updates for the UI: session, each tool call, the result."""
    emit = on_event or (lambda event: None)
    s = settings(env_file)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cap = session_budget(budget_usd, s)
    budget = cap["budget_usd"]
    manager = PaymentManager(payment_manager_arn=s["manager_arn"], region_name=s["region"], agent_name=AGENT_NAME)

    session_id = create_session(manager, s["user_id"], budget, int(s["expiry_minutes"]))
    emit({"type": "session", "run_id": run_id, "session_id": session_id, "budget_usd": budget, "cap": cap,
          "network": s["network"], "explorer": EXPLORERS[s["network"]]})

    def on_call(entry: dict[str, Any]) -> None:
        emit({"type": "call", "entry": entry})
        if entry["stage"] == "done":  # after each payment decision, show what AgentCore now says
            emit({"type": "session_record", "record": get_session(manager, s["user_id"], session_id)})

    config = SearchConfig(
        manager=manager,
        user_id=s["user_id"],
        instrument_id=s["instrument_id"],
        session_id=session_id,
        endpoint=s["endpoint"],
        network=NETWORKS[s["network"]],
        max_price_usd=Decimal(s["max_price_usd"]),
        on_event=on_call,
    )
    agent = build_agent(make_search_tool(config), s["model_id"], s["region"], prompt_variant)
    claim = research(agent, companies, offering).model_dump()

    tool_log = log_as_dicts(config.log)
    session_record = get_session(manager, s["user_id"], session_id)
    report = verify(claim, tool_log, session_record)

    result = {
        "run_id": run_id,
        "network": s["network"],
        "explorer": EXPLORERS[s["network"]],
        "model_id": s["model_id"],
        "prompt_variant": prompt_variant,
        "companies": companies,
        "offering": offering,
        "budget_usd": budget,
        "cap": cap,
        "session_id": session_id,
        "claim": claim,
        "tool_log": tool_log,
        "session_record": session_record,
        "verification": report.to_dict(),
        "governed": gate(claim, report, governed=True),
    }
    save(result)
    emit({"type": "result", "result": result})
    return result


def save(result: dict[str, Any]) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"{result['run_id']}.json"
    path.write_text(json.dumps(result, indent=2))
    return path


def saved_runs() -> list[dict[str, Any]]:
    """One line per saved run, newest first (for the UI's replay picker)."""
    rows = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        try:
            r = json.loads(path.read_text())
            rows.append({
                "run_id": r["run_id"], "network": r["network"], "prompt_variant": r["prompt_variant"],
                "companies": r["companies"], "budget_usd": r["budget_usd"],
                "passed": r["verification"]["passed"],
            })
        except (ValueError, KeyError):
            continue  # fixtures or partial files
    return rows


def load_run(run_id: str) -> dict[str, Any]:
    if not run_id.replace("T", "").replace("Z", "").isdigit():  # only our own file names
        raise ValueError("bad run id")
    return json.loads((RUNS_DIR / f"{run_id}.json").read_text())


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def print_summary(result: dict[str, Any]) -> None:
    claim, log = result["claim"], result["tool_log"]
    print(f"\nRun {result['run_id']} · {result['network']} · prompt={result['prompt_variant']} · cap ${result['budget_usd']}")
    if result["cap"]["clamped"]:
        print(f"  (requested ${result['cap']['requested_usd']}, limited by the operator ceiling ${result['cap']['ceiling_usd']})")

    print("\nTool log (written by app code):")
    for e in log:
        tx = f" tx {e['tx_hash']}" if e.get("tx_hash") else ""
        print(f"  {e['call_id']} {e['company']:<20} {e['payment_outcome']:<16} ${e['amount_usd']}{tx}")

    print("\nAgent claims:")
    print(f"  {claim['paid_calls']} paid searches, ${claim['total_spent_usd']}, "
          f"researched {len(claim['companies_researched'])}, {len(claim['briefs'])} briefs")

    v = result["verification"]
    print(f"\nVerifier: {'PASS' if v['passed'] else 'FAIL'}")
    for c in v["checks"]:
        mark = "ok  " if c["passed"] else "FAIL"
        print(f"  {mark} {c['name']:<11} expected={c['expected']} actual={c['actual']} {c['detail']}")
    for b in v["briefs"]:
        if not b["verified"]:
            print(f"  blocked brief: {b['company']}: {'; '.join(b['reasons'])}")
    print(f"\nSaved runs/{result['run_id']}.json")


def main() -> None:
    # The SDK logs every declined payment at ERROR level. Declines are expected
    # here (that's the cap working), and the tool log already records them.
    logging.getLogger("bedrock_agentcore.payments").setLevel(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Run the research agent once and verify its report.")
    parser.add_argument("--companies", required=True, help="Comma-separated company names")
    parser.add_argument("--offering", required=True, help="What we sell, one line")
    parser.add_argument("--prompt", choices=["neutral", "pressure"], default="neutral")
    parser.add_argument("--budget", help="Session cap in USD (default: DEFAULT_BUDGET_USD)")
    parser.add_argument("--env", default=".env", help="Settings file, e.g. .env.mainnet (default: .env)")
    args = parser.parse_args()

    companies = [c.strip() for c in args.companies.split(",") if c.strip()]
    print_summary(run(companies, args.offering, args.prompt, args.budget, args.env))


if __name__ == "__main__":
    main()
