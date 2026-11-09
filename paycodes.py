"""ScopePay core: PayPal vault + disposable codes (cap, expiry, merchant lock, single/multi use)
+ tamper-evident evidence trail for disputes.

Safety layer around money movement:
  * reservations under a lock, so parallel payments can never spend past a code's cap
  * stable PayPal-Request-Id per payment, so a retry can never charge twice
  * UNCERTAIN state + reconcile() when PayPal gives no definite answer
  * AI intent check (ai.py) that can only hold or decline, never approve
  * human approval for held payments, bound to a hash of the exact request
  * verified PayPal webhooks: refunds and reversals made in PayPal restore the code's budget
  * deterministic guards before any AI (guards.py): hidden characters / instruction text in baskets, real catalog prices
  * duplicate guard: Idempotency-Key replays the first answer; an identical repeat within the window is declined
  * Ed25519-signed evidence records (signing.py), verifiable with the public key alone
  * optional PayPal authorization hold for held payments (HOLD_MODE=authorize): capture on approve, void on reject
"""
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

import ai
import escrow
import guards
import signing

load_dotenv()
BASE = "https://api-m.sandbox.paypal.com"
DB = Path(__file__).with_name("paycodes_db.json")
CURRENCY = "GBP"
SETTLEMENT = os.getenv("SETTLEMENT", "auto")  # auto | direct | payout (capture to PayCodes, then Payouts to merchant)
HOLD_MODE = os.getenv("HOLD_MODE", "app")  # app: hold the code's budget only | authorize: also place a PayPal authorization
CATALOG_CHECK = os.getenv("CATALOG_CHECK", "1") != "0"  # basket lines must match the shop's catalog prices
DUPLICATE_WINDOW = int(os.getenv("DUPLICATE_WINDOW_SECONDS", "60"))  # decline an identical payment repeated this soon

# Demo merchants. Each one is paid into ITS OWN sandbox business account (payee).
MERCHANTS = {
    "greenleaf": {"name": "Greenleaf Grocer", "emoji": "🥬", "payee_email": os.getenv("GREENLEAF_PAYEE_EMAIL", "")},
    "northside": {"name": "Northside Electronics", "emoji": "🔌", "payee_email": os.getenv("NORTHSIDE_PAYEE_EMAIL", "")},
}

_LOCK = threading.RLock()  # one writer at a time for the JSON store (uvicorn runs sync endpoints in a thread pool)


class PayPalError(Exception):
    pass


