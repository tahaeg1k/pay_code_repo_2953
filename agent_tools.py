"""The tools an AI agent gets for shopping with a ScopePay code. Shared by agent.py (Claude) and mcp_server.py.

What an agent can do: read a code's limits, browse a shop, and ask to pay with the code.
What it cannot do: see or use the payer's PayPal, set prices, approve held payments, change limits.
Talks to a running ScopePay server over HTTP (SCOPEPAY_URL, default http://localhost:8001), like any outside agent.
"""
import json
import os

import requests

BASE = os.getenv("SCOPEPAY_URL", "http://localhost:8001").rstrip("/")


class ScopePayClient:
    def __init__(self, base=BASE, agent_name="ScopePay demo agent"):
        self.base, self.agent_name = base, agent_name

    def _req(self, method, path, body=None):
        try:
            r = requests.request(method, self.base + path, json=body, timeout=90)
        except requests.RequestException as e:
            return {"error": f"ScopePay server not reachable at {self.base} ({e}). Is it running?"}
        try:
            return r.json()
        except ValueError:
            return {"error": f"ScopePay {r.status_code}: {r.text[:200]}"}

    def check_code(self, code):
        return self._req("POST", "/api/codes/check", {"code": code})

    def list_products(self, merchant_id):
        return self._req("GET", f"/api/catalog/{merchant_id}")

    def pay_with_code(self, code, merchant_id, items):
        return self._req("POST", "/api/agent/checkout", {"code": code, "merchant_id": merchant_id, "items": items,
                                                          "agent": self.agent_name})


_CODE = {"code": {"type": "string", "description": "The ScopePay code the user gave you, e.g. SP-1A2B-3C4D-5E6F"}}
_ITEMS = {"type": "array", "description": "Products to buy", "items": {"type": "object", "properties": {
    "product_id": {"type": "string"}, "qty": {"type": "integer", "minimum": 1}}, "required": ["product_id", "qty"]}}
_MERCHANT = {"type": "string", "enum": ["greenleaf", "northside"], "description": "greenleaf = Greenleaf Grocer, northside = Northside Electronics"}

TOOL_SPECS = [
    ("check_code", "Read the limits on the ScopePay code: status, money remaining, expiry, which shop it is locked to, "
                   "and the owner's stated intent. Check this before shopping.", {}, []),
    ("list_products", "List the products and prices of a shop.", {"merchant_id": _MERCHANT}, ["merchant_id"]),
    ("pay_with_code", "Pay for a basket with the ScopePay code. The shop sets the prices. Returns approved, declined "
                      "(with the reason), or held (the owner must approve it in their wallet; do not retry).",
     {"merchant_id": _MERCHANT, "items": _ITEMS}, ["merchant_id", "items"]),
]


def schemas(with_code, skip=()):
    """Tool definitions. with_code=False keeps the code out of the model's hands (the runner injects it)."""
    out = []
    for name, desc, props, required in TOOL_SPECS:
        if name in skip:
            continue
        p, r = dict(props), list(required)
        if with_code:
            p = {**_CODE, **p}; r = ["code"] + r
        out.append({"name": name, "description": desc, "input_schema": {"type": "object", "properties": p, "required": r}})
    return out


def run(client, name, args, code=None):
    code = code or args.get("code", "")
    if name == "check_code":
        return client.check_code(code)
    if name == "list_products":
        return client.list_products(args["merchant_id"])
    if name == "pay_with_code":
        return client.pay_with_code(code, args["merchant_id"], args.get("items", []))
    return {"error": f"unknown tool {name}"}


def as_text(result):
    return json.dumps(result, ensure_ascii=False, indent=1)
