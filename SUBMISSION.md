# PayCodes — disposable, rule-bound payment codes for people and AI agents

## Inspiration
AI agents are starting to shop for us, but today you either hand an agent your card details or PayPal login, or you approve every purchase yourself. Neither scales. Surveys show 69% of people distrust AI with payments, 70% want to review purchases first, and 60% would stop using a shopping agent after one mistake. The deeper problem is that **a payment can succeed and still violate what the user wanted**, and nobody in the chain (merchant, PayPal, the user) can later prove what was actually authorised.

PayCodes gives agents spending power that is limited, scoped and accountable, settled on PayPal.

## What it does
**1. Link PayPal once.** The user approves PayPal a single time (PayPal Vault). PayCodes never sees or stores their PayPal credentials.

**2. Generate a PayCode instead of sharing credentials.** Each code (e.g. `PC-8D8F-6B37-DA55`) carries its own rules:
- **Spending cap**: e.g. £60, tracked across payments
- **Expiry**: 15 minutes to 7 days
- **Merchant lock**: only usable at one store
- **Single-use or multi-use** until the cap is spent
- **Issued to**: the user, or an AI agent

Codes are shown once and stored only as a hash. They can be revoked at any time.

**3. Pay with Fast Pay.** At checkout the merchant submits the code with the basket. PayCodes checks every rule and charges the user's vaulted PayPal with **no popup**. The money goes to **the merchant's own PayPal account**, and the basket's line items appear on the PayPal transaction. Anything outside the rules is declined with a clear reason (over the cap, wrong merchant, expired, already used, revoked).

**4. Evidence trail for disputes.** Every action becomes a tamper-evident evidence record:
- linking PayPal (the user's consent)
- issuing a code (the rules the user set)
- every payment attempt, approved or declined: basket, amount, merchant, whether a human or an agent paid, each check that passed or failed, the code's rules at that moment, and PayPal's order, capture and payout IDs

Records are SHA-256 hash-chained, so editing or deleting history is detectable. Each record has a shareable evidence page that can be printed to PDF or downloaded as JSON, for merchants, PayPal or an arbiter.

**5. Optional PYUSD escrow rail: reversible stablecoin payments.** Stablecoin transfers are final. PayCodes adds a refund window: the payment is held in the `PayCodeEscrow` smart contract, tagged with the hash of its evidence record.
- During the window, the payer (or their agent's wallet) can **dispute**, which freezes the funds.
- The **arbiter** settles disputes using the evidence trail; the **merchant** can refund voluntarily.
- After the window, **anyone can release** the funds to the merchant.
- Each payment is held separately, so one refund can never touch another payment's funds.

The contract targets PYUSD on Ethereum Sepolia. The app also runs an exact simulation of it for demos.

## How we built it
- **PayPal REST APIs**, sandbox:
  - **Vault** (Payment Method Tokens: setup tokens and payment tokens) to save the buyer's PayPal with consent
  - **Orders v2** with `payment_source.paypal.vault_id` for popup-free charges, with line items and `custom_id`
  - **Payouts** to settle each merchant's share in their own PayPal account
- **Backend**: Python and FastAPI: a rules engine, the code lifecycle, and the hash-chained evidence ledger.
- **Frontend**: a wallet (link PayPal, generate and revoke codes, activity, trail integrity badge), two fictional demo stores (Greenleaf Grocer, Northside Electronics) with Fast Pay, and an evidence and dispute console.
- **Smart contract**: Solidity and Foundry, `PayCodeEscrow.sol`, adapted from Circle's open-source Refund Protocol (Apache-2.0) and simplified to remove pooled balances and the arbiter early-withdrawal path. Tested with unit tests and fuzzing.
- **web3.py** for the on-chain rail, with a simulator that mirrors the contract.

## Challenges we ran into
- **Paying the merchant, not ourselves.** PayPal won't charge a buyer's saved payment method on behalf of a different payee, because the billing agreement belongs to the app that created it. We settle in two legs: capture to PayCodes, then pay out to the merchant through the Payouts API, recorded in the evidence trail. PayPal's multiparty (platform) onboarding is the production path.
- **Reversibility on final rails.** We combined an escrow window with an off-chain evidence trail anchored on-chain by hash, so disputes can be decided on facts.

## Accomplishments we're proud of
- Agents never handle a PayPal credential. They get a narrow, revocable, expiring code.
- Declines are explainable, and every decision is provable after the fact.
- One set of rules works across two settlement rails: PayPal, and PYUSD escrow.

## What's next
- **Intent mandates**: attach structured intent to a code (e.g. "refundable only", "groceries, no alcohol", "delivered by Friday") and check line items against it, so a purchase that stays within budget but breaks intent is blocked before payment.
- **An AI shopping agent demo** that builds a basket from a plain-English request, asks for a scoped code, pays, and recovers from declines.
- PayPal multiparty onboarding for direct merchant settlement, and integration with the PayPal Agent Toolkit.

## Try it
See the README: double-click `PayCodes.command` (or run `uvicorn app:app --port 8001`), link a sandbox buyer, generate a code, then pay at `/store?m=greenleaf`.
