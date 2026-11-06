"""Vault spike: save the buyer's PayPal once, then charge it with NO popup.

Steps (run in order):
  python vault_spike.py setup              # creates a setup token, prints an approval link
  (open the link, log in as the sandbox PERSONAL buyer, approve)
  python vault_spike.py confirm            # turns the approved setup token into a vault_id
  python vault_spike.py charge 12.50       # charges the saved PayPal, no approval needed

Mini PayCode demo on top of the vault:
  python vault_spike.py issue --cap 20 --minutes 60     # prints a one-time code
  python vault_spike.py pay <CODE> 15.00                # merchant redeems the code
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api-m.sandbox.paypal.com"
STATE = Path(__file__).with_name(".vault_state.json")
CURRENCY = "GBP"


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(s):
    STATE.write_text(json.dumps(s, indent=2))


def token():
    r = requests.post(f"{BASE}/v1/oauth2/token",
                      auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
                      data={"grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def call(method, path, body=None):
    r = requests.request(method, f"{BASE}{path}", json=body, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
        "PayPal-Request-Id": str(uuid.uuid4()),
    })
    if not r.ok:
        print(f"PayPal error {r.status_code}:\n{json.dumps(r.json(), indent=2)}")
        sys.exit(1)
    return r.json()


def cmd_setup(_):
    res = call("POST", "/v3/vault/setup-tokens", {"payment_source": {"paypal": {
        "description": "PayCodes wallet: save PayPal for future payments",
        "usage_type": "MERCHANT",
        "customer_type": "CONSUMER",
        "permit_multiple_payment_tokens": False,
        "experience_context": {
            "brand_name": "PayCodes",
            "shipping_preference": "NO_SHIPPING",
            "return_url": "https://example.com/approved",
            "cancel_url": "https://example.com/cancelled",
        },
    }}})
    s = state(); s["setup_token"] = res["id"]; save(s)
    approve = next(l["href"] for l in res["links"] if l["rel"] == "approve")
    print("Setup token:", res["id"], "| status:", res["status"])
    print("\nOpen this and approve with the sandbox PERSONAL account:\n" + approve)
    print("\n(You'll land on example.com afterwards - that's expected.) Then run: python vault_spike.py confirm")


def cmd_confirm(_):
    s = state()
    if "setup_token" not in s:
        sys.exit("Run `setup` first.")
    res = call("POST", "/v3/vault/payment-tokens",
               {"payment_source": {"token": {"id": s["setup_token"], "type": "SETUP_TOKEN"}}})
    s["vault_id"] = res["id"]; s["customer_id"] = res.get("customer", {}).get("id"); save(s)
    email = res.get("payment_source", {}).get("paypal", {}).get("email_address", "?")
    print(f"Saved! vault_id={res['id']} (buyer: {email})")
    print("Now try: python vault_spike.py charge 12.50")


def charge(amount):
    s = state()
    if "vault_id" not in s:
        sys.exit("No saved PayPal yet. Run `setup` then `confirm`.")
    order = call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"amount": {"currency_code": CURRENCY, "value": f"{amount:.2f}"}}],
        "payment_source": {"paypal": {"vault_id": s["vault_id"]}},
    })
    if order["status"] != "COMPLETED":  # some accounts need an explicit capture
        order = call("POST", f"/v2/checkout/orders/{order['id']}/capture")
    cap = order["purchase_units"][0]["payments"]["captures"][0]
    return order["id"], cap["id"], cap["status"]


def cmd_charge(a):
    oid, cid, st = charge(a.amount)
    print(f"Charged £{a.amount:.2f} with no popup -> order {oid}, capture {cid}, status {st}")


def h(code):
    return hashlib.sha256(code.encode()).hexdigest()


def cmd_issue(a):
    s = state()
    code = "PC-" + secrets.token_hex(4).upper()
    s.setdefault("codes", {})[h(code)] = {"cap": a.cap, "expires": time.time() + a.minutes * 60, "used": False}
    save(s)
    print(f"PayCode: {code}  (cap £{a.cap:.2f}, expires in {a.minutes} min, single use)")


def cmd_pay(a):
    s = state()
    c = s.get("codes", {}).get(h(a.code))
    if not c:
        sys.exit("DECLINED: unknown code")
    if c["used"]:
        sys.exit("DECLINED: code already used")
    if time.time() > c["expires"]:
        sys.exit("DECLINED: code expired")
    if a.amount > c["cap"]:
        sys.exit(f"DECLINED: £{a.amount:.2f} is over the £{c['cap']:.2f} cap")
    oid, cid, st = charge(a.amount)
    c["used"] = True; save(s)
    print(f"APPROVED: £{a.amount:.2f} paid via code -> order {oid}, capture {cid}, status {st}")


p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
sub = p.add_subparsers(dest="cmd", required=True)
sub.add_parser("setup").set_defaults(f=cmd_setup)
sub.add_parser("confirm").set_defaults(f=cmd_confirm)
c = sub.add_parser("charge"); c.add_argument("amount", type=float); c.set_defaults(f=cmd_charge)
i = sub.add_parser("issue"); i.add_argument("--cap", type=float, required=True)
i.add_argument("--minutes", type=int, default=60); i.set_defaults(f=cmd_issue)
y = sub.add_parser("pay"); y.add_argument("code"); y.add_argument("amount", type=float); y.set_defaults(f=cmd_pay)
a = p.parse_args(); a.f(a)
