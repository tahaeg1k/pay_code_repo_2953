"""PYUSD escrow rail for PayCodes.

Two modes, same semantics as contracts/paycode-escrow/src/PayCodeEscrow.sol:
  * "sepolia"   : real transactions on Ethereum Sepolia via web3 (set SEPOLIA_RPC_URL, ESCROW_ADDRESS, PAYER_KEY, ...)
  * "simulated" : a local ledger that mirrors the contract exactly (default; lets the demo run with no chain setup)
Testnet keys only. Never put a key that holds real funds in .env.
"""
import hashlib
import os
import time

from dotenv import load_dotenv

load_dotenv()

STATUS = ["None", "Escrowed", "Disputed", "Released", "Refunded"]
PYUSD_DEFAULT = "0xCaC524BcA292aaade2DF8A05cC58F0a65B1B3bB9"  # PYUSD on Ethereum Sepolia (Paxos docs)
EXPLORER = "https://sepolia.etherscan.io/tx/"
SIM_WINDOW = int(os.getenv("ESCROW_WINDOW_SECONDS", "120"))  # simulated refund window (short, for demos)

RPC = os.getenv("SEPOLIA_RPC_URL", "")
ESCROW_ADDRESS = os.getenv("ESCROW_ADDRESS", "")
PYUSD_ADDRESS = os.getenv("PYUSD_ADDRESS", PYUSD_DEFAULT)
PAYER_KEY = os.getenv("PAYER_KEY", "")          # wallet that pays (e.g. the AI agent's wallet)
ARBITER_KEY = os.getenv("ARBITER_KEY", "")      # PayCodes acting as arbiter
MERCHANT_WALLETS = {"greenleaf": os.getenv("GREENLEAF_WALLET", ""), "northside": os.getenv("NORTHSIDE_WALLET", "")}
MERCHANT_KEYS = {"greenleaf": os.getenv("GREENLEAF_WALLET_KEY", ""), "northside": os.getenv("NORTHSIDE_WALLET_KEY", "")}

MODE = "sepolia" if (RPC and ESCROW_ADDRESS and PAYER_KEY) else "simulated"

ESCROW_ABI = [
    {"type": "function", "name": "pay", "stateMutability": "nonpayable",
     "inputs": [{"name": "merchant", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "evidenceHash", "type": "bytes32"}],
     "outputs": [{"name": "id", "type": "uint256"}]},
    {"type": "function", "name": "dispute", "stateMutability": "nonpayable",
     "inputs": [{"name": "id", "type": "uint256"}, {"name": "reasonHash", "type": "bytes32"}], "outputs": []},
    {"type": "function", "name": "refundByMerchant", "stateMutability": "nonpayable", "inputs": [{"name": "id", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "release", "stateMutability": "nonpayable", "inputs": [{"name": "id", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "resolve", "stateMutability": "nonpayable",
     "inputs": [{"name": "id", "type": "uint256"}, {"name": "refundPayer", "type": "bool"}], "outputs": []},
    {"type": "function", "name": "getPayment", "stateMutability": "view", "inputs": [{"name": "id", "type": "uint256"}],
     "outputs": [{"name": "", "type": "tuple", "components": [
         {"name": "payer", "type": "address"}, {"name": "merchant", "type": "address"}, {"name": "amount", "type": "uint96"},
         {"name": "releaseAt", "type": "uint40"}, {"name": "status", "type": "uint8"}, {"name": "evidenceHash", "type": "bytes32"}]}]},
    {"type": "event", "name": "PaymentEscrowed", "anonymous": False, "inputs": [
        {"name": "id", "type": "uint256", "indexed": True}, {"name": "payer", "type": "address", "indexed": True},
        {"name": "merchant", "type": "address", "indexed": True}, {"name": "amount", "type": "uint256", "indexed": False},
        {"name": "releaseAt", "type": "uint256", "indexed": False}, {"name": "evidenceHash", "type": "bytes32", "indexed": False}]},
]
ERC20_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]


class EscrowError(Exception):
    pass


# ----------------------------------------------------------------- helpers
def evidence_hash(evidence_id):
    """bytes32 reference to the off-chain evidence record (keccak on-chain, sha256 in simulation)."""
    if MODE == "sepolia":
        from web3 import Web3
        return Web3.keccak(text=evidence_id).hex()
    return "0x" + hashlib.sha256(evidence_id.encode()).hexdigest()


def info():
    out = {"mode": MODE, "token": "PYUSD", "window_seconds": SIM_WINDOW if MODE == "simulated" else None,
           "merchants": {k: (v or None) for k, v in MERCHANT_WALLETS.items()}}
    if MODE == "sepolia":
        try:
            w3, esc, tok = _chain()
            payer = w3.eth.account.from_key(PAYER_KEY).address
            dec = tok.functions.decimals().call()
            out.update(escrow=ESCROW_ADDRESS, payer=payer, payer_balance=tok.functions.balanceOf(payer).call() / 10 ** dec,
                       explorer="https://sepolia.etherscan.io/address/" + ESCROW_ADDRESS)
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
    return out


# ------------------------------------------------------------- simulation
def _sim_status(p):
    return p["status"]


def _sim_pay(db, merchant_id, amount, ev_hash):
    pid = len(db.setdefault("escrow", []))
    p = {"id": pid, "payer": "sim:your-pyusd-wallet", "merchant": f"sim:{merchant_id}-wallet", "amount": amount,
         "releaseAt": time.time() + SIM_WINDOW, "status": "Escrowed", "evidenceHash": ev_hash}
    db["escrow"].append(p)
    return pid, f"sim-tx-{pid}-pay"


def _sim_action(db, pid, action, merchant_id=None):
    p = db["escrow"][pid]
    now = time.time()
    st = p["status"]
    if action == "dispute":
        if st != "Escrowed": raise EscrowError(f"WrongStatus({st})")
        if now >= p["releaseAt"]: raise EscrowError("WindowClosed: refund window has passed")
        p["status"] = "Disputed"
    elif action == "merchant_refund":
        if st not in ("Escrowed", "Disputed"): raise EscrowError(f"WrongStatus({st})")
        p["status"] = "Refunded"
    elif action == "release":
        if st != "Escrowed": raise EscrowError(f"WrongStatus({st})")
        if now < p["releaseAt"]: raise EscrowError(f"StillInRefundWindow: {int(p['releaseAt'] - now)}s left")
        p["status"] = "Released"
    elif action in ("resolve_refund", "resolve_release"):
        if st != "Disputed": raise EscrowError(f"WrongStatus({st}): only disputed payments can be resolved")
        p["status"] = "Refunded" if action == "resolve_refund" else "Released"
    else:
        raise EscrowError("unknown action")
    return f"sim-tx-{pid}-{action}"


# ------------------------------------------------------------------ chain
_cache = {}


def _chain():
    if "w3" not in _cache:
        try:
            from web3 import Web3
        except ImportError as e:
            raise EscrowError("pip install web3  (needed for Sepolia mode)") from e
        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 60}))
        _cache.update(w3=w3, esc=w3.eth.contract(address=Web3.to_checksum_address(ESCROW_ADDRESS), abi=ESCROW_ABI),
                      tok=w3.eth.contract(address=Web3.to_checksum_address(PYUSD_ADDRESS), abi=ERC20_ABI))
    return _cache["w3"], _cache["esc"], _cache["tok"]


