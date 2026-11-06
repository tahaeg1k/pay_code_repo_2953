"""ScopePay rules engine and money-safety tests. PayPal and Claude are mocked; nothing leaves the machine.
Run:  python -m pytest -q      (or: python -m unittest discover -s tests)"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai  # noqa: E402
import paycodes as pc  # noqa: E402


class FakePayPal:
    """Behaves like PayPal for orders: a repeated PayPal-Request-Id returns the original result."""

    def __init__(self, delay=0.0, fail_first=None):
        self.delay, self.fail_first = delay, fail_first
        self.calls, self.by_request_id, self.orders = [], {}, 0
        self.lock = threading.Lock()

    def __call__(self, method, path, body=None, request_id=None):
        with self.lock:
            self.calls.append((method, path, request_id))
            if request_id and request_id in self.by_request_id:
                return self.by_request_id[request_id]
            if self.fail_first and not any(rid == "failed" for *_, rid in self.calls[:-1]):
                self.calls.append(("-", "-", "failed"))
                exc = self.fail_first
                self.fail_first = None
                # PayPal DID create the order before the connection dropped: remember it under the id
                self.orders += 1
                self.by_request_id[request_id] = self._order(body)
                raise exc
        time.sleep(self.delay)
        with self.lock:
            if path == "/v2/checkout/orders":
                self.orders += 1
                res = self._order(body)
            else:
                res = {}
            if request_id:
                self.by_request_id[request_id] = res
            return res

    def _order(self, body):
        n = self.orders
        return {"id": f"ORDER{n}", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [
            {"id": f"CAP{n}", "status": "COMPLETED", "create_time": "now"}]}}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(pc, "DB", Path(self.tmp.name) / "db.json"),
                        mock.patch.dict(pc.MERCHANTS["greenleaf"], {"payee_email": ""}),
                        mock.patch.dict(pc.MERCHANTS["northside"], {"payee_email": ""}),
                        # these tests use synthetic baskets and repeat payments on purpose; both guards are
                        # covered in tests/test_attacks.py
                        mock.patch.object(pc, "CATALOG_CHECK", False), mock.patch.object(pc, "DUPLICATE_WINDOW", 0)]
        for p in self.patches:
            p.start()
        with pc._tx() as d:
            d["wallet"] = {"vault_id": "VAULT123", "email": "buyer@example.com"}
        self.paypal = FakePayPal()
        self.call_patch = mock.patch.object(pc, "_call", self.paypal)
        self.call_patch.start()

    def tearDown(self):
        self.call_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def basket(self, total):
        return [{"name": "Groceries", "qty": 1, "price": total}]

    def pay(self, code, amount, merchant="greenleaf", **kw):
        return pc.redeem(code, amount, merchant, kw.pop("basket", self.basket(amount)), **kw)

    def code_row(self, code):
        return pc._db()["codes"][pc._h(code)]


class DeclineReasons(Base):
    def test_each_rule_declines_with_a_reason(self):
        code = pc.issue_code(10, 60)
        self.assertIn("within_cap", self.pay(code, 12)["reason"])
        self.assertIn("merchant_known", self.pay(code, 5, merchant="nowhere")["reason"])
        self.assertIn("code_exists", self.pay("SP-0000-0000-0000", 5)["reason"])
        self.assertIn("amount_positive", self.pay(code, 0, basket=[])["reason"])
        self.assertIn("basket_matches", pc.redeem(code, 9, "greenleaf", self.basket(2))["reason"])
        locked = pc.issue_code(10, 60, merchant_lock="greenleaf")
        self.assertIn("merchant_lock", self.pay(locked, 5, merchant="northside")["reason"])
        revoked = pc.issue_code(10, 60)
        pc.revoke(revoked[-4:])
        self.assertIn("revoked", self.pay(revoked, 5)["reason"])
        expired = pc.issue_code(10, 60)
        with pc._tx() as d:
            d["codes"][pc._h(expired)]["expires"] = time.time() - 1
        self.assertIn("expired", self.pay(expired, 5)["reason"])
        single = pc.issue_code(10, 60)
        self.assertTrue(self.pay(single, 3)["approved"])
        self.assertIn("used", self.pay(single, 3)["reason"])
        self.assertEqual(self.paypal.orders, 1)  # only the one good payment reached PayPal

    def test_no_wallet(self):
        code = pc.issue_code(10, 60)
        with pc._tx() as d:
            d["wallet"] = {}
        self.assertIn("wallet_linked", self.pay(code, 5)["reason"])


class MoneySafety(Base):
    def test_approved_payment_debits_and_chain_holds(self):
        code = pc.issue_code(20, 60, multi_use=True)
        r = self.pay(code, 7.5)
        self.assertTrue(r["approved"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"], c["in_flight"]), (7.5, 0.0, 0))
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_parallel_payments_never_exceed_cap(self):
        """Limits read before a slow call and written after it must not let parallel payments overspend."""
        self.paypal.delay = 0.05
        code = pc.issue_code(10, 60, multi_use=True)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.pay(code, 4))) for _ in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(r["approved"] for r in results), 2)
        self.assertEqual(self.paypal.orders, 2)
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"]), (8.0, 0.0))
        self.assertEqual(len(pc._db()["transactions"]), 12)  # no lost writes
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_single_use_cannot_be_double_spent_in_parallel(self):
        self.paypal.delay = 0.05
        code = pc.issue_code(10, 60)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.pay(code, 2))) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(r["approved"] for r in results), 1)

    def test_request_ids_come_from_the_evidence_id(self):
        r = self.pay(pc.issue_code(10, 60), 5)
        rids = [rid for _, path, rid in self.paypal.calls if path == "/v2/checkout/orders"]
        self.assertEqual(rids, [f"{r['evidence_id']}-order"])

    def test_timeout_is_uncertain_and_reconcile_never_charges_twice(self):
        self.paypal.fail_first = pc.PayPalUncertain("read timeout")
        code = pc.issue_code(10, 60, multi_use=True)
        r = self.pay(code, 6)
        self.assertTrue(r.get("uncertain"))
        self.assertFalse(r["approved"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"]), (0.0, 6.0))       # budget stays reserved
        self.assertIn("within_cap", self.pay(code, 5)["reason"])           # so nothing can spend it twice
        done = pc.reconcile(r["evidence_id"])
        self.assertTrue(done["approved"])
        self.assertEqual(self.paypal.orders, 1)                            # replay returned the original order
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"]), (6.0, 0.0))
        self.assertEqual(pc.list_pending(), [])
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_definite_paypal_error_releases_the_reservation(self):
        code = pc.issue_code(10, 60, multi_use=True)
        with mock.patch.object(pc, "_call", side_effect=pc.PayPalError("PayPal 422: INSTRUMENT_DECLINED")):
            r = self.pay(code, 6)
        self.assertFalse(r["approved"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"], c["in_flight"]), (0.0, 0.0, 0))


def verdict(v, fits="yes"):
    return {"verdict": v, "summary": f"model says {v}", "items": [{"name": "x", "fits": fits}], "model": "test", "prompt_sha256": "h"}


class IntentAndApproval(Base):
    def test_intent_allow_pays(self):
        code = pc.issue_code(30, 60, intent="vegetarian groceries")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("allow")):
            r = self.pay(code, 10)
        self.assertTrue(r["approved"])

    def test_intent_decline_blocks_and_releases(self):
        code = pc.issue_code(30, 60, intent="vegetarian groceries, no alcohol")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("decline", "no")):
            r = self.pay(code, 10)
        self.assertFalse(r["approved"])
        self.assertIn("intent_ai", r["reason"])
        self.assertEqual(self.paypal.orders, 0)
        self.assertEqual(self.code_row(code)["reserved"], 0.0)

    def test_ai_never_overrides_rules(self):
        code = pc.issue_code(5, 60, intent="anything")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("allow")) as audit:
            r = self.pay(code, 50)
        self.assertIn("within_cap", r["reason"])
        audit.assert_not_called()  # the AI only sees payments the rules already allow

    def test_hold_then_approve_with_matching_hash(self):
        code = pc.issue_code(30, 60, intent="groceries")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("hold", "unsure")):
            r = self.pay(code, 12)
        self.assertTrue(r["held"])
        self.assertEqual(self.paypal.orders, 0)
        self.assertEqual(self.code_row(code)["reserved"], 12.0)
        held = pc.list_held()
        self.assertEqual([h["evidence_id"] for h in held], [r["evidence_id"]])
        done = pc.approve_held(r["evidence_id"], held[0]["request_hash"])
        self.assertTrue(done["approved"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"]), (12.0, 0.0))
        self.assertEqual(pc.list_held(), [])
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_approval_with_wrong_hash_is_refused(self):
        code = pc.issue_code(30, 60, intent="groceries")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("hold", "unsure")):
            r = self.pay(code, 12)
        with self.assertRaises(ValueError):
            pc.approve_held(r["evidence_id"], "0" * 64)
        self.assertEqual(self.paypal.orders, 0)
        types = [e["type"] for e in pc._db()["evidence"]]
        self.assertIn("approval_refused", types)

    def test_tampered_held_request_is_refused(self):
        code = pc.issue_code(30, 60, intent="groceries")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("hold", "unsure")):
            r = self.pay(code, 12)
        good_hash = pc.list_held()[0]["request_hash"]
        with pc._tx() as d:  # something swaps the merchant after the human saw the request
            d["held"][r["evidence_id"]]["request"]["merchant_id"] = "northside"
        with self.assertRaises(ValueError):
            pc.approve_held(r["evidence_id"], good_hash)
        self.assertEqual(self.paypal.orders, 0)

    def test_reject_releases_budget(self):
        code = pc.issue_code(30, 60, intent="groceries")
        with mock.patch.object(ai, "audit_basket", return_value=verdict("hold", "unsure")):
            r = self.pay(code, 12)
        pc.reject_held(r["evidence_id"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["reserved"]), (0.0, 0.0))

    def test_approval_threshold_holds_without_ai(self):
        code = pc.issue_code(30, 60, approve_over=10, multi_use=True)
        self.assertTrue(self.pay(code, 8)["approved"])
        r = self.pay(code, 15)
        self.assertTrue(r["held"])
        self.assertIn("approval threshold", r["reason"])

    def test_expired_before_approval_is_declined(self):
        code = pc.issue_code(30, 60, approve_over=1)
        r = self.pay(code, 5)
        with pc._tx() as d:
            d["codes"][pc._h(code)]["expires"] = time.time() - 1
        out = pc.approve_held(r["evidence_id"], pc.list_held()[0]["request_hash"])
        self.assertFalse(out["approved"])
        self.assertEqual(self.paypal.orders, 0)
        self.assertEqual(self.code_row(code)["reserved"], 0.0)


class AuditorBehaviour(unittest.TestCase):
    def tool_reply(self, verdict, items):
        return {"model": "claude-test", "content": [{"type": "tool_use", "name": "record_verdict",
                                                     "input": {"verdict": verdict, "summary": "s", "items": items}}]}

    def test_parses_forced_tool_call(self):
        with mock.patch.object(ai, "messages", return_value=self.tool_reply("allow", [{"name": "Apples", "fits": "yes"}])) as m:
            out = ai.audit_basket("fruit", [{"name": "Apples", "qty": 1, "price": 2.4}], 2.4, "Greenleaf")
        self.assertEqual(out["verdict"], "allow")
        body = m.call_args.kwargs
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "record_verdict"})
        self.assertIn("untrusted", body["system"])

    def test_allow_with_doubtful_item_becomes_hold(self):
        with mock.patch.object(ai, "messages", return_value=self.tool_reply("allow", [{"name": "Wine", "fits": "unsure"}])):
            out = ai.audit_basket("groceries", [{"name": "Wine", "qty": 1, "price": 9}], 9, "Greenleaf")
        self.assertEqual(out["verdict"], "hold")

    def test_any_failure_is_a_hold(self):
        with mock.patch.object(ai, "messages", side_effect=ai.AIError("no key")):
            out = ai.audit_basket("groceries", [{"name": "Bread", "qty": 1, "price": 3}], 3, "Greenleaf")
        self.assertEqual(out["verdict"], "hold")
        with mock.patch.object(ai, "messages", return_value={"content": [{"type": "text", "text": "allow!"}]}):
            out = ai.audit_basket("groceries", [{"name": "Bread", "qty": 1, "price": 3}], 3, "Greenleaf")
        self.assertEqual(out["verdict"], "hold")


class Webhooks(Base):
    def refund_event(self, eid, cap, value):
        return {"id": eid, "event_type": "PAYMENT.CAPTURE.REFUNDED", "summary": "refund",
                "resource": {"id": "REFUND1", "amount": {"value": str(value), "currency_code": "GBP"},
                             "links": [{"rel": "up", "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{cap}"}]}}

    def test_refund_restores_budget_once(self):
        code = pc.issue_code(20, 60)
        r = self.pay(code, 8)
        cap = pc._db()["evidence"][-1]["settlement"]["paypal_capture_id"]
        out = pc.handle_paypal_event(self.refund_event("WH-1", cap, 8))
        self.assertEqual(out["budget_restored"], 8.0)
        self.assertEqual(out["ref"], r["evidence_id"])
        c = self.code_row(code)
        self.assertEqual((c["spent"], c["uses"]), (0.0, 0))
        self.assertTrue(pc.handle_paypal_event(self.refund_event("WH-1", cap, 8))["duplicate"])
        out = pc.handle_paypal_event(self.refund_event("WH-2", cap, 8))  # a second refund can't exceed the payment
        self.assertEqual(out["budget_restored"], 0.0)
        self.assertTrue(pc.verify_evidence()["valid"])

    def test_unverified_without_webhook_id(self):
        with mock.patch.dict(os.environ, {"PAYPAL_WEBHOOK_ID": ""}):
            self.assertFalse(pc.verify_webhook({"paypal-transmission-id": "x"}, {}))


if __name__ == "__main__":
    unittest.main()
