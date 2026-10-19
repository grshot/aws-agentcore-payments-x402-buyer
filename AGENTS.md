# AGENTS.md

Guidance for coding agents and contributors working in this repo.

## What this is

A demo of Amazon Bedrock AgentCore Payments on the x402 buyer side: a simple GTM account research agent pays for a real x402 search API, and a verifier in plain application code checks the agent's report against records the model didn't write. See `README.md`.

Keep it focused on payments: no AgentCore Memory, multi-user/session management, Gateway, Browser or extra tools.

## Hard rules (these are the point of the demo — don't break them)

- **The agent has exactly one tool: `search`.** Session creation, balance or session reads, and verification are app code, never model-callable tools. Don't use the AgentCore Strands plugin or LangGraph middleware as-is: both register wallet/session query tools for the model by default.
- **Budgets live in the AgentCore payment session**, created by app code. Never put a budget in a prompt or let the model change it.
- **No wallet keys, API keys or provider secrets in the agent's environment.** Credentials stay in AgentCore Identity.
- **The verifier never trusts model output as evidence.** It only uses the tool log and the AgentCore session record. Keep `app/verifier.py` a pure function with no network calls or framework imports.

## Safety

- **Ask before** any action that creates or changes AWS resources or IAM, subscribes to AWS Marketplace listings, or funds or spends from a wallet (mainnet payments are real money).
- Never read, print or commit secrets. Don't open `.env` or `agentcore/.env.local`.
- Prefer current AgentCore CLI/SDK docs over remembered APIs; field names change.

## Commands

```bash
uv sync                 # install dependencies
uv run pytest           # run tests
```

## Layout

```
agent/    Strands agent, paid search tool, prompts, output schema
app/      orchestrator, chat, verifier (+ tests in app/tests)
ui/       local web viewer: FastAPI + one HTML page, no build step
infra/    AgentCore project and setup notes
scripts/  wallet setup, smoke test, rehearsal, environment check
runs/     saved runs (local only, gitignored)
```

## Conventions

- Python ≥ 3.12, managed with `uv`. Region `us-east-1` by default.
- Code is meant to be read on stage and by learners: small functions, plain names, comments that explain *why*.
- Test fixtures use fictional companies and `example.com` URLs.
