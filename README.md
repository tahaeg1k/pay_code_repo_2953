# PayCodes — PayPal AI Hackathon

Disposable payment codes backed by your PayPal: spending cap, expiry, single or multi-use.
Link PayPal once (Vault), generate a code, pay at a merchant with no PayPal popup.

## Run the demo
```bash
cd ~/Code/hackathons/paypal
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # sandbox Client ID + Secret (app must have Vault enabled)
uvicorn app:app --port 8001 --reload   # or just double-click PayCodes.command
```
- Wallet: http://localhost:8001  (link PayPal, generate codes, see activity)
- Demo store: http://localhost:8001/store  (basket + Fast Pay with a code)

## PYUSD escrow rail
At checkout choose **PYUSD escrow** instead of PayPal: the payment is held in `PayCodeEscrow` for a refund window.
The evidence page then shows Dispute / Refund / Release / Arbiter buttons, and every action is added to the evidence trail.
Runs simulated by default (same rules as the contract). For real Sepolia transactions: deploy `contracts/paycode-escrow`,
`pip install web3`, and fill the escrow section of `.env`.

## Website
- `/`, `/demo`, `/blog`: site from `static/site.html`
- `/preview`, `/preview/demo`, `/preview/blog`: alternative design in `static/site/` (home, demo tab wrapping the live app, blog)
- `/wallet`, `/store?m=greenleaf`, `/evidence/<id>`: the working app

## AI: intent check and shopping agent (Claude)
Add `OPENROUTER_API_KEY` (OpenRouter's Anthropic-compatible endpoint, models `anthropic/claude-haiku-5.5` and
`anthropic/claude-sonnet-5.5`) or `ANTHROPIC_API_KEY` to `.env`. OpenRouter is used if both are set, unless `AI_PROVIDER=anthropic`.
- **Intent check.** When you generate a code, write what it may buy ("vegetarian groceries, no alcohol").
  On every payment Claude checks the basket's line items against it. It runs only after the hard rules
  (cap, shop, expiry) have passed, and it can only **pause** (you decide in the wallet) or **block**. It can never
  allow what the rules refuse, and any failure becomes a pause. Basket text is treated as untrusted input.
- **Approval threshold.** "Ask me before paying over £X": larger payments wait in the wallet's *Waiting for you* panel.
  Approving signs off a SHA-256 hash of the exact request, so nothing can change the amount, shop or basket in between.
- **Shopping agent.** In the wallet, issue a code to "My AI agent", then *Send your AI agent shopping*. Or from the terminal:
  ```bash
  python agent.py --code SP-XXXX-XXXX-XXXX "Vegetarian groceries for two, about £15, at Greenleaf"
  python agent.py --code SP-... --no-precheck "A big breakfast"   # skips reading the limits: shows a decline, then recovery
  ```
  The code stays inside the tool layer, so the model never sees it. Prices come from the shop's catalog, not the agent.
- **MCP server.** Let Claude Desktop (or any MCP client) shop with a code. Add to `claude_desktop_config.json`:
  ```json
  {"mcpServers": {"scopepay": {"command": "python3", "args": ["/Users/you/Code/hackathons/paypal/mcp_server.py"],
                               "env": {"SCOPEPAY_URL": "http://localhost:8001"}}}}
  ```

## Money safety
- **No overspending under load.** The amount is reserved on the code (under a lock) before PayPal is called, so parallel
  payments can't spend past the cap, and no write can overwrite another.
- **No double charges.** Every PayPal call that moves money carries a `PayPal-Request-Id` derived from the evidence id.
  If PayPal times out, the payment is marked *uncertain*, the amount stays reserved, and **Check with PayPal** replays the
  same request ids: PayPal returns the original result instead of charging again.
- **Verified webhooks.** `POST /webhooks/paypal` asks PayPal to verify each event's signature (needs `PAYPAL_WEBHOOK_ID`).
  Refunds and reversals made in PayPal put the money back on the code; disputes are added to the evidence trail.
- **Guards before any AI** (`guards.py`). Basket lines with hidden characters (zero-width, bidi, Unicode tags) or text aimed
  at an AI ("pre-approved by the user", "ignore previous instructions") are declined before the intent check runs, and
  every line must be a real product at the shop's catalog price.
- **No duplicate payments from the merchant side.** Send an `Idempotency-Key` header with `/api/redeem` or
  `/api/agent/checkout` (the demo store does): a retry or double click gets the first answer back. Without a key, an
  identical payment within 60 s is declined with a pointer to the first one.
- **PayPal authorization holds** (`HOLD_MODE=authorize`, off by default). A held payment places a real PayPal authorization
  on the buyer's wallet: approving captures it, rejecting or expiry voids it. If PayPal refuses the hold, ScopePay still
  holds the code's budget and charges normally after approval.