class PayPalUncertain(PayPalError):
    """PayPal gave no definite answer (timeout, dropped connection, 5xx): the money may or may not have moved."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db():
    d = json.loads(DB.read_text()) if DB.exists() else {}
    for k, v in {"wallet": {}, "codes": {}, "transactions": [], "evidence": [],
                 "held": {}, "pending": {}, "webhook_events": [], "refunds": {}, "idempotency": {}}.items():
        d.setdefault(k, v)
    for c in d["codes"].values():  # older records predate these fields
        c.setdefault("reserved", 0.0); c.setdefault("in_flight", 0)
        c.setdefault("intent", None); c.setdefault("approve_over", None)
    return d


def _save(d):
    tmp = DB.with_name(DB.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, DB)  # atomic: a crash mid-write can't leave half a file


@contextmanager
def _tx():
    """Read-modify-write the store under the lock. Nothing is saved if the block raises."""
    with _LOCK:
        d = _db()
        yield d
        _save(d)


# ---------- PayPal REST ----------
_TOKEN = {"value": None, "expires": 0.0}
_TOKEN_LOCK = threading.Lock()


def _token(force=False):
    with _TOKEN_LOCK:
        if not force and _TOKEN["value"] and time.time() < _TOKEN["expires"]:
            return _TOKEN["value"]
        r = requests.post(f"{BASE}/v1/oauth2/token",
                          auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                          data={"grant_type": "client_credentials"}, timeout=20)
        r.raise_for_status()
        j = r.json()
        _TOKEN.update(value=j["access_token"], expires=time.time() + int(j.get("expires_in", 3600)) - 120)
        return _TOKEN["value"]


def _call(method, path, body=None, request_id=None):
    """Call PayPal. Pass a stable request_id for anything that moves money: PayPal then treats a repeat
    of the same id as the same request and returns the original result instead of charging again.
    Such calls raise PayPalUncertain when PayPal gives no definite answer."""
    money = request_id is not None
    rid = request_id or str(uuid.uuid4())
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {_token(force=attempt == 2)}", "Content-Type": "application/json",
                   "PayPal-Request-Id": rid, "Prefer": "return=representation"}
        try:
            r = requests.request(method, f"{BASE}{path}", json=body, timeout=30, headers=headers)
        except (requests.Timeout, requests.ConnectionError) as e:
            if money:
                raise PayPalUncertain(f"PayPal did not answer ({type(e).__name__}); the payment may or may not have gone through") from e
            raise PayPalError(f"PayPal unreachable: {e}") from e
        if r.status_code == 401 and attempt == 1:
            continue  # token expired early: refresh once and retry with the same request id
        break
    if money and r.status_code >= 500:
        raise PayPalUncertain(f"PayPal {r.status_code}: no definite answer; the payment may or may not have gone through")
    if not r.ok:
        try:
            detail = r.json()
            msg = detail.get("message") or detail.get("name")
            issues = "; ".join(f"{x.get('field') or x.get('issue')}: {x.get('description') or x.get('issue')}" for x in detail.get("details", []))
        except ValueError:
            msg, issues = r.text, ""
        raise PayPalError(f"PayPal {r.status_code}: {msg} {issues}".strip())
    return r.json() if r.content else {}


# ---------- evidence trail (hash-chained, append-only) ----------
def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _new_eid():
    return "EV-" + secrets.token_hex(4).upper()


_SIG_FIELDS = ("hash", "sig", "sig_alg", "key_id")


def _signer():
    return signing.signer_for(os.getenv("EVIDENCE_SIGNING_KEY_FILE") or DB.with_name("evidence_signing_key.pem"))


def evidence_public_key():
    s = _signer()
    return s.public() if s else {"alg": None, "detail": "install the cryptography package to sign evidence"}


def _append_evidence(d, record, eid=None):
    prev = d["evidence"][-1]["hash"] if d["evidence"] else "GENESIS"
    record = {"id": eid or _new_eid(), "seq": len(d["evidence"]) + 1,
              "recorded_at": _now_iso(), **record, "prev_hash": prev}
    record["hash"] = hashlib.sha256(_canon(record).encode()).hexdigest()
    s = _signer()
    if s:  # the signature covers the hash, which covers everything else; it is not part of the hash itself
        record.update(s.sign(record["hash"]))
    d["evidence"].append(record)
    return record


def verify_evidence():
    ev = _db()["evidence"]
    s = _signer()
    pub = s.public() if s else None
    prev, signed, unsigned = "GENESIS", 0, 0
    for r in ev:
        body = {k: v for k, v in r.items() if k not in _SIG_FIELDS}
        if r["prev_hash"] != prev or hashlib.sha256(_canon(body).encode()).hexdigest() != r["hash"]:
            return {"valid": False, "records": len(ev), "broken_at": r["id"], "problem": "hash chain"}
        if r.get("sig"):
            if pub and (r.get("key_id") != pub["key_id"] or not signing.verify(r["hash"], r["sig"], pub["public_key_b64"])):
                return {"valid": False, "records": len(ev), "broken_at": r["id"], "problem": "signature"}
            signed += 1
        else:
            unsigned += 1
        prev = r["hash"]
    return {"valid": True, "records": len(ev), "head": prev, "signed": signed, "unsigned": unsigned,
            "signatures_checked": bool(pub), "public_key": pub}


def get_evidence(eid):
    ev = _db()["evidence"]
    rec = next((r for r in ev if r["id"] == eid), None)
    if not rec:
        return None
    # include the code's issuance record so the bundle shows the rules the user set
    issuance = next((r for r in ev if r["type"] == "code_issued" and r["code"]["hint"] == rec.get("code", {}).get("hint")), None)
    actions = [r for r in ev if r["type"] == "escrow_action" and r.get("ref") == eid]
    related = [r for r in ev if r.get("ref") == eid and r["type"] != "escrow_action"]  # approvals, reconcile, PayPal events
    live = None
    st = rec.get("settlement") or {}
    if st.get("rail") == "pyusd_escrow":
        try:
            live = escrow.status(_db(), st["escrow_payment_id"])
        except Exception as e:  # noqa: BLE001
            live = {"error": str(e)}
    return {"record": rec, "code_issuance": issuance, "escrow_actions": actions, "related": related,
            "escrow_status": live, "chain": verify_evidence()}


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
    with _tx() as d:
        d["wallet"]["setup_token"] = res["id"]
    return next(l["href"] for l in res["links"] if l["rel"] == "approve")


def finish_link(setup_token=None):
    st = setup_token or _db()["wallet"].get("setup_token")
    res = _call("POST", "/v3/vault/payment-tokens", {"payment_source": {"token": {"id": st, "type": "SETUP_TOKEN"}}})
    pp = res.get("payment_source", {}).get("paypal", {})
    with _tx() as d:
        d["wallet"] = {"vault_id": res["id"], "customer_id": res.get("customer", {}).get("id"),
                       "email": pp.get("email_address", "sandbox buyer"), "linked_at": time.time()}
        _append_evidence(d, {"type": "wallet_linked", "vault_id": res["id"], "payer_email": d["wallet"]["email"],
                             "consent": "Buyer approved PayPal billing agreement (setup token %s)" % st})
        return d["wallet"]


def unlink():
    with _tx() as d:
        d["wallet"] = {}


def seed_demo_wallet():
    """Hosted demo (e.g. Render, where local files reset on restart): restore a sandbox wallet that was linked
    earlier, from DEMO_VAULT_ID, so visitors can try ScopePay without their own PayPal sandbox login.
    The vault id stays valid at PayPal; this only puts it back in the empty local store."""
    vault = os.getenv("DEMO_VAULT_ID", "").strip()
    if not vault:
        return None
    with _tx() as d:
        if d["wallet"].get("vault_id"):
            return d["wallet"]
        d["wallet"] = {"vault_id": vault, "email": os.getenv("DEMO_PAYER_EMAIL", "sandbox buyer"),
                       "linked_at": time.time(), "restored_from_env": True}
        _append_evidence(d, {"type": "wallet_linked", "vault_id": vault, "payer_email": d["wallet"]["email"],
                             "consent": "Restored from DEMO_VAULT_ID: a sandbox PayPal vault the buyer approved earlier"})
        return d["wallet"]


def code_issued_to(code):
    c = _db()["codes"].get(_h(code))
    return c.get("issued_to") if c else None


def record_event(event_type, **fields):
    """Add a non-payment event (e.g. an automation trigger) to the evidence trail."""
    with _tx() as d:
        return _append_evidence(d, {"type": event_type, **fields})["id"]


# ---------- codes ----------
def _h(code):
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _rules(c):
    return {"cap": c["cap"], "spent_before": c["spent"], "reserved_before": c.get("reserved", 0.0),
            "expires_at": datetime.fromtimestamp(c["expires"], timezone.utc).isoformat(timespec="seconds"),
            "merchant_lock": c.get("merchant_lock"), "multi_use": c["multi_use"], "uses_before": c["uses"],
            "intent": c.get("intent"), "approve_over": c.get("approve_over")}


def issue_code(cap, minutes, label="", multi_use=False, merchant_lock=None, issued_to="user", intent=None, approve_over=None):
    if not wallet().get("vault_id"):
        raise ValueError("Link your PayPal first.")
    if merchant_lock and merchant_lock not in MERCHANTS:
        raise ValueError("Unknown merchant")
    if not cap or cap <= 0 or not minutes or minutes <= 0:
        raise ValueError("Cap and expiry must be positive.")
    intent = (intent or "").strip()[:500] or None
    approve_over = round(float(approve_over), 2) if approve_over not in (None, "") else None
    code = "SP-" + "-".join(secrets.token_hex(2).upper() for _ in range(3))
    with _tx() as d:
        c = {"hint": code[-4:], "label": label or "Untitled", "cap": round(cap, 2), "spent": 0.0, "reserved": 0.0,
             "created": time.time(), "expires": time.time() + minutes * 60, "multi_use": multi_use,
             "merchant_lock": merchant_lock or None, "issued_to": issued_to, "uses": 0, "in_flight": 0,
             "revoked": False, "intent": intent, "approve_over": approve_over}
        d["codes"][_h(code)] = c
        _append_evidence(d, {"type": "code_issued", "code": {"hint": c["hint"], "label": c["label"], "code_sha256": _h(code)},
                             "issued_to": issued_to, "rules": _rules(c)})
    return code


def revoke(hint):
    with _tx() as d:
        for c in d["codes"].values():
            if c["hint"] == hint and not c["revoked"]:
                c["revoked"] = True
                _append_evidence(d, {"type": "code_revoked", "code": {"hint": hint, "label": c["label"]}})


def _status(c):
    if c["revoked"]: return "revoked"
    if time.time() > c["expires"]: return "expired"
    if not c["multi_use"] and c["uses"] >= 1: return "used"
    if c["spent"] >= c["cap"] - 1e-9: return "exhausted"
    if not c["multi_use"] and c.get("in_flight", 0) >= 1: return "in_use"     # a payment is in progress or held
    if c["spent"] + c.get("reserved", 0.0) >= c["cap"] - 1e-9: return "in_use"
    return "active"


def _remaining(c):
    return round(c["cap"] - c["spent"] - c.get("reserved", 0.0), 2)


def list_codes():
    return [{**c, "status": _status(c), "remaining": _remaining(c),
             "merchant_name": MERCHANTS.get(c.get("merchant_lock") or "", {}).get("name"),
             "minutes_left": max(0, int((c["expires"] - time.time()) / 60))}
            for c in sorted(_db()["codes"].values(), key=lambda c: -c["created"])]


def code_info(code):
    """What an agent holding a code may know about it (never the payer's PayPal details)."""
    c = _db()["codes"].get(_h(code))
    if not c:
        return {"status": "unknown", "detail": "This code is not recognised."}
    return {"status": _status(c), "label": c["label"], "cap": c["cap"], "remaining": _remaining(c), "currency": CURRENCY,
            "expires_at": datetime.fromtimestamp(c["expires"], timezone.utc).isoformat(timespec="seconds"),
            "merchant_lock": c.get("merchant_lock"), "merchant_name": MERCHANTS.get(c.get("merchant_lock") or "", {}).get("name"),
            "multi_use": c["multi_use"], "intent": c.get("intent"), "approve_over": c.get("approve_over")}


def transactions():
    return list(reversed(_db()["transactions"]))[:50]


# ---------- payments ----------
def _recent_duplicate(d, request_hash):
    """The latest payment with the same fingerprint inside the window, if it went through or is still going."""
    now = time.time()
    for t in reversed(d["transactions"]):
        if now - t["time"] > DUPLICATE_WINDOW:
            return None
        if t.get("request_hash") == request_hash:
            return t if t["result"] in ("APPROVED", "HELD", "UNCERTAIN") else None
    return None


def _rule_checks(d, c, m, merchant_id, amount, basket, request_hash=None):
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "passed": ok, "detail": detail})
        return ok

    ok = check("merchant_known", bool(m), merchant_id)
    ok = ok and check("code_exists", bool(c), "code recognised" if c else "unknown code")
    ok = ok and check("amount_positive", amount > 0, f"£{amount:.2f}")
    if ok and basket:
        total = round(sum(i["price"] * i["qty"] for i in basket), 2)
        ok = check("basket_matches", abs(total - amount) < 0.005, f"items £{total:.2f} vs charge £{amount:.2f}")
    if ok and basket:
        findings = guards.scan_basket(basket)
        ok = check("basket_clean", not findings,
                   f"{findings[0][1]} in “{findings[0][0]}”" if findings else "no hidden characters or text aimed at an AI")
    if ok and basket and CATALOG_CHECK:
        ok = check("catalog_prices", *guards.catalog_prices(merchant_id, basket))
    if ok:
        st = _status(c)
        ok = check("code_active", st == "active", "a payment with this code is already in progress" if st == "in_use" and not c["multi_use"] else st)
        if ok and DUPLICATE_WINDOW > 0 and request_hash:
            dup = _recent_duplicate(d, request_hash)
            ok = check("not_duplicate", dup is None,
                       f"identical to {dup['evidence_id']} ({dup['result'].lower()}) {int(time.time() - dup['time'])}s ago; "
                       f"wait {DUPLICATE_WINDOW}s or change the basket" if dup else f"no identical payment in the last {DUPLICATE_WINDOW}s")
        ok = ok and check("merchant_lock", not c.get("merchant_lock") or c["merchant_lock"] == merchant_id,
                          f"locked to {MERCHANTS[c['merchant_lock']]['name']}" if c.get("merchant_lock") else "any merchant")
        remaining = _remaining(c)
        ok = ok and check("within_cap", amount <= remaining + 1e-9, f"£{amount:.2f} vs £{remaining:.2f} remaining")
        ok = ok and check("wallet_linked", bool(d["wallet"].get("vault_id")), "vaulted PayPal present")
    return ok, checks


