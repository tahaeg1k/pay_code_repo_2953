"""ScopePay demo server.  Run:  uvicorn app:app --reload   then open http://localhost:8000"""
import json
from typing import List, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

import agent
import agent_tools
import ai
import catalog
import escrow
import paycodes as pc

app = FastAPI(title="ScopePay")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def no_cache(request, call_next):
    # demo app: always serve the latest pages and styles (avoids stale CSS after updates)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(Exception)
async def err(_, e):
    return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/")
@app.get("/demo")
@app.get("/blog")
@app.get("/blog/{slug}")
def site_page(slug: str = ""):
    return FileResponse("static/site.html")


# Alternative site design (static/site/), for comparison
@app.get("/preview")
def preview_home():
    return FileResponse("static/site/home.html")


@app.get("/preview/demo")
def preview_demo():
    return FileResponse("static/site/demo.html")


@app.get("/preview/blog")
def preview_blog():
    return FileResponse("static/site/blog.html")


@app.get("/wallet")
def wallet_page():
    return FileResponse("static/wallet.html")


@app.get("/store")
def store_page():
    return FileResponse("static/store.html")


@app.get("/evidence/{eid}")
def evidence_page(eid: str):
    return FileResponse("static/evidence.html")


@app.get("/api/state")
def state():
    return {"wallet": pc.wallet(), "codes": pc.list_codes(), "transactions": pc.transactions(),
            "merchants": pc.merchants(), "chain": pc.verify_evidence(), "escrow": escrow.info(),
            "held": pc.list_held(), "pending": pc.list_pending(), "ai": {"enabled": ai.available(), "model": ai.AUDIT_MODEL, "provider": ai.PROVIDER}}


@app.get("/api/merchants")
def merchants():
    return pc.merchants()


@app.post("/api/link")
def link(request: Request):
    base = str(request.base_url).rstrip("/")
    return {"approve_url": pc.start_link(f"{base}/vault/return", f"{base}/wallet?linked=cancelled")}


@app.get("/vault/return")
def vault_return(approval_token_id: Optional[str] = None):
    pc.finish_link(approval_token_id)
    return RedirectResponse("/wallet?linked=ok")


@app.post("/api/unlink")
def unlink():
    pc.unlink(); return {"ok": True}


class NewCode(BaseModel):
    cap: float
    minutes: int = 60
    label: str = ""
    multi_use: bool = False
    merchant_lock: Optional[str] = None
    issued_to: str = "user"
    intent: Optional[str] = None        # plain-English purpose, checked by the AI on every payment
    approve_over: Optional[float] = None  # payments above this wait for your approval


@app.post("/api/codes")
def new_code(b: NewCode):
    return {"code": pc.issue_code(b.cap, b.minutes, b.label, b.multi_use, b.merchant_lock or None, b.issued_to,
                                  b.intent, b.approve_over)}


class CodeOnly(BaseModel):
    code: str


@app.post("/api/codes/check")
def check_code(b: CodeOnly):
    """What an agent holding the code may see: limits and intent, never PayPal details."""
    return pc.code_info(b.code)


@app.post("/api/codes/{hint}/revoke")
def revoke(hint: str):
    pc.revoke(hint); return {"ok": True}


class Item(BaseModel):
    name: str
    qty: int
    price: float


class Redeem(BaseModel):
    code: str
    amount: float
    merchant_id: str = "greenleaf"
    basket: List[Item] = []
    channel: str = "human"
    rail: str = "paypal"  # "paypal" or "pyusd"


@app.post("/api/redeem")
def redeem(b: Redeem, request: Request, idempotency_key: Optional[str] = Header(None)):
    """Merchant Fast Pay. Send an Idempotency-Key header per checkout: a retry or double click then gets the first answer."""
    client = {"ip": request.client.host if request.client else None, "user_agent": request.headers.get("user-agent")}
    return pc.redeem(b.code, b.amount, b.merchant_id, [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in b.basket],
                     b.channel, client, b.rail, idempotency_key=idempotency_key)


@app.get("/api/evidence")
def evidence_all():
    return {"chain": pc.verify_evidence(), "records": pc.evidence_list()}


@app.get("/api/evidence/verify")
def evidence_verify():
    return pc.verify_evidence()


@app.get("/api/evidence/public-key")
def evidence_public_key():
    """ScopePay's Ed25519 public key: anyone holding an evidence record can check its signature with it."""
    return pc.evidence_public_key()


@app.get("/api/evidence/{eid}")
def evidence_one(eid: str):
    e = pc.get_evidence(eid)
    return e if e else JSONResponse({"error": "not found"}, status_code=404)


@app.get("/api/escrow")
def escrow_info():
    return escrow.info()


@app.post("/api/escrow/{eid}/{action}")
def escrow_action(eid: str, action: str):
    return pc.escrow_action(eid, action)


# ---------- AI agents ----------
@app.get("/api/catalog/{merchant_id}")
def shop_catalog(merchant_id: str):
    items = catalog.products(merchant_id)
    if not items:
        return JSONResponse({"error": "unknown shop"}, status_code=404)
    return {"merchant_id": merchant_id, "merchant": pc.merchants()[merchant_id]["name"], "currency": pc.CURRENCY, "products": items}


class AgentItem(BaseModel):
    product_id: str
    qty: int = 1


class AgentCheckout(BaseModel):
    code: str
    merchant_id: str
    items: List[AgentItem]
    agent: str = "AI agent"


@app.post("/api/agent/checkout")
def agent_checkout(b: AgentCheckout, idempotency_key: Optional[str] = Header(None)):
    """Merchant checkout for agents: the shop prices the basket, then the normal ScopePay rules apply."""
    basket = catalog.build_basket(b.merchant_id, [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in b.items])
    amount = round(sum(i["price"] * i["qty"] for i in basket), 2)
    out = pc.redeem(b.code, amount, b.merchant_id, basket, channel="agent", client={"agent": b.agent},
                    idempotency_key=idempotency_key)
    return {"amount": amount, "basket": basket, **out}


class AgentRun(BaseModel):
    task: str
    code: str
    precheck: bool = True


@app.post("/api/agent/run")
def agent_run(b: AgentRun, request: Request):
    """Run the Claude shopping agent from the wallet page. It calls this server over HTTP like any outside agent."""
    if not ai.available():
        return JSONResponse({"error": "Add OPENROUTER_API_KEY (or ANTHROPIC_API_KEY) to .env to run the agent."}, status_code=400)
    client = agent_tools.ScopePayClient(base=str(request.base_url).rstrip("/"), agent_name="Claude shopping agent")
    return agent.run(b.task, b.code, client=client, precheck=b.precheck, log=lambda *_: None)


# ---------- held payments, reconcile ----------
@app.get("/api/held")
def held():
    return pc.list_held()


class Approval(BaseModel):
    request_hash: str


@app.post("/api/held/{eid}/approve")
def approve(eid: str, b: Approval):
    return pc.approve_held(eid, b.request_hash)


@app.post("/api/held/{eid}/reject")
def reject(eid: str):
    return pc.reject_held(eid)


@app.post("/api/reconcile/{eid}")
def reconcile(eid: str):
    return pc.reconcile(eid)


# ---------- PayPal webhooks ----------
@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    """Signature-verified with PayPal before anything changes. Needs PAYPAL_WEBHOOK_ID in .env."""
    try:
        event = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not await run_in_threadpool(pc.verify_webhook, dict(request.headers), event):
        return JSONResponse({"error": "webhook signature not verified"}, status_code=401)
    return await run_in_threadpool(pc.handle_paypal_event, event)
