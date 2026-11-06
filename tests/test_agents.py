"""Agent, catalog and MCP server tests. Claude and the ScopePay server are mocked."""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent  # noqa: E402
import agent_tools  # noqa: E402
import ai  # noqa: E402
import catalog  # noqa: E402
import mcp_server  # noqa: E402

CODE = "SP-AAAA-BBBB-CCCC"


class FakeClient:
    def __init__(self):
        self.calls = []

    def check_code(self, code):
        self.calls.append(("check_code", code)); return {"status": "active", "remaining": 15.0, "intent": "vegetarian"}

    def list_products(self, merchant_id):
        self.calls.append(("list_products", merchant_id)); return {"products": catalog.products(merchant_id)}

    def pay_with_code(self, code, merchant_id, items):
        self.calls.append(("pay_with_code", code, merchant_id, items)); return {"approved": True, "evidence_id": "EV-1"}


def reply(blocks, stop="tool_use"):
    return {"content": blocks, "stop_reason": stop}


class Catalog(unittest.TestCase):
    def test_shop_sets_the_prices(self):
        basket = catalog.build_basket("greenleaf", [{"product_id": "broccoli", "qty": 2}])
        self.assertEqual(basket, [{"name": "Broccoli", "qty": 2, "price": 0.9}])

    def test_rejects_unknown_products_and_bad_quantities(self):
        with self.assertRaises(ValueError):
            catalog.build_basket("greenleaf", [{"product_id": "earbuds", "qty": 1}])
        with self.assertRaises(ValueError):
            catalog.build_basket("greenleaf", [{"product_id": "broccoli", "qty": 0}])

    def test_matches_store_page(self):
        html = (ROOT / "static" / "store.html").read_text()
        for m in catalog.CATALOG.values():
            for _, emoji, name, price, _cat in m:
                self.assertIn(f'"{name}",{price:g}', html, name)


class AgentLoop(unittest.TestCase):
    def test_code_is_injected_and_never_shown_to_the_model(self):
        script = [
            reply([{"type": "tool_use", "id": "t1", "name": "check_code", "input": {}}]),
            reply([{"type": "tool_use", "id": "t2", "name": "pay_with_code",
                    "input": {"merchant_id": "greenleaf", "items": [{"product_id": "broccoli", "qty": 1}]}}]),
            reply([{"type": "text", "text": "Bought broccoli for £0.90, evidence EV-1."}], stop="end_turn"),
        ]
        sent = []

        def fake_messages(**body):
            sent.append(json.dumps(body))
            return script.pop(0)

        client = FakeClient()
        with mock.patch.object(ai, "messages", side_effect=fake_messages):
            out = agent.run("buy veg", CODE, client=client, log=lambda *_: None)
        self.assertIn("EV-1", out["final"])
        self.assertEqual(client.calls[0], ("check_code", CODE))
        self.assertEqual(client.calls[1][1], CODE)
        self.assertTrue(all(CODE not in s for s in sent))          # the model never saw the code
        tools = json.loads(sent[0])["tools"]
        self.assertTrue(all("code" not in t["input_schema"]["properties"] for t in tools))  # no tool asks the model for it

    def test_no_precheck_hides_check_code(self):
        names = [t["name"] for t in agent_tools.schemas(with_code=False, skip=("check_code",))]
        self.assertEqual(names, ["list_products", "pay_with_code"])


class MCPServer(unittest.TestCase):
    def test_handshake_and_tools_over_stdio(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                               "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "nope"}]
        p = subprocess.run([sys.executable, str(ROOT / "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                           capture_output=True, text=True, timeout=30, cwd=str(ROOT))
        replies = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "scopepay")
        tools = {t["name"]: t for t in replies[1]["result"]["tools"]}
        self.assertEqual(set(tools), {"check_code", "list_products", "pay_with_code"})
        self.assertIn("code", tools["pay_with_code"]["inputSchema"]["required"])
        self.assertEqual(replies[2]["error"]["code"], -32601)

    def test_tool_call_passes_the_code_through(self):
        client = FakeClient()
        out = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "pay_with_code", "arguments": {
            "code": CODE, "merchant_id": "greenleaf", "items": [{"product_id": "broccoli", "qty": 1}]}}}, client)
        self.assertFalse(out["result"]["isError"])
        self.assertEqual(client.calls[0][1], CODE)


try:
    from fastapi.testclient import TestClient  # needs fastapi + httpx
    HAVE_APP = True
except Exception:  # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not installed")
class AppSmoke(unittest.TestCase):
    def test_catalog_and_webhook_refuses_unsigned(self):
        import app
        c = TestClient(app.app)
        self.assertEqual(c.get("/api/catalog/greenleaf").json()["products"][0]["product_id"], "apples-6")
        self.assertEqual(c.post("/webhooks/paypal", json={"id": "x"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