def _request_hash(code_key, request):
    """Fingerprint of exactly what would be paid. A human approves this hash, not a description of it."""
    core = {k: request.get(k) for k in ("merchant_id", "amount", "currency", "rail", "basket", "channel")}
    return hashlib.sha256(_canon({"code_sha256": code_key, **core}).encode()).hexdigest()


def _reserve(c, amount):
    c["reserved"] = round(c.get("reserved", 0.0) + amount, 2); c["in_flight"] = c.get("in_flight", 0) + 1


def _release(c, amount):
    c["reserved"] = round(max(0.0, c.get("reserved", 0.0) - amount), 2); c["in_flight"] = max(0, c.get("in_flight", 0) - 1)


def _tx_detail(decision, reason, settlement):
    if reason or not settlement:
        return reason or decision.lower()
    if settlement.get("rail") == "pyusd_escrow":
        return f"PYUSD escrowed #{settlement['escrow_payment_id']} for {settlement['paid_to']}"
    return (f"paid to {settlement['paid_to']}" + (" via payout" if settlement.get("payout_batch_id") else "")
            + (" ⚠ payout failed" if settlement.get("payout_error") else "") + f" · order {settlement['paypal_order_id']}")


def _record(d, eid, code_ref, rules, request, checks, decision, reason, settlement, extra=None):
    ev = _append_evidence(d, {"type": "payment_attempt", "code": code_ref, "rules_at_time": rules, "request": request,
                              "checks": checks, "decision": decision, "reason": reason, "settlement": settlement, **(extra or {})}, eid)
    d["transactions"].append({"time": time.time(), "merchant": request.get("merchant_name") or request["merchant_id"],
                              "amount": request["amount"], "code_hint": code_ref["hint"], "result": decision,
                              "evidence_id": ev["id"], "rail": request["rail"], "detail": _tx_detail(decision, reason, settlement),
                              "request_hash": request.get("request_hash")})
    return ev


