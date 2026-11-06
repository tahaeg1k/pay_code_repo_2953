"""ScopePay demo server.  Run:  uvicorn app:app --reload   then open http://localhost:8000"""
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
            "merchants": pc.merchants(), "chain": pc.verify_evidence(), "escrow": escrow.info()}


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


@app.post("/api/codes")
def new_code(b: NewCode):
    return {"code": pc.issue_code(b.cap, b.minutes, b.label, b.multi_use, b.merchant_lock or None, b.issued_to)}


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
def redeem(b: Redeem, request: Request):
    client = {"ip": request.client.host if request.client else None, "user_agent": request.headers.get("user-agent")}
    return pc.redeem(b.code, b.amount, b.merchant_id, [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in b.basket],
                     b.channel, client, b.rail)


@app.get("/api/evidence")
def evidence_all():
    return {"chain": pc.verify_evidence(), "records": pc.evidence_list()}


@app.get("/api/evidence/verify")
def evidence_verify():
    return pc.verify_evidence()


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
