# SPEC.md — Statement of Requirements
> Agent: keep this file updated as the build evolves (add a "Current state" section and tick acceptance
> criteria). It must always be enough to rebuild the app from scratch.

## Context
Entry for the PayPal AI Hackathon (https://paypalaihackathon.devpost.com/, deadline 12 Nov 2026).
Must meaningfully use the PayPal developer platform (sandbox) and AI.

## Problem
Paying online means handing over reusable credentials (card numbers, PayPal login) to merchants and,
increasingly, to AI agents, with no limits on what they can spend.

## Solution
"PayCodes": disposable payment codes backed by the user's PayPal, each with rules:
spending cap, allowed merchant(s), expiry, single- or multi-use. Codes can be used by the
user or delegated to an AI agent.

## Architecture
- Backend: Python + FastAPI, JSON file store (paycodes_db.json, atomic writes under a lock). PayPal REST (Orders v2, Vault / payment tokens, Payouts, Webhooks). Claude via the Anthropic Messages API.
- User links PayPal once via a vault-with-purchase / setup-token flow; backend stores the vault ID.
- Code generation: random code, stored hashed, linked to vault ID + rules.
- Merchant "Fast Pay": merchant calls /redeem {code, merchant_id, amount}; backend validates rules,
  then creates + captures a PayPal order using payment_source vault_id. Returns success or a clear decline reason.
- Agent: LLM agent (Claude or OpenAI, plus PayPal Agent Toolkit where useful) that builds a basket on the
  demo store, requests a scoped code, pays, and explains declines.

## Demo apps
1. Wallet dashboard: linked PayPal, "Generate code" (set cap / merchant / expiry), copy button, active codes, history.
2. Demo grocery store (own fake brand): basket + "Fast Pay with code".
3. Agent scenario: "weekly vegetarian groceries, £60" -> agent shops, pays with a scoped code;
   show a deliberate over-cap attempt being declined.

## Build order
1. Spike: vault the sandbox personal account, then charge the vault ID with no popup. (Riskiest step — do first.)
2. Code service + rules engine + /redeem, with tests for every decline reason.
3. Wallet dashboard and demo store UIs.
4. AI agent scenario.
5. README, seed data, deploy (e.g. Render), demo script.

## Acceptance criteria
- [ ] A code pays successfully within its rules; payment visible in sandbox business account.
- [x] Over-cap, wrong-merchant, expired and reused codes are declined with a reason. (offline tests, PayPal mocked)
- [ ] Agent completes a purchase end to end without the user's PayPal credentials.
- [ ] README gets a judge from clone to running demo in under 5 minutes (.env.example, seed data).

## Out of scope
Real merchants (no Amazon or other brands), production payments, bank-account (IBAN) payments.

## Submission checklist (human)
Public GitHub repo with OSI license, setup instructions or hosted URL,
YouTube demo under 3 min, no third-party trademarks or music.

## Current state
- Sandbox credentials work; `paypal_test.py` creates, approves and captures an order (COMPLETED).
- `vault_spike.py` written (build step 1): setup token -> buyer approval -> payment token (vault_id) -> charge with no popup,
  plus a mini local PayCode demo (issue / pay with cap, expiry, single use). Not yet run against the sandbox.
- Web demo built: `app.py` (FastAPI) + `paycodes.py` + `static/` wallet (link PayPal, generate codes, revoke, activity)
  and fictional "Greenleaf Grocer" store with Fast Pay. Rules engine tested offline with PayPal mocked; not yet run live.
- Added: merchant payees (each store paid into its own sandbox business account via purchase_units.payee; env
  GREENLEAF_PAYEE_EMAIL / NORTHSIDE_PAYEE_EMAIL), merchant locks on codes, "issued to AI agent" flag, line items sent
  to PayPal, and a hash-chained evidence trail (wallet linked, code issued/revoked, every payment attempt with checks,
  basket, rules at the time and PayPal IDs). Evidence page /evidence/<id> with JSON download + print; chain verification.
  Open risk: PayPal may refuse a different payee when paying from a vaulted token; ALLOW_PLATFORM_FALLBACK=1 settles to
  the platform instead and records that in the evidence.
- Added `contracts/paycode-escrow` (Foundry): PayCodeEscrow.sol, reversible PYUSD payments with a refund window,
  payer disputes, arbiter resolution, merchant refunds, evidence hash per payment. Adapted from Circle's Refund
  Protocol (Apache-2.0) without the pooled balances / early-withdrawal path. 11 tests incl. fuzz; not yet compiled
  (run `forge test` on the Mac). Target: PYUSD on Ethereum Sepolia. Not yet wired into the web app.
- Escrow wired into the app (`escrow.py`): store offers "PYUSD escrow" rail; evidence page shows live escrow status,
  countdown and role buttons (payer dispute, merchant refund, arbiter resolve, release); each action is an evidence
  record; refunds restore the code's budget. Simulated mode mirrors the contract; Sepolia mode via web3 when env set.
  Tested offline in simulated mode.
- Website: alternative design under /preview (static/site/: home with banknote-style code hero, PayPal money path,
  rules, evidence chain, PayPal Mafia case file with an illustrated silhouette; demo tab wrapping wallet + stores;
  blog with 5 posts). App pages (wallet, Greenleaf, Northside, evidence) restyled to the same system; Greenleaf green kept.
  Previous app stylesheet kept as static/style.v1.css.
- Wallet redesigned (PayPal account card, codes as banknote cards with gauges and Used/Expired/Revoked stamps, activity
  timeline with evidence links) and shops redesigned (patterned banners, perks bar, category filters, quantity steppers,
  order summary, "Pay with ScopePay" button, success/decline screens). Previous versions kept as *.v1.html.
- Safety layer + AI (Nov 5): see README "AI" and "Money safety".
  - `paycodes.redeem` reserves the amount under a lock before calling PayPal (no overspend under parallel payments,
    no lost writes); money-moving PayPal calls use request ids derived from the evidence id; timeouts/5xx give an
    UNCERTAIN payment that keeps its reservation until `reconcile()` replays the same ids; cached OAuth token.
  - New code fields `intent` (plain English, checked by `ai.audit_basket`) and `approve_over`. Order of checks:
    hard rules -> AI intent check (allow / hold / decline, tighten-only, failure = hold) -> threshold -> settle.
    Held payments wait in the wallet; approval must present the request hash (approval_refused recorded otherwise).
    New checks: amount_positive, basket_matches (charge equals the line items).
  - Agent: `catalog.py` (server-side prices), `agent_tools.py`, `agent.py` (Claude tool loop; the code is injected by
    the runner, never shown to the model), `mcp_server.py` (stdio JSON-RPC, no SDK). Endpoints /api/codes/check,
    /api/catalog/{m}, /api/agent/checkout, /api/agent/run, /api/held, /api/held/{id}/approve|reject, /api/reconcile/{id},
    /webhooks/paypal (signature verified with PayPal; refunds/reversals restore the code's budget).
  - Wallet: intent + threshold fields, "Waiting for you" panel, agent panel. Store: held / uncertain outcomes.
    Wallet and store now escape merchant/agent text. Previous versions kept as paycodes.v1.py, app.v1.py, *.v2.html.
  - `tests/` (30 tests, unittest style, run with pytest): all pass offline. Not yet run live: Claude calls, agent
    end to end, webhooks (need PAYPAL_WEBHOOK_ID and a public URL).
- Guards, signatures, holds (Nov 6): `guards.py` (basket_clean: hidden characters / text aimed at an AI, checked before the
  AI; catalog_prices: lines must match catalog.py prices, CATALOG_CHECK), duplicate guard (not_duplicate within
  DUPLICATE_WINDOW_SECONDS by request hash; Idempotency-Key header on /api/redeem and /api/agent/checkout returns the first
  answer for approved/held/uncertain, re-evaluates after a decline/error; store sends one key per checkout),
  `signing.py` (Ed25519 signature on every new evidence record, key from EVIDENCE_SIGNING_KEY or evidence_signing_key.pem;
  verify_evidence reports signed/unsigned and "problem": hash chain | signature; GET /api/evidence/public-key;
  `verify_evidence.py` offline CLI), HOLD_MODE=authorize (held payment -> Orders v2 AUTHORIZE on the vault; approve ->
  /v2/payments/authorizations/{id}/capture + payout; reject/expiry -> void; PayPal refusal falls back to app hold).
  Default HOLD_MODE=app. `tests/test_attacks.py` (23 attack tests); 56 offline tests pass. Existing trail (14 unsigned
  records) still verifies. Not yet run live: HOLD_MODE=authorize against the sandbox.
- AI provider (Nov 6): ai.py uses OpenRouter's Anthropic-compatible endpoint when OPENROUTER_API_KEY is set (models anthropic/claude-haiku-5.5, anthropic/claude-sonnet-5.5), else Anthropic directly; AI_PROVIDER / AI_*_MODEL override. 34 offline tests pass.
- Deploy, monitor, automations (Nov 8, from Marcos's plan): `render.yaml` (free web service, uvicorn with proxy headers,
  /healthz health check, deploys after CI passes), `.github/workflows/ci.yml` (pytest + OpenAPI artifact),
  `/dashboard` (AG Grid 33 from jsdelivr: attempts with result filters, codes, held/uncertain, evidence trail, KPIs),
  `seed_demo_wallet()` (DEMO_VAULT_ID restores the sandbox wallet on an empty hosted store), POST
  /api/automations/agent-run + GET /api/automations/runs/{id} (X-ScopePay-Token = AUTOMATION_TOKEN; only codes issued
  to ai_agent; background or wait; automation_triggered evidence record), OpenAPI limited to the JSON API with tags
  (title "ScopePay API" 0.3.0) for APIMatic. Not done on purpose: AI creating its own codes (step 4 as written), remote MCP.
  Verified live on localhost: dashboard renders real data, /healthz, /openapi.json (25 paths, no pages), 503 without
  AUTOMATION_TOKEN. Endpoint tests in tests/test_deploy.py run where FastAPI is installed (CI, the Mac).
- AG Grid + PayPal data (Nov 12): GET /api/paypal/activity (Transaction Search + balances, 60 s cache, rows matched to
  evidence by invoice_id = evidence id); dashboard "Straight from PayPal" grid and balance KPIs; Monitor (AG Grid) tab on
  /preview/demo, step 6 and nav link on the main site. Verified on localhost: grids render, PayPal call reaches the sandbox
  and returns the "turn on Transaction search" message until that feature is enabled on the app. 68 offline tests pass.
