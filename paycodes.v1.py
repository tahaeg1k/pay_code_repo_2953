"""PayCodes core: PayPal vault + disposable codes (cap, expiry, merchant lock, single/multi use)
+ tamper-evident evidence trail for disputes."""
import hashlib
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import escrow

load_dotenv()
BASE = "https://api-m.sandbox.paypal.com"
DB = Path(__file__).with_name("paycodes_db.json")
CURRENCY = "GBP"
SETTLEMENT = os.getenv("SETTLEMENT", "auto")  # auto | direct | payout (capture to PayCodes, then Payouts to merchant)

# Demo merchants. Each one is paid into ITS OWN sandbox business account (payee).
MERCHANTS = {
    "greenleaf": {"name": "Greenleaf Grocer", "emoji": "🥬", "payee_email": os.getenv("GREENLEAF_PAYEE_EMAIL", "")},
    "northside": {"name": "Northside Electronics", "emoji": "🔌", "payee_email": os.getenv("NORTHSIDE_PAYEE_EMAIL", "")},
}


class PayPalError(Exception):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db():
    d = json.loads(DB.read_text()) if DB.exists() else {}
    for k, v in {"wallet": {}, "codes": {}, "transactions": [], "evidence": []}.items():
        d.setdefault(k, v)
    return d


def _save(d):
    DB.write_text(json.dumps(d, indent=2))


