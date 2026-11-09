"""Hosted-demo pieces: health check, demo wallet restore, Zapier automation endpoint, OpenAPI spec, Render config."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import paycodes as pc  # noqa: E402

try:
    from fastapi.testclient import TestClient  # needs fastapi + httpx
    import app as app_module
    HAVE_APP = True
except Exception:  # noqa: BLE001
    HAVE_APP = False


class TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = mock.patch.object(pc, "DB", Path(self.tmp.name) / "db.json")
        self.db.start()

    def tearDown(self):
        self.db.stop()
        self.tmp.cleanup()


class DemoWallet(TempStore):
    def test_restores_once_and_records_it(self):
        with mock.patch.dict(os.environ, {"DEMO_VAULT_ID": "VAULT-DEMO", "DEMO_PAYER_EMAIL": "buyer@example.com"}):
            pc.seed_demo_wallet()
            pc.seed_demo_wallet()  # a second start must not add another record
        self.assertEqual(pc.wallet()["vault_id"], "VAULT-DEMO")
        linked = [e for e in pc._db()["evidence"] if e["type"] == "wallet_linked"]
        self.assertEqual(len(linked), 1)
        self.assertIn("DEMO_VAULT_ID", linked[0]["consent"])
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_no_env_no_change(self):
        with mock.patch.dict(os.environ, {"DEMO_VAULT_ID": ""}):
            self.assertIsNone(pc.seed_demo_wallet())
        self.assertEqual(pc.wallet(), {})


class RenderConfig(unittest.TestCase):
    def test_blueprint_points_at_real_things(self):
        y = (ROOT / "render.yaml").read_text()
        self.assertIn("healthCheckPath: /healthz", y)
        self.assertIn("--proxy-headers", y)
        self.assertIn("uvicorn app:app", y)
        self.assertTrue((ROOT / ".github" / "workflows" / "ci.yml").exists())


@unittest.skipUnless(HAVE_APP, "fastapi/httpx not installed")
class Endpoints(TempStore):
    def setUp(self):
        super().setUp()
        with pc._tx() as d:
            d["wallet"] = {"vault_id": "VAULT1", "email": "b@x"}
        self.agent_code = pc.issue_code(20, 60, issued_to="ai_agent")
        self.person_code = pc.issue_code(20, 60)
        self.c = TestClient(app_module.app)
        self.env = mock.patch.dict(os.environ, {"AUTOMATION_TOKEN": "secret-token"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def post(self, body, token="secret-token"):
        h = {"X-ScopePay-Token": token} if token else {}
        return self.c.post("/api/automations/agent-run", json=body, headers=h)

    def test_health_and_dashboard(self):
        self.assertTrue(self.c.get("/healthz").json()["ok"])
        self.assertEqual(self.c.get("/dashboard").status_code, 200)

    def test_openapi_has_only_the_api(self):
        spec = self.c.get("/openapi.json").json()
        self.assertEqual(spec["info"]["title"], "ScopePay API")
        self.assertIn("/api/agent/checkout", spec["paths"])
        self.assertIn("/api/automations/agent-run", spec["paths"])
        self.assertNotIn("/wallet", spec["paths"])
        self.assertNotIn("/vault/return", spec["paths"])

    def test_automation_needs_the_token(self):
        self.assertEqual(self.post({"task": "x", "code": self.agent_code}, token=None).status_code, 401)
        self.assertEqual(self.post({"task": "x", "code": self.agent_code}, token="wrong").status_code, 401)
        with mock.patch.dict(os.environ, {"AUTOMATION_TOKEN": ""}):
            self.assertEqual(self.post({"task": "x", "code": self.agent_code}).status_code, 503)

    def test_automation_only_spends_codes_issued_to_an_agent(self):
        with mock.patch.object(app_module.ai, "available", return_value=True):
            self.assertEqual(self.post({"task": "x", "code": self.person_code}).status_code, 403)
            self.assertEqual(self.post({"task": "x", "code": "SP-0000-0000-0000"}).status_code, 404)

    def test_automation_runs_the_agent_and_records_the_trigger(self):
        fake = {"final": "Bought broccoli.", "steps": [{"kind": "tool", "name": "pay_with_code", "input": {},
                                                        "output": {"approved": True, "amount": 0.9, "evidence_id": "EV-X"}}]}
        with mock.patch.object(app_module.ai, "available", return_value=True), \
             mock.patch.object(app_module.agent, "run", return_value=fake) as run:
            r = self.post({"task": "buy broccoli", "code": self.agent_code, "source": "zapier", "wait": True})
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual((out["status"], out["outcome"], out["evidence_id"]), ("done", "approved", "EV-X"))
        self.assertEqual(run.call_args.args[1], self.agent_code)
        ev = pc._db()["evidence"]
        trig = next(e for e in ev if e["type"] == "automation_triggered")
        self.assertEqual(trig["source"], "zapier")
        self.assertNotIn(self.agent_code, str(trig))  # the full code is never written to the trail
        status = self.c.get(f"/api/automations/runs/{out['run_id']}", headers={"X-ScopePay-Token": "secret-token"}).json()
        self.assertEqual(status["status"], "done")

    def test_background_run_returns_at_once(self):
        def slow(*a, **k):
            time.sleep(0.2)
            return {"final": "done", "steps": []}
        with mock.patch.object(app_module.ai, "available", return_value=True), \
             mock.patch.object(app_module.agent, "run", side_effect=slow):
            out = self.post({"task": "x", "code": self.agent_code}).json()
            self.assertEqual(out["status"], "running")
            time.sleep(0.5)
        status = self.c.get(f"/api/automations/runs/{out['run_id']}", headers={"X-ScopePay-Token": "secret-token"}).json()
        self.assertEqual((status["status"], status["outcome"]), ("done", "no_payment"))


if __name__ == "__main__":
    unittest.main()