def _build_request(code, amount, merchant_id, basket, channel, client, rail):
    m = MERCHANTS.get(merchant_id)
    request = {"merchant_id": merchant_id, "merchant_name": (m or {}).get("name"), "amount": round(float(amount), 2),
               "currency": "PYUSD" if rail == "pyusd" else CURRENCY, "rail": rail,
               "basket": basket or [], "channel": channel, "client": client or {}}
    request["request_hash"] = _request_hash(_h(code), request)
    return request


def redeem(code, amount, merchant_id, basket=None, channel="human", client=None, rail="paypal", idempotency_key=None):
    """Merchant side: check every rule, charge the vaulted PayPal into the MERCHANT's account,
    and write an evidence record either way.

    idempotency_key (the merchant's Idempotency-Key header): the same key with the same payment returns the first
    answer instead of paying again; the same key with a different payment is refused."""
    if not idempotency_key:
        return _redeem(code, amount, merchant_id, basket, channel, client, rail)
    rh = _build_request(code, amount, merchant_id, basket, channel, client, rail)["request_hash"]
    slot = f"{merchant_id}:{str(idempotency_key)[:120]}"
    with _tx() as d:
        prior = d["idempotency"].get(slot)
        if prior and prior["request_hash"] != rh:
            raise ValueError("This Idempotency-Key was already used for a different payment.")
        if prior and prior.get("response"):
            return {**prior["response"], "replayed": True}
        if prior:
            return {"approved": False, "in_progress": True, "evidence_id": prior.get("evidence_id"),
                    "reason": "not_duplicate: the same payment is already being processed"}
        d["idempotency"][slot] = {"request_hash": rh, "at": time.time()}
    try:
        out = _redeem(code, amount, merchant_id, basket, channel, client, rail)
    except Exception:
        with _tx() as d:
            d["idempotency"].pop(slot, None)
        raise
    with _tx() as d:
        if out.get("approved") or out.get("held") or out.get("uncertain"):  # money moved, is held, or may have moved
            d["idempotency"][slot].update(response=out, evidence_id=out.get("evidence_id"))
        else:  # a decline or a definite error: a retry with the same key is evaluated again
            d["idempotency"].pop(slot, None)
    return out