def _token():
    r = requests.post(f"{BASE}/v1/oauth2/token",
                      auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                      data={"grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]


def _call(method, path, body=None):
    r = requests.request(method, f"{BASE}{path}", json=body, timeout=30, headers={
        "Authorization": f"Bearer {_token()}", "Content-Type": "application/json",
        "PayPal-Request-Id": str(uuid.uuid4()), "Prefer": "return=representation"})
    if not r.ok:
        try:
            detail = r.json()
            msg = detail.get("message") or detail.get("name")
            issues = "; ".join(f"{x.get('field') or x.get('issue')}: {x.get('description') or x.get('issue')}" for x in detail.get("details", []))
        except ValueError:
            msg, issues = r.text, ""
        raise PayPalError(f"PayPal {r.status_code}: {msg} {issues}".strip())
    return r.json()


# ---------- evidence trail (hash-chained, append-only) ----------
def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _new_eid():
    return "EV-" + secrets.token_hex(4).upper()


def _append_evidence(d, record, eid=None):
    prev = d["evidence"][-1]["hash"] if d["evidence"] else "GENESIS"
    record = {"id": eid or _new_eid(), "seq": len(d["evidence"]) + 1,
              "recorded_at": _now_iso(), **record, "prev_hash": prev}
    record["hash"] = hashlib.sha256(_canon(record).encode()).hexdigest()
    d["evidence"].append(record)
    return record


def verify_evidence():
    ev = _db()["evidence"]
    prev = "GENESIS"
    for r in ev:
        body = {k: v for k, v in r.items() if k != "hash"}
        if r["prev_hash"] != prev or hashlib.sha256(_canon(body).encode()).hexdigest() != r["hash"]:
            return {"valid": False, "records": len(ev), "broken_at": r["id"]}
        prev = r["hash"]
    return {"valid": True, "records": len(ev), "head": prev}


def get_evidence(eid):
    ev = _db()["evidence"]
    rec = next((r for r in ev if r["id"] == eid), None)
    if not rec:
        return None
    # include the code's issuance record so the bundle shows the rules the user set
    issuance = next((r for r in ev if r["type"] == "code_issued" and r["code"]["hint"] == rec.get("code", {}).get("hint")), None)
    actions = [r for r in ev if r["type"] == "escrow_action" and r.get("ref") == eid]
    live = None
    st = rec.get("settlement") or {}
    if st.get("rail") == "pyusd_escrow":
        try:
            live = escrow.status(_db(), st["escrow_payment_id"])
        except Exception as e:  # noqa: BLE001
            live = {"error": str(e)}
    return {"record": rec, "code_issuance": issuance, "escrow_actions": actions, "escrow_status": live, "chain": verify_evidence()}


def evidence_list():
    return list(reversed(_db()["evidence"]))


# ---------- wallet / vault ----------
def wallet():
    return _db()["wallet"]


def merchants():
    return {k: {"name": m["name"], "emoji": m["emoji"], "payee_email": m["payee_email"] or None} for k, m in MERCHANTS.items()}


def start_link(return_url, cancel_url):
    res = _call("POST", "/v3/vault/setup-tokens", {"payment_source": {"paypal": {
        "description": "ScopePay: save PayPal for future code payments",
        "usage_type": "MERCHANT", "customer_type": "CONSUMER", "permit_multiple_payment_tokens": False,
        "experience_context": {"brand_name": "ScopePay", "shipping_preference": "NO_SHIPPING",
                               "return_url": return_url, "cancel_url": cancel_url}}}})
    d = _db(); d["wallet"]["setup_token"] = res["id"]; _save(d)
    return next(l["href"] for l in res["links"] if l["rel"] == "approve")


def finish_link(setup_token=None):
    d = _db()
    st = setup_token or d["wallet"].get("setup_token")
    res = _call("POST", "/v3/vault/payment-tokens", {"payment_source": {"token": {"id": st, "type": "SETUP_TOKEN"}}})
    pp = res.get("payment_source", {}).get("paypal", {})
    d["wallet"] = {"vault_id": res["id"], "customer_id": res.get("customer", {}).get("id"),
                   "email": pp.get("email_address", "sandbox buyer"), "linked_at": time.time()}
    _append_evidence(d, {"type": "wallet_linked", "vault_id": res["id"], "payer_email": d["wallet"]["email"],
                         "consent": "Buyer approved PayPal billing agreement (setup token %s)" % st})
    _save(d)
    return d["wallet"]


def unlink():
    d = _db(); d["wallet"] = {}; _save(d)


# ---------- codes ----------
def _h(code):
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _rules(c):
    return {"cap": c["cap"], "spent_before": c["spent"], "expires_at": datetime.fromtimestamp(c["expires"], timezone.utc).isoformat(timespec="seconds"),
            "merchant_lock": c.get("merchant_lock"), "multi_use": c["multi_use"], "uses_before": c["uses"]}


def issue_code(cap, minutes, label="", multi_use=False, merchant_lock=None, issued_to="user"):
    if not wallet().get("vault_id"):
        raise ValueError("Link your PayPal first.")
    if merchant_lock and merchant_lock not in MERCHANTS:
        raise ValueError("Unknown merchant")
    code = "SP-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
    d = _db()
    c = {"hint": code[-4:], "label": label or "Untitled", "cap": round(cap, 2), "spent": 0.0,
         "created": time.time(), "expires": time.time() + minutes * 60, "multi_use": multi_use,
         "merchant_lock": merchant_lock or None, "issued_to": issued_to, "uses": 0, "revoked": False}
    d["codes"][_h(code)] = c
    _append_evidence(d, {"type": "code_issued", "code": {"hint": c["hint"], "label": c["label"], "code_sha256": _h(code)},
                         "issued_to": issued_to, "rules": _rules(c)})
    _save(d)
    return code


def revoke(hint):
    d = _db()
    for c in d["codes"].values():
        if c["hint"] == hint and not c["revoked"]:
            c["revoked"] = True
            _append_evidence(d, {"type": "code_revoked", "code": {"hint": hint, "label": c["label"]}})
    _save(d)


def _status(c):
    if c["revoked"]: return "revoked"
    if time.time() > c["expires"]: return "expired"
    if not c["multi_use"] and c["uses"] >= 1: return "used"
    if c["spent"] >= c["cap"] - 1e-9: return "exhausted"
    return "active"


def list_codes():
    return [{**c, "status": _status(c), "remaining": round(c["cap"] - c["spent"], 2),
             "merchant_name": MERCHANTS.get(c.get("merchant_lock") or "", {}).get("name"),
             "minutes_left": max(0, int((c["expires"] - time.time()) / 60))}
            for c in sorted(_db()["codes"].values(), key=lambda c: -c["created"])]


def transactions():
    return list(reversed(_db()["transactions"]))[:50]


def redeem(code, amount, merchant_id, basket=None, channel="human", client=None, rail="paypal"):
    """Merchant side: check every rule, charge the vaulted PayPal into the MERCHANT's account,
    and write an evidence record either way."""
    d = _db()
    m = MERCHANTS.get(merchant_id)
    c = d["codes"].get(_h(code))
    amount = round(float(amount), 2)
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": ok, "detail": detail})
        return ok

    ok = check("merchant_known", bool(m), merchant_id)
    ok = ok and check("code_exists", bool(c), "code recognised" if c else "unknown code")
    if ok:
        st = _status(c)
        ok = check("code_active", st == "active", st)
        ok = ok and check("merchant_lock", not c.get("merchant_lock") or c["merchant_lock"] == merchant_id,
                          f"locked to {MERCHANTS[c['merchant_lock']]['name']}" if c.get("merchant_lock") else "any merchant")
        remaining = round(c["cap"] - c["spent"], 2)
        ok = ok and check("within_cap", amount <= remaining + 1e-9, f"£{amount:.2f} vs £{remaining:.2f} remaining")
        ok = ok and check("wallet_linked", bool(d["wallet"].get("vault_id")), "vaulted PayPal present")
    reason = next((f"{x['check']}: {x['detail']}" for x in checks if not x["passed"]), None)

    request = {"merchant_id": merchant_id, "merchant_name": (m or {}).get("name"), "amount": amount,
               "currency": "PYUSD" if rail == "pyusd" else CURRENCY, "rail": rail,
               "basket": basket or [], "channel": channel, "client": client or {}}
    eid = _new_eid()
    code_ref = {"hint": c["hint"], "label": c["label"]} if c else {"hint": "????"}
    settlement = None
    if ok and rail == "pyusd":
        ev_hash = escrow.evidence_hash(eid)
        try:
            pid, txh = escrow.pay(d, merchant_id, amount, ev_hash)
            settlement = {"rail": "pyusd_escrow", "mode": escrow.MODE, "escrow_payment_id": pid, "tx": txh,
                          "tx_link": escrow.tx_link(txh), "status": "Escrowed", "evidence_hash": ev_hash,
                          "paid_to": escrow.MERCHANT_WALLETS.get(merchant_id) or f"{m['name']} wallet (simulated)",
                          "refund_window_seconds": escrow.SIM_WINDOW if escrow.MODE == "simulated" else "per contract"}
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"Escrow: {e}"
            checks.append({"check": "escrow_pay", "passed": False, "detail": str(e)})
    elif ok:
        payee = m["payee_email"]
        pu = {"amount": {"currency_code": CURRENCY, "value": f"{amount:.2f}"},
              "description": f"{m['name']} via ScopePay code …{c['hint']}", "custom_id": f"paycode-{c['hint']}"}
        if basket:
            pu["items"] = [{"name": i["name"][:127], "quantity": str(i["qty"]),
                            "unit_amount": {"currency_code": CURRENCY, "value": f"{i['price']:.2f}"}} for i in basket]
            pu["amount"]["breakdown"] = {"item_total": {"currency_code": CURRENCY,
                                                        "value": f"{sum(i['price'] * i['qty'] for i in basket):.2f}"}}
        vault = d["wallet"]["vault_id"]
        # Direct settlement (payee on the order) only works if the merchant holds the billing agreement.
        # A wallet vaulted by PayCodes belongs to PayCodes, so we fall back to: capture to PayCodes, then Payouts to merchant.
        try_direct = bool(payee) and SETTLEMENT in ("auto", "direct") and d["wallet"].get("direct_payee_ok") is not False
        try:
            order, model = None, "platform_then_payout" if payee else "platform"
            if try_direct:
                try:
                    order = _call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [{**pu, "payee": {"email_address": payee}}],
                                                                  "payment_source": {"paypal": {"vault_id": vault}}})
                    model = "direct"; d["wallet"]["direct_payee_ok"] = True
                except PayPalError as e:
                    if SETTLEMENT == "direct":
                        raise
                    d["wallet"]["direct_payee_ok"] = False
                    checks.append({"check": "direct_payee", "passed": False,
                                   "detail": f"PayPal won't charge this vault for another payee ({str(e)[:90]}…); using capture + payout"})
            if order is None:
                order = _call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [pu],
                                                              "payment_source": {"paypal": {"vault_id": vault}}})
            if order["status"] != "COMPLETED":
                order = _call("POST", f"/v2/checkout/orders/{order['id']}/capture")
            unit = order["purchase_units"][0]
            cap = unit["payments"]["captures"][0]
            settlement = {"rail": "paypal", "settlement_model": model, "paypal_order_id": order["id"], "paypal_capture_id": cap["id"],
                          "capture_status": cap["status"], "capture_time": cap.get("create_time"), "payer_vault_id": vault,
                          "paid_to": unit.get("payee", {}).get("email_address") if model == "direct" else "ScopePay platform account",
                          "payee_merchant_id": unit.get("payee", {}).get("merchant_id")}
            if model == "platform_then_payout":
                try:
                    po = _call("POST", "/v1/payments/payouts", {
                        "sender_batch_header": {"sender_batch_id": f"paycodes-{eid}", "recipient_type": "EMAIL",
                                                "email_subject": f"ScopePay payment from a customer ({eid})"},
                        "items": [{"recipient_type": "EMAIL", "receiver": payee, "sender_item_id": eid,
                                   "amount": {"currency": CURRENCY, "value": f"{amount:.2f}"},
                                   "note": f"{m['name']} order via ScopePay code …{c['hint']} (PayPal capture {cap['id']})"}]})
                    bh = po.get("batch_header", {})
                    settlement.update(paid_to=payee, payout_batch_id=bh.get("payout_batch_id"), payout_status=bh.get("batch_status"))
                    checks.append({"check": "merchant_payout", "passed": True, "detail": f"payout {bh.get('payout_batch_id')} {bh.get('batch_status')}"})
                except PayPalError as e:
                    settlement.update(payout_error=str(e))
                    checks.append({"check": "merchant_payout", "passed": False,
                                   "detail": f"captured to platform but payout failed: {e}. Enable Payouts on the sandbox app."})
        except PayPalError as e:
            ok, reason = False, str(e)
            checks.append({"check": "paypal_capture", "passed": False, "detail": str(e)})

    rules_snapshot = _rules(c) if c else None
    result = "APPROVED" if ok else ("ERROR" if reason and (reason.startswith("PayPal") or reason.startswith("Escrow")) else "DECLINED")
    if ok:
        c["spent"] = round(c["spent"] + amount, 2); c["uses"] += 1
    ev = _append_evidence(d, {"type": "payment_attempt", "code": code_ref, "rules_at_time": rules_snapshot,
                              "request": request, "checks": checks, "decision": result, "reason": reason, "settlement": settlement}, eid)
    d["transactions"].append({"time": time.time(), "merchant": request["merchant_name"] or merchant_id, "amount": amount,
                              "code_hint": code_ref["hint"], "result": result, "evidence_id": ev["id"],
                              "rail": rail, "detail": reason or (
                                  f"PYUSD escrowed #{settlement['escrow_payment_id']} for {settlement['paid_to']}" if rail == "pyusd"
                                  else f"paid to {settlement['paid_to']}" + (" via payout" if settlement.get("payout_batch_id") else "")
                                  + (" ⚠ payout failed" if settlement.get("payout_error") else "") + f" · order {settlement['paypal_order_id']}")})
    _save(d)
    out = {"approved": ok, "evidence_id": ev["id"]}
    out.update(settlement or {"reason": reason})
    return out


