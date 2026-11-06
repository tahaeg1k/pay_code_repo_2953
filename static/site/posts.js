const POSTS = [
{
 slug: "payment-succeeds-still-wrong", date: "2026-11-05", read: 4,
 title: "A payment can succeed and still be wrong",
 summary: "Why AI shopping agents need more than a card number, and where ScopePay draws the line.",
 body: `
<p>Ask an agent to book a hotel under $600 and it might find a room for $580 and pay. The card works, the hotel is real, the price is within budget. But it booked the non-refundable rate, which you'd never have picked.</p>
<p>Vignesh Sreedhar's essay <a href="https://x.com/vigstar1/status/2116973323460735200" target="_blank" rel="noopener">“Agentic commerce is at an impasse”</a> puts it plainly: a payment can clear and still violate what the user wanted. And nobody downstream can tell. The merchant can't tell a careful agent from a bad bot, the bank approves without context, and a dispute becomes one person's word against another's.</p>
<blockquote>The agent proposes. The rules decide. The record proves it.</blockquote>
<h2>Today's options</h2>
<p>Agents pay today by autofilling your real card from a vault, by using card-network agent tokens that many banks won't issue, through virtual cards on someone else's balance sheet, or from a prepaid stablecoin balance where the agent's mistakes are yours to eat.</p>
<h2>What ScopePay does differently</h2>
<p>The agent never gets your credentials. It gets a code with a cap, a merchant lock and an expiry. Every purchase is checked against those rules before PayPal is charged, and every decision, approved or declined, is written to an evidence trail that can't be quietly edited.</p>
<p>Limits aren't the whole of intent: “refundable only” is next on our list. But a scoped, provable payment is the foundation everything else needs.</p>`
},
{
 slug: "why-paypal", date: "2026-11-03", read: 3,
 title: "Why we built on PayPal",
 summary: "One company already holds the buyer's wallet, the merchant's checkout and the dispute desk.",
 body: `
<p>Agent payments break down because every party sees only part of the transaction. The card network sees an authorisation. The bank sees an amount. The shop sees an order from something that might be a bot.</p>
<p>PayPal is unusual: it's the buyer's wallet, it's the merchant's checkout, and it's where disputes get resolved. That closed loop is exactly what an agent's instructions need to travel intact from one end to the other.</p>
<h2>The PayPal pieces we use</h2>
<ul>
<li><b>Vault</b> saves the buyer's PayPal once, with their approval, so later payments need no popup.</li>
<li><b>Orders v2</b> charges that saved PayPal, with every basket item attached to the order.</li>
<li><b>Payouts</b> sends each merchant their money in their own PayPal account.</li>
<li><b>PayPal USD (PYUSD)</b> powers an optional escrow rail with a refund window.</li>
</ul>
<p>Merchants don't have to adopt anything new: a ScopePay payment is a PayPal payment. It just arrives with proof that the purchase was authorised.</p>`
},
{
 slug: "paying-the-merchant", date: "2026-11-02", read: 3,
 title: "Paying the merchant, not ourselves",
 summary: "The billing-agreement error that taught us how money really flows on PayPal.",
 body: `
<p>Our first version had a quiet flaw: every payment landed in our own sandbox account, because our app was the merchant. Fine for a demo, wrong for a product. The money has to reach the shop.</p>
<p>So we named the shop as the payee on each order. PayPal answered with <code>BILLING_AGREEMENT_NOT_FOUND</code>.</p>
<h2>What was going on</h2>
<p>When a buyer saves their PayPal, the permission belongs to the app that asked for it. Name a different merchant as the payee and PayPal looks for the agreement under that merchant, finds nothing, and refuses. That's a sensible safeguard.</p>
<h2>The fix</h2>
<p>We settle in two legs: charge the saved PayPal into ScopePay, then pay the merchant out immediately through the Payouts API. Both legs are written to the evidence record. In production, PayPal's multiparty platform setup does this in a single step, with the merchant onboarded under ScopePay.</p>`
},
{
 slug: "evidence-trail", date: "2026-11-01", read: 3,
 title: "Every payment leaves a receipt",
 summary: "How a hash-chained evidence trail turns a dispute from an argument into a lookup.",
 body: `
<p>When an agent buys the wrong thing, the first question is always the same: what was it actually allowed to do? ScopePay answers that with records, not memory.</p>
<h2>What gets recorded</h2>
<p>Linking PayPal records the buyer's consent. Issuing a code records its rules. Every payment attempt records the basket, the amount, the shop, whether a person or an agent paid, each rule check and its result, and PayPal's order and capture IDs.</p>
<h2>Why chain them</h2>
<p>Each record includes a fingerprint (a SHA-256 hash) of the one before it. Change a past amount, or delete a decline, and every fingerprint after it stops matching. The wallet shows a simple badge: trail intact, or tampered at a specific record.</p>
<p>Each record has its own page you can print or download, ready to hand to a shop, to PayPal or to an arbiter.</p>`
},
{
 slug: "reversible-stablecoins", date: "2026-10-31", read: 4,
 title: "Reversible stablecoin payments with a refund window",
 summary: "Stablecoin transfers are final. We added a three-day window where an agent's mistake can still be undone.",
 body: `
<p>Stablecoins like PayPal USD are fast and programmable, but once they move, they're gone. That's great for merchants and terrible when your agent bought the wrong thing.</p>
<h2>An escrow with a clock</h2>
<p>ScopePay's optional PYUSD rail holds each payment in a small smart contract for a refund window, three days by default and set per merchant. During the window the payer can dispute, which freezes the funds. The merchant can refund at any time. An arbiter settles disputes using the evidence record, whose fingerprint is stored with the payment on-chain. After the window, anyone can release the money to the merchant.</p>
<h2>Learning from prior work</h2>
<p>We adapted the design from Circle's open-source Refund Protocol and simplified it: every payment is held separately, so refunding one can never touch another, and there's no early-withdrawal path for the arbiter. It targets PYUSD on Ethereum Sepolia; the demo runs an exact simulation of the contract.</p>`
}
];