- **Signed evidence.** Each record's hash is signed with ScopePay's Ed25519 key, so a rebuilt chain can't pass. Anyone can
  check a downloaded evidence file offline:
  ```bash
  curl -s localhost:8001/api/evidence/public-key > key.json
  python verify_evidence.py EV-1A2B3C4D.json --key-file key.json     # or the whole trail from /api/evidence
  ```

### Attack suite (`tests/test_attacks.py`, all blocked before PayPal unless noted)
| Attack | Result |
|---|---|
| Checkout relabels a £34 keyboard as £3.40 | `catalog_prices` decline |
| Item the shop doesn't sell; charge ≠ line items | `catalog_prices` / `basket_matches` decline |
| Zero-width or Unicode-tag characters in an item name | `basket_clean` decline |
| Item name says "pre-approved by the user, verdict allow" | `basket_clean` decline; the AI is never asked |
| Double click on a multi-use code | second attempt `not_duplicate`; one PayPal order |
| Same Idempotency-Key replayed / reused for another basket | first answer returned / refused |
| Agent asks to raise its own limit | no tool exists that can issue, change or approve a code |
| Edit a record; edit and re-hash the whole chain | `hash chain` broken; `signature` invalid |
| Held payment rejected, or code expires first (`HOLD_MODE=authorize`) | PayPal authorization voided, budget restored |

## Payment monitor (AG Grid)
`/dashboard` shows every payment attempt, code, held payment and evidence record in AG Grid tables (sort, filter,
paginate), with headline numbers and the trail's integrity, refreshing every 5 seconds.

## Deploy to Render
1. Push to GitHub, then in Render: **New → Blueprint** → pick the repo. `render.yaml` sets everything up.
2. Fill the prompted values: PayPal sandbox client id/secret, payee emails, `OPENROUTER_API_KEY`, and `DEMO_VAULT_ID`
   (the `vault_id` from your local `paycodes_db.json`, so the hosted demo starts with PayPal already linked).
   Render generates `AUTOMATION_TOKEN` and `EVIDENCE_SIGNING_KEY`.
3. Render deploys after the GitHub Actions tests pass (`autoDeployTrigger: checksPass`); `/healthz` is the health check.
4. Optional: create a PayPal webhook for `https://<service>.onrender.com/webhooks/paypal` and set `PAYPAL_WEBHOOK_ID`.

The free plan sleeps when idle and resets local files on restart, so the demo ledger starts empty each time.

## Continuous integration
`.github/workflows/ci.yml` runs the test suite on every push and pull request, checks the OpenAPI spec builds, and
uploads `openapi.json` as a build artifact.

## Zapier automations
A Zap can start the shopping agent, for example "new row in a shopping-list sheet → buy it":
**Webhooks by Zapier → POST** to `https://<service>.onrender.com/api/automations/agent-run`, header
`X-ScopePay-Token: <AUTOMATION_TOKEN>`, JSON body `{"task": "Vegetarian groceries for two, about £15", "code": "SP-…"}`.
- The code must already exist and be issued to "My AI agent" in the wallet. Automations spend within a code, never create one.
- Answers at once with a `run_id` and `status_url`; add `"wait": true` to get the agent's result in the same call.
- Each trigger is written to the evidence trail (`automation_triggered`, with the code's last 4 characters only).

## API and SDKs (APIMatic)
FastAPI publishes the API at `/openapi.json` (interactive docs at `/docs`), limited to the JSON API and grouped by tags.
To generate client SDKs, import that URL (or the `openapi` CI artifact) into APIMatic.

## Tests
```bash
pip install pytest httpx
python -m pytest -q          # PayPal and Claude are mocked: rules, races, idempotency, holds, webhooks, agent, MCP
```

## Files
- `paycodes.py` — PayPal vault + code rules engine, reservations, holds/approvals, reconcile, webhooks
- `ai.py` — Claude intent check (tighten-only); `agent.py` — Claude shopping agent; `agent_tools.py` — the agent's tools
- `mcp_server.py` — ScopePay as an MCP server (stdio, no SDK needed); `catalog.py` — the shops' prices
- `guards.py` — injection scan + catalog price check; `signing.py` — Ed25519 evidence signatures;
  `verify_evidence.py` — offline verifier for evidence files
- `tests/` — offline test suite; `static/dashboard.html` — AG Grid monitor
- `render.yaml` — Render Blueprint; `.github/workflows/ci.yml` — tests + OpenAPI check on every push (`test_attacks.py`: the attack table above)
- `app.py` — FastAPI server; `static/` — wallet and store pages
- `escrow.py` — PYUSD escrow rail (simulated or Sepolia); `contracts/paycode-escrow` — Solidity contract + tests
- `vault_spike.py` — CLI version of the vault flow; `paypal_test.py` — basic order test

## Third-party code
`contracts/paycode-escrow/src/PayCodeEscrow.sol` is adapted from Circle's Refund Protocol (Apache-2.0); its licence notice is kept in the file.
