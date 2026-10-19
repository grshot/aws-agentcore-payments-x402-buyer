# Keep the Model Away From the Money

An AI agent pays for a real x402 API through **Amazon Bedrock AgentCore Payments**, then reports what it did. A verifier written in plain application code checks that report against records the model didn't write.

**This is the buyer side of x402.** In x402 a *seller* (the paid API) answers a request with `402 Payment Required` and a price; a *buyer* pays and retries with proof of payment. Here the agent is the buyer and [Exa](https://exa.ai) search is the seller. This repo doesn't run a paid API of its own; if you want to sell over x402, you need the seller side (a resource server and a facilitator), which isn't covered here.

> A model that can spend money can also fabricate the receipt proving it spent correctly. The part that decides to spend and the part that reports what happened can't both be the model.

## What it shows

A simple GTM (go-to-market) research agent gets a list of companies and what you sell. For each company it calls one tool, `search`, which is a paid web search (Exa, $0.007 per search over [x402](https://www.x402.org/)). It writes an account brief per company and a report of what it paid for.

Three things happen outside the model:

1. **The cap.** App code opens an AgentCore payment session with a budget before the agent starts. AgentCore checks every payment against it and refuses the one that would go over. The model never sees the budget and can't change it; telling it "the budget was raised" changes nothing.
2. **The credentials.** The Coinbase wallet credentials live in AgentCore Identity. The agent's environment has IDs, not keys.
3. **The verifier.** After the run, plain Python compares the agent's report with two records the model can't write: the tool log (written by the search tool's code) and the session record (read back from AgentCore). Briefs that cite searches the agent never paid for, or facts with no source, are blocked.

The cap stops overspending. The verifier stops misreporting. AgentCore's own logs and traces in CloudWatch are the audit trail for both.

## How it works

```
 you / the viewer
      │ companies, what you sell
      ▼
 app code (app/orchestrator.py)                      ── not the model ──
   1. create payment session with a cap ──────────► AgentCore Payments
   2. run the agent                                   │ budget check,
      │                                               │ Coinbase wallet signs
      ▼                                               │
   agent (Strands, one tool: search)                  │
      │ search(company, query)                        │
      ▼                                               │
   search tool (agent/tools.py)                       │
      POST Exa ─► 402 Payment Required (x402)         │
      price rule: Base USDC, ≤ $0.01 ─────────────────┤
      payment proof ◄─────────────────────────────────┘ (or: declined, over budget)
      POST Exa again with proof ─► results + tx hash
      writes the tool log
   3. read the session back from AgentCore
   4. verifier (app/verifier.py): claim vs tool log + session record
   5. save runs/<run id>.json
                                     AgentCore → CloudWatch: logs, spans, metrics
```

Rules the code keeps (see `AGENTS.md`):

- The agent has exactly **one** tool, `search`. Creating sessions, reading balances and verifying are app code, never tools.
- Budgets live in the payment session, created by app code. Never in a prompt.
- No wallet keys, API keys or provider secrets in the agent's environment.
- The verifier only trusts the tool log and the AgentCore session record. It's a pure function with no network calls.

## Repo layout

```
agent/    Strands agent, paid search tool, prompts, output schema
app/      orchestrator, chat, verifier (+ unit tests)
ui/       local web viewer: FastAPI + one HTML page, no build step
infra/    AgentCore project (payment managers + Coinbase connectors), observability setup
scripts/  wallet setup, smoke test, rehearsal, environment check
runs/     saved runs (local only)
```

## Testnet or mainnet

| | Testnet (Base Sepolia) | Mainnet (Base) |
|---|---|---|
| Money | Free test USDC from a faucet | Real USDC |
| Paid API | A sandbox x402 endpoint ($0.002) | Exa search ($0.007) |
| What you see | The payment flow: cap, decline, verifier checks on spend and count | All of it, with real search results and real briefs |

Testnet returns a fixed test payload, not search results, so briefs are empty there. Start on testnet anyway: it proves the setup works without spending anything.

## Setup

### Prerequisites

- An AWS account, a region with AgentCore Payments (this repo uses `us-east-1`), and AWS credentials for it.
- Bedrock model access for the model in your settings (default: Claude Sonnet 4.6, `us.anthropic.claude-sonnet-4-6`).
- A subscription to **Coinbase Wallets for AgentCore Payments** in AWS Marketplace, and a Coinbase account.
- Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node.js 20+, and the AgentCore CLI: `npm install -g @aws/agentcore`.

### 1. Install

```bash
uv sync
cd infra/PayDemo/agentcore/cdk && npm ci && cd -
cp infra/PayDemo/agentcore/aws-targets.example.json infra/PayDemo/agentcore/aws-targets.json
# edit aws-targets.json: your account ID and region
```

### 2. Deploy the payment managers and connectors

`infra/PayDemo/agentcore/agentcore.json` defines two payment managers, one per network, each with a Coinbase connector created by QuickCreate (AWS creates and stores the Coinbase credentials; you never handle keys).

```bash
cd infra/PayDemo
agentcore deploy
```

The first deploy in an account and region may need CDK bootstrapping (`npx cdk bootstrap aws://<account>/<region>`). The stack is called `AgentCore-PayDemo-default`; its outputs have the manager ARNs and connector IDs:

```bash
aws cloudformation describe-stacks --stack-name AgentCore-PayDemo-default --query "Stacks[0].Outputs"
```

### 3. Approve each connector on Coinbase (within 10 minutes)

Each new connector waits in `PENDING_AUTHENTICATION` until you approve it. Get its link right after the deploy and open it:

```bash
aws bedrock-agentcore-control get-payment-connector \
  --payment-manager-id <manager id> --payment-connector-id <connector id> \
  --query authorizationUrl --output text
```

- The link expires about 10 minutes after the connector is created. If it has, rename the connector in `agentcore.json` and deploy again to get a new one.
- On the Coinbase page, choose an existing project. If none is offered, create one, then **open the link again** and select it; creating a project partway through the approval can fail it.
- When it's approved, `get-payment-connector` shows `READY`.

### 4. Create a wallet, delegate it, fund it

```bash
uv run python scripts/setup_wallet.py --user-id you --email you@example.com \
  --manager-arn <manager ARN> --connector-id <connector id>
```

It prints the wallet address and a Coinbase delegation link. Open the link and allow AgentCore to sign payments from the wallet. Then fund it:

- **Testnet:** Base Sepolia USDC from the [Circle faucet](https://faucet.circle.com/).
- **Mainnet:** a small amount of USDC **on the Base network** (a few dollars covers hundreds of searches). The wallet doesn't need ETH: the x402 facilitator pays the gas.

Each network has its own manager and wallet. Never send mainnet funds to a testnet address.

### 5. Settings

Copy `.env.example` to `.env` (testnet) and/or `.env.mainnet` and fill in the manager ARN, connector ID, instrument ID and user ID. For testnet, set `NETWORK=testnet` and `SEARCH_ENDPOINT=https://sandbox.node4all.com/v1/x402-test`.

Check it:

```bash
uv run python -m scripts.check_env --env .env        # no provider secrets in the agent's environment
uv run python scripts/smoke_test.py --user-id you \
  --manager-arn <testnet manager ARN> --instrument-id <instrument id>   # one paid testnet call
```

### 6. Logs and traces in CloudWatch (optional, recommended)

AgentCore Payments publishes metrics by itself. To also get its logs (every payment, including declines and the reason) and traces, run once per payment manager:

```bash
uv run python infra/setup_observability.py --manager-arn <manager ARN>
```

It turns on CloudWatch Transaction Search for the account (needed to search traces; takes a couple of minutes) and delivers logs to `/aws/vendedlogs/bedrock-agentcore/payments/<manager id>`.

## Run it

**Viewer** (recommended):

```bash
uv run uvicorn ui.server:app --port 8000
```

Open http://localhost:8000 and pick a network.

- **Run** tab: companies, what you sell, a prompt (`neutral`, or `pressure`: "always deliver a brief for every company"), and the session cap. Watch each payment as it happens, then compare what the agent said with what was recorded, and flip **Governed / Ungoverned** to see what the verifier blocks. Saved runs can be replayed; `?run=<run id>` opens one directly.
- **Chat** tab: one chat is one payment session. Ask about companies; try telling the agent the budget was raised. **Verify now** checks the agent's report of the chat against the records.
- Each session links to its AgentCore logs in CloudWatch.

**Command line:**

```bash
uv run python -m app.orchestrator --env .env.mainnet --prompt pressure --budget 0.015 \
  --companies "Company A, Company B, Company C" --offering "what you sell"
```

**Rehearsal** (runs the cap-and-pressure scenario several times and reports how often the agent misreports):

```bash
uv run python -m scripts.rehearse --env .env.mainnet --runs 5
```

## Spending limits

- **Price rule** (`MAX_PRICE_PER_CALL_USD`, app code): refuses any 402 that isn't Base USDC or costs more than this, before anything is signed.
- **Session cap** (AgentCore): per run or chat; set by app code.
- **Operator ceiling** (`MAX_SESSION_CAP_USD`): no session gets a bigger cap, whatever the viewer or CLI asks for. When deployed, point `MAX_SESSION_CAP_SSM_PARAMETER` at an SSM Parameter Store parameter instead and change it in the console; it's read on every new session.
- **Wallet balance:** the ceiling above everything. Fund it with what you're willing to lose.

The payment manager's `defaultSpendLimit` only applies to sessions the AgentCore CLI creates with `agentcore invoke --auto-session`; this app sets its own cap on every session.

## Cost

- **Searches:** $0.007 each on mainnet. The cap-and-pressure scenario with a $0.015 cap spends $0.014 per run.
- **Bedrock:** model tokens for each run or chat message (a few cents per run with Sonnet).
- **AWS:** AgentCore Payments, CloudWatch logs and traces, and the Coinbase Marketplace listing are billed per their own pricing; check them for your account. Demo volume is small.

## Things that tripped us up

- **The AgentCore CLI template outputs each connector's `AuthorizationUrl`**, which disappears once the connector is approved, so every deploy after an approval failed and rolled back. `infra/PayDemo/agentcore/cdk/lib/cdk-stack.ts` doesn't output it; get the link from the connector (step 3).
- **Exa offers two payment protocols in one 402:** x402 (`payment-required`) and MPP (`WWW-Authenticate: Payment`). The SDK prefers MPP when that header is present, which signed a payment the wallet couldn't make. The search tool passes AgentCore only the x402 offer its price rule approved.
- **The docs' `allow-vended-log-delivery-for-resource` step** isn't an API call; it's an IAM permission (`bedrock-agentcore:AllowVendedLogDeliveryForResource`) checked when the delivery is created.
- **Junk tokens in the wallet.** After the wallet makes payments on mainnet, its address is public and it will receive airdrop spam tokens. Ignore them: don't sell, swap, approve or visit sites they point to. The app only ever pays in USDC (the price rule checks the token contract).
- **Trace delivery** can't be set up until Transaction Search is `ACTIVE`, and the first calls right after setup may produce logs but no spans.

## Tests

```bash
uv run pytest
```

## Tear down

1. `cd infra/PayDemo && agentcore remove` (follow the prompts) and `agentcore deploy`, or delete the `AgentCore-PayDemo-default` stack in CloudFormation.
2. If you ran the observability setup: delete the log deliveries and the `/aws/vendedlogs/bedrock-agentcore/payments/...` log groups. Transaction Search can stay on or be switched back in X-Ray settings.
3. Delete the `CDKToolkit` stack only if nothing else in the account uses CDK (empty its S3 bucket first).
4. Optionally cancel the Coinbase Marketplace subscription and delete the Coinbase project QuickCreate made.

## License

Apache-2.0