def _redeem(code, amount, merchant_id, basket=None, channel="human", client=None, rail="paypal"):
    eid = _new_eid()
    m = MERCHANTS.get(merchant_id)
    key = _h(code)
    request = _build_request(code, amount, merchant_id, basket, channel, client, rail)
    amount, basket = request["amount"], request["basket"]

    # 1. deterministic rules, and reserve the amount before anything slow happens
    with _tx() as d:
        c = d["codes"].get(key)
        ok, checks = _rule_checks(d, c, m, merchant_id, amount, basket, request["request_hash"])
        rules = _rules(c) if c else None
        code_ref = {"hint": c["hint"], "label": c["label"]} if c else {"hint": "????"}
        if not ok:
            reason = next(f"{x['check']}: {x['detail']}" for x in checks if not x["passed"])
            _record(d, eid, code_ref, rules, request, checks, "DECLINED", reason, None)
            return {"approved": False, "evidence_id": eid, "reason": reason}
        _reserve(c, amount)
        intent, approve_over = c.get("intent"), c.get("approve_over")

    # 2. AI intent check: runs only after the rules passed and can only hold or decline
    audit, hold = None, None
    if intent:
        audit = ai.audit_basket(intent, basket, amount, m["name"])
        checks.append({"check": "intent_ai", "passed": audit["verdict"] == "allow",
                       "detail": f"{audit['verdict']}: {audit['summary']}"})
        if audit["verdict"] == "decline":
            reason = f"intent_ai: {audit['summary']}"
            with _tx() as d:
                _release(d["codes"][key], amount)
                _record(d, eid, code_ref, rules, request, checks, "DECLINED", reason, None, {"ai_audit": audit})
            return {"approved": False, "evidence_id": eid, "reason": reason, "ai_audit": audit}
        if audit["verdict"] == "hold":
            hold = f"the AI intent check wants you to look: {audit['summary']}"
    if not hold and approve_over is not None and amount > approve_over:
        hold = f"£{amount:.2f} is above your £{approve_over:.2f} approval threshold"
        checks.append({"check": "approval_threshold", "passed": False, "detail": hold})

    # 3. held: wait for the user, keeping the reservation (and, with HOLD_MODE=authorize, a PayPal authorization)
    if hold:
        rh = request["request_hash"]
        auth = None
        if HOLD_MODE == "authorize" and request["rail"] == "paypal":
            try:
                auth = _paypal_authorize(eid, code_ref["hint"], m, amount, basket, _db()["wallet"].get("vault_id"))
                checks.append({"check": "paypal_authorization", "passed": True,
                               "detail": f"PayPal is holding £{amount:.2f} (authorization {auth['authorization_id']}"
                                         + (f", valid until {auth['expiration_time']}" if auth.get("expiration_time") else "") + ")"})
            except PayPalError as e:  # includes PayPalUncertain: any authorization PayPal did place simply lapses
                checks.append({"check": "paypal_authorization", "passed": False,
                               "detail": f"no PayPal hold ({str(e)[:120]}); ScopePay still holds the code's budget"})
        with _tx() as d:
            d["held"][eid] = {"status": "pending", "code_key": key, "request": request, "request_hash": rh,
                              "checks": checks, "rules": rules, "audit": audit, "reason": hold, "created": time.time(),
                              "authorization": auth}
            _record(d, eid, code_ref, rules, request, checks, "HELD", f"held: {hold}", None,
                    {"ai_audit": audit, "request_hash": rh, "paypal_authorization": auth})
        return {"approved": False, "held": True, "evidence_id": eid, "reason": f"held: {hold}", "request_hash": rh,
                "ai_audit": audit, "paypal_authorization": auth}

    return _settle(eid, key, request, checks, rules, audit)


def _purchase_unit(eid, hint, m, amount, basket):
    pu = {"amount": {"currency_code": CURRENCY, "value": f"{amount:.2f}"},
          "description": f"{m['name']} via ScopePay code …{hint}", "custom_id": f"paycode-{hint}", "invoice_id": eid}
    if basket:
        pu["items"] = [{"name": i["name"][:127], "quantity": str(i["qty"]),
                        "unit_amount": {"currency_code": CURRENCY, "value": f"{i['price']:.2f}"}} for i in basket]
        pu["amount"]["breakdown"] = {"item_total": {"currency_code": CURRENCY,
                                                    "value": f"{sum(i['price'] * i['qty'] for i in basket):.2f}"}}
    return pu


def _paypal_authorize(eid, hint, m, amount, basket, vault):
    """Place a PayPal authorization on the vaulted wallet: the buyer's money is held, not taken.
    Captured if the owner approves, voided if they reject. Settles to the platform (then Payouts), like a capture."""
    order = _call("POST", "/v2/checkout/orders", {"intent": "AUTHORIZE", "purchase_units": [_purchase_unit(eid, hint, m, amount, basket)],
                                                  "payment_source": {"paypal": {"vault_id": vault}}}, request_id=f"{eid}-authorize")
    if order.get("status") != "COMPLETED":
        order = _call("POST", f"/v2/checkout/orders/{order['id']}/authorize", request_id=f"{eid}-authorize-2")
    a = order["purchase_units"][0]["payments"]["authorizations"][0]
    return {"authorization_id": a["id"], "order_id": order["id"], "status": a.get("status"), "expiration_time": a.get("expiration_time")}


def _paypal_void(eid, auth):
    """Release a PayPal authorization. Returns a note for the evidence record; never raises."""
    try:
        _call("POST", f"/v2/payments/authorizations/{auth['authorization_id']}/void", request_id=f"{eid}-void")
        return {"authorization_id": auth["authorization_id"], "voided": True}
    except PayPalError as e:
        return {"authorization_id": auth["authorization_id"], "voided": False, "error": str(e)[:200],
                "detail": "PayPal keeps the hold until it expires; no money is taken"}


def _paypal_settle(eid, hint, m, amount, basket, vault, try_direct, authorization=None):
    """Charge the vault (or capture an earlier authorization). Every call carries a request id derived from the evidence
    id, so calling this again for the same payment (reconcile) returns PayPal's original result instead of charging twice."""
    payee = m["payee_email"]
    notes = []
    if authorization:
        return _capture_authorization(eid, hint, m, amount, vault, authorization, notes)
    pu = _purchase_unit(eid, hint, m, amount, basket)
    # Direct settlement (payee on the order) only works if the merchant holds the billing agreement.
    # A wallet vaulted by ScopePay belongs to ScopePay, so we fall back to: capture to ScopePay, then Payouts to merchant.
    order, model, direct_ok = None, "platform_then_payout" if payee else "platform", None
    if try_direct:
        try:
            order = _call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [{**pu, "payee": {"email_address": payee}}],
                                                          "payment_source": {"paypal": {"vault_id": vault}}}, request_id=f"{eid}-order-direct")
            model, direct_ok = "direct", True
        except PayPalUncertain:
            raise
        except PayPalError as e:
            if SETTLEMENT == "direct":
                raise
            direct_ok = False
            notes.append({"check": "direct_payee", "passed": False,
                          "detail": f"PayPal won't charge this vault for another payee ({str(e)[:90]}…); using capture + payout"})
    if order is None:
        order = _call("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [pu],
                                                      "payment_source": {"paypal": {"vault_id": vault}}}, request_id=f"{eid}-order")
    if order["status"] != "COMPLETED":
        order = _call("POST", f"/v2/checkout/orders/{order['id']}/capture", request_id=f"{eid}-capture")
    unit = order["purchase_units"][0]
    cap = unit["payments"]["captures"][0]
    settlement = {"rail": "paypal", "settlement_model": model, "paypal_order_id": order["id"], "paypal_capture_id": cap["id"],
                  "capture_status": cap["status"], "capture_time": cap.get("create_time"), "payer_vault_id": vault,
                  "paypal_request_id": f"{eid}-order-direct" if model == "direct" else f"{eid}-order",
                  "paid_to": unit.get("payee", {}).get("email_address") if model == "direct" else "ScopePay platform account",
                  "payee_merchant_id": unit.get("payee", {}).get("merchant_id")}
    _payout_merchant(eid, hint, m, amount, cap["id"], model, settlement, notes)
    return settlement, notes, direct_ok