def _send(fn, key):
    w3, _, _ = _chain()
    acct = w3.eth.account.from_key(key)
    tx = fn.build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": w3.eth.chain_id})
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    h = w3.eth.send_raw_transaction(raw)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=180)
    if rcpt.status != 1:
        raise EscrowError(f"transaction reverted: {h.hex()}")
    return h.hex() if h.hex().startswith("0x") else "0x" + h.hex(), rcpt


def _chain_pay(merchant_id, amount, ev_hash):
    w3, esc, tok = _chain()
    to = MERCHANT_WALLETS.get(merchant_id)
    if not to:
        raise EscrowError(f"No wallet set for {merchant_id} (env {merchant_id.upper()}_WALLET)")
    from web3 import Web3
    dec = tok.functions.decimals().call()
    units = int(round(amount * 10 ** dec))
    payer = w3.eth.account.from_key(PAYER_KEY).address
    if tok.functions.balanceOf(payer).call() < units:
        raise EscrowError("Payer wallet has insufficient PYUSD (get test PYUSD at faucet.paxos.com)")
    if tok.functions.allowance(payer, esc.address).call() < units:
        _send(tok.functions.approve(esc.address, 2 ** 256 - 1), PAYER_KEY)
    txh, rcpt = _send(esc.functions.pay(Web3.to_checksum_address(to), units, bytes.fromhex(ev_hash[2:])), PAYER_KEY)
    pid = esc.events.PaymentEscrowed().process_receipt(rcpt)[0]["args"]["id"]
    return int(pid), txh


def _chain_action(pid, action, merchant_id):
    _, esc, _ = _chain()
    if action == "dispute":
        return _send(esc.functions.dispute(pid, b"\x00" * 32), PAYER_KEY)[0]
    if action == "release":
        return _send(esc.functions.release(pid), PAYER_KEY)[0]
    if action == "merchant_refund":
        key = MERCHANT_KEYS.get(merchant_id)
        if not key:
            raise EscrowError(f"Merchant refund needs {merchant_id.upper()}_WALLET_KEY in .env")
        return _send(esc.functions.refundByMerchant(pid), key)[0]
    if action in ("resolve_refund", "resolve_release"):
        if not ARBITER_KEY:
            raise EscrowError("Arbiter actions need ARBITER_KEY in .env")
        return _send(esc.functions.resolve(pid, action == "resolve_refund"), ARBITER_KEY)[0]
    raise EscrowError("unknown action")


def _chain_status(pid):
    w3, esc, tok = _chain()
    p = esc.functions.getPayment(pid).call()
    dec = tok.functions.decimals().call()
    return {"id": pid, "payer": p[0], "merchant": p[1], "amount": p[2] / 10 ** dec, "releaseAt": p[3],
            "status": STATUS[p[4]], "evidenceHash": "0x" + p[5].hex().removeprefix("0x")}


# ------------------------------------------------------------- public API
def pay(db, merchant_id, amount, ev_hash):
    """Returns (payment_id, tx_hash)."""
    return _chain_pay(merchant_id, amount, ev_hash) if MODE == "sepolia" else _sim_pay(db, merchant_id, amount, ev_hash)


def act(db, pid, action, merchant_id):
    return _chain_action(pid, action, merchant_id) if MODE == "sepolia" else _sim_action(db, pid, action, merchant_id)


def status(db, pid):
    p = _chain_status(pid) if MODE == "sepolia" else dict(db["escrow"][pid])
    now = time.time()
    p["seconds_to_release"] = max(0, int(p["releaseAt"] - now))
    p["mode"] = MODE
    allowed = []
    if p["status"] == "Escrowed":
        allowed += ["dispute", "merchant_refund"] if now < p["releaseAt"] else ["release", "merchant_refund"]
    elif p["status"] == "Disputed":
        allowed += ["resolve_refund", "resolve_release", "merchant_refund"]
    p["allowed_actions"] = allowed
    return p


def tx_link(txh):
    return EXPLORER + txh if txh and txh.startswith("0x") else None