# ---------- escrow actions (dispute / refund / release) ----------
ACTION_LABELS = {"dispute": "Payer disputed", "merchant_refund": "Merchant refunded", "release": "Released to merchant",
                 "resolve_refund": "Arbiter refunded payer", "resolve_release": "Arbiter paid merchant"}


def escrow_action(eid, action):
    d = _db()
    rec = next((r for r in d["evidence"] if r["id"] == eid), None)
    st = (rec or {}).get("settlement") or {}
    if st.get("rail") != "pyusd_escrow":
        raise ValueError("Not a PYUSD escrow payment")
    pid = st["escrow_payment_id"]
    mid = rec["request"]["merchant_id"]
    txh = escrow.act(d, pid, action, mid)  # raises EscrowError if the contract would revert
    after = escrow.status(d, pid)
    if after["status"] == "Refunded":  # give the money back to the code's budget
        for c in d["codes"].values():
            if c["hint"] == rec["code"]["hint"]:
                c["spent"] = round(max(0.0, c["spent"] - rec["request"]["amount"]), 2)
                c["uses"] = max(0, c["uses"] - 1)
    ev = _append_evidence(d, {"type": "escrow_action", "ref": eid, "action": action, "label": ACTION_LABELS.get(action, action),
                              "escrow_payment_id": pid, "tx": txh, "tx_link": escrow.tx_link(txh), "status_after": after["status"]})
    d["transactions"].append({"time": time.time(), "merchant": rec["request"]["merchant_name"], "amount": rec["request"]["amount"],
                              "code_hint": rec["code"]["hint"], "result": after["status"].upper(), "evidence_id": eid, "rail": "pyusd",
                              "detail": f"{ACTION_LABELS.get(action, action)} · escrow #{pid}"})
    _save(d)
    return {"status": after, "evidence_id": ev["id"], "tx": txh, "tx_link": escrow.tx_link(txh)}