def _capture_authorization(eid, hint, m, amount, vault, auth, notes):
    cap = _call("POST", f"/v2/payments/authorizations/{auth['authorization_id']}/capture", {"final_capture": True},
                request_id=f"{eid}-auth-capture")
    model = "platform_then_payout" if m["payee_email"] else "platform"
    settlement = {"rail": "paypal", "settlement_model": model, "hold": "paypal_authorization",
                  "paypal_order_id": auth.get("order_id"), "paypal_authorization_id": auth["authorization_id"],
                  "paypal_capture_id": cap.get("id"), "capture_status": cap.get("status"), "capture_time": cap.get("create_time"),
                  "payer_vault_id": vault, "paypal_request_id": f"{eid}-auth-capture", "paid_to": "ScopePay platform account"}
    _payout_merchant(eid, hint, m, amount, cap.get("id"), model, settlement, notes)
    return settlement, notes, None


def _payout_merchant(eid, hint, m, amount, capture_id, model, settlement, notes):
    payee = m["payee_email"]
    cap = {"id": capture_id}
    if model == "platform_then_payout":
        try:  # sender_batch_id is PayPal's own idempotency key for payouts
            po = _call("POST", "/v1/payments/payouts", {
                "sender_batch_header": {"sender_batch_id": f"paycodes-{eid}", "recipient_type": "EMAIL",
                                        "email_subject": f"ScopePay payment from a customer ({eid})"},
                "items": [{"recipient_type": "EMAIL", "receiver": payee, "sender_item_id": eid,
                           "amount": {"currency": CURRENCY, "value": f"{amount:.2f}"},
                           "note": f"{m['name']} order via ScopePay code …{hint} (PayPal capture {cap['id']})"}]},
                request_id=f"{eid}-payout")
            bh = po.get("batch_header", {})
            settlement.update(paid_to=payee, payout_batch_id=bh.get("payout_batch_id"), payout_status=bh.get("batch_status"))
            notes.append({"check": "merchant_payout", "passed": True, "detail": f"payout {bh.get('payout_batch_id')} {bh.get('batch_status')}"})
        except PayPalError as e:  # the buyer was charged; the payout can be retried from the evidence record
            settlement.update(payout_error=str(e))
            notes.append({"check": "merchant_payout", "passed": False,
                          "detail": f"captured to platform but payout failed: {e}. Enable Payouts on the sandbox app."})


def _settle(eid, key, request, checks, rules, audit=None, authorization=None):
    """Move the money for a payment whose rules passed and whose amount is already reserved."""
    amount, merchant_id = request["amount"], request["merchant_id"]
    m = MERCHANTS[merchant_id]
    extra = {"ai_audit": audit} if audit else {}
    if request.get("approval_of"):
        extra["approval_of"] = request["approval_of"]

    if request["rail"] == "pyusd":  # simulated escrow is instant; keep it inside one transaction
        with _tx() as d:
            c = d["codes"][key]
            code_ref = {"hint": c["hint"], "label": c["label"]}
            _release(c, amount)
            ev_hash = escrow.evidence_hash(eid)
            try:
                pid, txh = escrow.pay(d, merchant_id, amount, ev_hash)
                settlement = {"rail": "pyusd_escrow", "mode": escrow.MODE, "escrow_payment_id": pid, "tx": txh,
                              "tx_link": escrow.tx_link(txh), "status": "Escrowed", "evidence_hash": ev_hash,
                              "paid_to": escrow.MERCHANT_WALLETS.get(merchant_id) or f"{m['name']} wallet (simulated)",
                              "refund_window_seconds": escrow.SIM_WINDOW if escrow.MODE == "simulated" else "per contract"}
            except Exception as e:  # noqa: BLE001
                reason = f"Escrow: {e}"
                checks.append({"check": "escrow_pay", "passed": False, "detail": str(e)})
                _record(d, eid, code_ref, rules, request, checks, "ERROR", reason, None, extra)
                return {"approved": False, "evidence_id": eid, "reason": reason}
            c["spent"] = round(c["spent"] + amount, 2); c["uses"] += 1
            _record(d, eid, code_ref, rules, request, checks, "APPROVED", None, settlement, extra)
        return {"approved": True, "evidence_id": eid, **settlement}

    with _LOCK:
        d = _db()
        c = d["codes"][key]
        hint, vault = c["hint"], d["wallet"].get("vault_id")
        try_direct = (not authorization and bool(m["payee_email"]) and SETTLEMENT in ("auto", "direct")
                      and d["wallet"].get("direct_payee_ok") is not False)
    code_ref = {"hint": hint, "label": c["label"]}
    decision, reason, settlement, release = "APPROVED", None, None, True
    try:
        settlement, notes, direct_ok = _paypal_settle(eid, hint, m, amount, request["basket"], vault, try_direct, authorization)
        checks.extend(notes)
    except PayPalUncertain as e:
        decision, release, direct_ok = "UNCERTAIN", False, None
        reason = f"paypal_uncertain: {e}. Your budget stays reserved until ScopePay checks with PayPal (Reconcile)."
        checks.append({"check": "paypal_capture", "passed": False, "detail": str(e)})
    except PayPalError as e:
        decision, reason, direct_ok = "ERROR", str(e), None
        checks.append({"check": "paypal_capture", "passed": False, "detail": str(e)})

    with _tx() as d:
        c = d["codes"][key]
        if direct_ok is not None:
            d["wallet"]["direct_payee_ok"] = direct_ok
        if release:
            _release(c, amount)
        if decision == "APPROVED":
            c["spent"] = round(c["spent"] + amount, 2); c["uses"] += 1
        if decision == "UNCERTAIN":
            d["pending"][eid] = {"code_key": key, "amount": amount, "hint": hint, "merchant_id": merchant_id,
                                 "basket": request["basket"], "vault_id": vault, "try_direct": try_direct, "created": time.time(),
                                 "authorization": authorization}
        _record(d, eid, code_ref, rules, request, checks, decision, reason, settlement, extra)
    out = {"approved": decision == "APPROVED", "evidence_id": eid}
    if decision == "UNCERTAIN":
        out["uncertain"] = True
    out.update(settlement or {"reason": reason})
    return out


def reconcile(eid):
    """Ask PayPal what happened to an UNCERTAIN payment by replaying it with the same request ids."""
    p = _db()["pending"].get(eid)
    if not p:
        raise ValueError("No payment waiting to be reconciled with that id.")
    m = MERCHANTS[p["merchant_id"]]
    try:
        settlement, notes, _ = _paypal_settle(eid, p["hint"], m, p["amount"], p["basket"], p["vault_id"], p["try_direct"],
                                              p.get("authorization"))
        decision, reason = "APPROVED", None
    except PayPalUncertain as e:
        return {"resolved": False, "evidence_id": eid, "reason": str(e)}
    except PayPalError as e:
        settlement, notes, decision, reason = None, [], "ERROR", str(e)
    with _tx() as d:
        p = d["pending"].pop(eid, None)
        if p is None:
            return {"resolved": True, "evidence_id": eid, "reason": "already reconciled"}
        c = d["codes"][p["code_key"]]
        _release(c, p["amount"])
        if decision == "APPROVED":
            c["spent"] = round(c["spent"] + p["amount"], 2); c["uses"] += 1
        ev = _append_evidence(d, {"type": "payment_reconciled", "ref": eid, "code": {"hint": c["hint"], "label": c["label"]},
                                  "decision": decision, "reason": reason, "checks": notes, "settlement": settlement})
        d["transactions"].append({"time": time.time(), "merchant": m["name"], "amount": p["amount"], "code_hint": c["hint"],
                                  "result": decision, "evidence_id": eid, "rail": "paypal",
                                  "detail": "reconciled with PayPal: " + _tx_detail(decision, reason, settlement)})
    return {"resolved": True, "approved": decision == "APPROVED", "evidence_id": eid, "reconcile_record": ev["id"], **(settlement or {"reason": reason})}


def list_pending():
    return [{"evidence_id": k, **{x: v[x] for x in ("hint", "merchant_id", "amount", "created")}} for k, v in _db()["pending"].items()]


# ---------- held payments: human approval bound to the request hash ----------
def list_held():
    d = _db()
    out = []
    for eid, h in d["held"].items():
        if h["status"] != "pending":
            continue
        c = d["codes"].get(h["code_key"], {})
        r = h["request"]
        out.append({"evidence_id": eid, "code_hint": c.get("hint"), "code_label": c.get("label"), "merchant": r["merchant_name"],
                    "amount": r["amount"], "currency": r["currency"], "basket": r["basket"], "channel": r["channel"],
                    "reason": h["reason"], "ai_summary": (h.get("audit") or {}).get("summary"),
                    "ai_items": (h.get("audit") or {}).get("items"), "request_hash": h["request_hash"], "created": h["created"]})
    return sorted(out, key=lambda x: -x["created"])


def approve_held(eid, request_hash, approver="wallet owner"):
    refused, lapsed = None, None
    with _tx() as d:
        h = d["held"].get(eid)
        if not h or h["status"] != "pending":
            raise ValueError("No payment is waiting for approval with that id.")
        c = d["codes"][h["code_key"]]
        if request_hash != h["request_hash"] or _request_hash(h["code_key"], h["request"]) != h["request_hash"]:
            refused = "the payment changed after it was held, so the approval does not match it"
            _append_evidence(d, {"type": "approval_refused", "ref": eid, "code": {"hint": c["hint"], "label": c["label"]},
                                 "reason": refused, "presented_hash": request_hash, "held_hash": h["request_hash"]})
        else:
            st = _status(c)
            if st in ("revoked", "expired"):
                _release(c, h["request"]["amount"])
                h["status"] = "declined"
                lapsed = {"type": "payment_rejected", "ref": eid, "code": {"hint": c["hint"], "label": c["label"]},
                          "reason": f"code {st} before approval"}
            else:
                h["status"], h["decided_at"] = "approved", time.time()
                _append_evidence(d, {"type": "payment_approved", "ref": eid, "code": {"hint": c["hint"], "label": c["label"]},
                                     "request_hash": request_hash, "approved_by": approver})
    if refused:
        raise ValueError(refused)
    if lapsed:
        if h.get("authorization"):
            lapsed["paypal_void"] = _paypal_void(eid, h["authorization"])
        with _tx() as d:
            _append_evidence(d, lapsed)
        return {"approved": False, "evidence_id": eid, "reason": f"code_active: {lapsed['reason'].split()[1]}"}
    request = {**h["request"], "approval_of": eid}
    checks = h["checks"] + [{"check": "human_approval", "passed": True, "detail": f"approved by {approver}; request hash {request_hash[:12]}…"}]
    return _settle(_new_eid(), h["code_key"], request, checks, h["rules"], h.get("audit"), h.get("authorization"))


def reject_held(eid, approver="wallet owner"):
    pre = _db()["held"].get(eid) or {}
    void = _paypal_void(eid, pre["authorization"]) if pre.get("status") == "pending" and pre.get("authorization") else None
    with _tx() as d:
        h = d["held"].get(eid)
        if not h or h["status"] != "pending":
            raise ValueError("No payment is waiting for approval with that id.")
        c = d["codes"][h["code_key"]]
        _release(c, h["request"]["amount"])
        h["status"], h["decided_at"] = "rejected", time.time()
        _append_evidence(d, {"type": "payment_rejected", "ref": eid, "code": {"hint": c["hint"], "label": c["label"]},
                             "reason": f"rejected by {approver}", **({"paypal_void": void} if void else {})})
        d["transactions"].append({"time": time.time(), "merchant": h["request"]["merchant_name"], "amount": h["request"]["amount"],
                                  "code_hint": c["hint"], "result": "REJECTED", "evidence_id": eid, "rail": h["request"]["rail"],
                                  "detail": f"you rejected it · {h['reason']}" + (" · PayPal hold released" if (void or {}).get("voided") else ""),
                                  "request_hash": h["request"].get("request_hash")})
    return {"rejected": True, "evidence_id": eid}


# ---------- PayPal webhooks ----------
def verify_webhook(headers, event):
    """Ask PayPal whether this event really came from PayPal for our webhook. Fails closed."""
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    if not webhook_id:
        return False
    h = {k.lower(): v for k, v in headers.items()}
    need = ["paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id", "paypal-transmission-sig", "paypal-transmission-time"]
    if any(not h.get(k) for k in need):
        return False
    res = _call("POST", "/v1/notifications/verify-webhook-signature", {
        "auth_algo": h["paypal-auth-algo"], "cert_url": h["paypal-cert-url"], "transmission_id": h["paypal-transmission-id"],
        "transmission_sig": h["paypal-transmission-sig"], "transmission_time": h["paypal-transmission-time"],
        "webhook_id": webhook_id, "webhook_event": event})
    return res.get("verification_status") == "SUCCESS"


def _capture_id_of(resource):
    for link in resource.get("links", []):
        if link.get("rel") == "up" and "/captures/" in link.get("href", ""):
            return link["href"].rstrip("/").rsplit("/", 1)[-1]
    return resource.get("id")


def handle_paypal_event(event):
    """Apply a VERIFIED PayPal event. Refunds and reversals made inside PayPal give the code its budget back."""
    etype, res = event.get("event_type", ""), event.get("resource", {})
    with _tx() as d:
        if event.get("id") in d["webhook_events"]:
            return {"ok": True, "duplicate": True}
        d["webhook_events"] = (d["webhook_events"] + [event.get("id")])[-500:]
        if etype == "CUSTOMER.DISPUTE.CREATED":
            capture_ids = [t.get("seller_transaction_id") for t in res.get("disputed_transactions", [])]
        else:
            capture_ids = [_capture_id_of(res)]
        rec = next((r for r in reversed(d["evidence"]) if r["type"] in ("payment_attempt", "payment_reconciled")
                    and (r.get("settlement") or {}).get("paypal_capture_id") in capture_ids), None)
        record = {"type": "paypal_event", "event_id": event.get("id"), "event_type": etype, "capture_id": next(iter(capture_ids), None),
                  "ref": (rec or {}).get("ref") or (rec or {}).get("id"), "summary": event.get("summary")}
        base = rec if (rec or {}).get("type") == "payment_attempt" else next(
            (r for r in d["evidence"] if rec and r["id"] == rec.get("ref")), None)  # a reconciled payment points at its attempt
        if rec and base and etype in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
            rec, original = base, base["request"]["amount"]
            cap_id = record["capture_id"]
            value = float((res.get("amount") or {}).get("value") or original)
            already = d["refunds"].get(cap_id, 0.0)
            restore = round(max(0.0, min(value, original - already)), 2)
            d["refunds"][cap_id] = round(already + restore, 2)
            c = next((c for c in d["codes"].values() if c["hint"] == rec["code"]["hint"]), None)
            if c and restore:
                c["spent"] = round(max(0.0, c["spent"] - restore), 2)
                if d["refunds"][cap_id] >= original - 1e-9:
                    c["uses"] = max(0, c["uses"] - 1)
            record.update(amount=value, budget_restored=restore)
            d["transactions"].append({"time": time.time(), "merchant": rec["request"]["merchant_name"], "amount": restore,
                                      "code_hint": rec["code"]["hint"], "result": "REFUNDED", "evidence_id": record["ref"],
                                      "rail": "paypal", "detail": f"{etype.split('.')[-1].lower()} in PayPal · £{restore:.2f} back on the code"})
        _append_evidence(d, record)
    return {"ok": True, "matched": bool(rec), **{k: record.get(k) for k in ("budget_restored", "ref")}}


# ---------- escrow actions (dispute / refund / release) ----------
ACTION_LABELS = {"dispute": "Payer disputed", "merchant_refund": "Merchant refunded", "release": "Released to merchant",
                 "resolve_refund": "Arbiter refunded payer", "resolve_release": "Arbiter paid merchant"}


def escrow_action(eid, action):
    with _tx() as d:
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
    return {"status": after, "evidence_id": ev["id"], "tx": txh, "tx_link": escrow.tx_link(txh)}
