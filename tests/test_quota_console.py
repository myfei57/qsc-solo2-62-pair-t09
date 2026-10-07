"""End to end quota settlement routes: periods, rollback and shed/stop."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.store import Store


class QuotaConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_shift_carryover_and_balance_lookup(self) -> None:
        status, body = self.call("POST", "/quota/add", {"amount": 70.0})
        self.assertEqual(status, 200)
        self.assertEqual(body["balance"], 30.0)

        status, body = self.call("POST", "/quota/period/close", {"label": "night", "limit": 100.0})
        self.assertEqual(body["settled"]["state"], "settled")
        self.assertEqual(body["settled"]["balance"], 30.0)
        self.assertEqual(body["open"]["seq"], 2)
        self.assertEqual(body["open"]["carryover"], 30.0)

        self.call("POST", "/quota/add", {"amount": 40.0})
        status, body = self.call("GET", "/quota/balance", {"seq": 2})
        self.assertEqual(body["period"]["used"], 40.0)
        # 100 limit + 30 carry - 40 used
        self.assertEqual(body["period"]["balance"], 90.0)

        status, body = self.call("GET", "/quota/ledger")
        self.assertEqual(status, 200)
        self.assertEqual([p["seq"] for p in body["periods"]], [1, 2])
        self.assertEqual(body["periods"][1]["carryover"], 30.0)

    def test_rollback_recomputes_and_realignment_is_visible(self) -> None:
        self.call("POST", "/quota/add", {"amount": 90.0})
        self.call("POST", "/quota/period/settle", {})
        self.call("POST", "/quota/period/open", {})

        status, body = self.call(
            "POST", "/quota/period/rollback", {"seq": 1, "reason": "meter misread"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["rollback"]["superseded_balance"], 10.0)
        self.assertEqual(body["period"]["state"], "open")

        self.call("POST", "/quota/period/adjust", {"seq": 1, "amount": -30.0, "note": "misread"})
        status, body = self.call("POST", "/quota/period/settle", {"note": "resettled"})
        self.assertEqual(body["period"]["used"], 60.0)
        self.assertEqual(body["period"]["balance"], 40.0)

        status, body = self.call("GET", "/quota/rollbacks")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["rollbacks"][0]["reason"], "meter misread")

    def test_rollback_refused_once_next_shift_used(self) -> None:
        self.call("POST", "/quota/add", {"amount": 50.0})
        self.call("POST", "/quota/period/settle", {})
        self.call("POST", "/quota/period/open", {})
        self.call("POST", "/quota/add", {"amount": 5.0})
        status, body = self.call("POST", "/quota/period/rollback", {"seq": 1, "reason": "late"})
        self.assertEqual(status, 400)
        self.assertIn("adjustment", body["error"])

    def test_shed_then_stop_enforcement_touches_compressors(self) -> None:
        self.call("POST", "/comp/add", {"id": "c1", "stages": 3})
        self.call("POST", "/comp/add", {"id": "c2", "stages": 2})
        self.call("POST", "/comp/start", {"id": "c1"})
        self.call("POST", "/comp/start", {"id": "c2"})
        self.call("POST", "/comp/stage", {"id": "c1", "stage": 3})
        self.call("POST", "/comp/stage", {"id": "c2", "stage": 2})

        status, body = self.call("POST", "/quota/add", {"amount": 100.0})
        self.assertEqual(status, 200)
        self.assertEqual(body["enforcement"]["level"], "SHED")
        # shed drops one stage per machine but keeps them running
        _, bank = self.call("GET", "/comp")
        stages = {c["id"]: c["stage"] for c in bank["compressors"]}
        self.assertEqual(stages, {"c1": 2, "c2": 1})
        self.assertEqual(bank["running"], 2)

        status, body = self.call("POST", "/quota/add", {"amount": 12.0})  # 112%
        self.assertEqual(body["enforcement"]["level"], "STOP")
        _, bank = self.call("GET", "/comp")
        self.assertEqual(bank["running"], 0)
        self.assertTrue(all(c["stage"] == 0 for c in bank["compressors"]))

        # latch does not auto-recover
        _, enforcement = self.call("GET", "/quota/enforcement")
        self.assertEqual(enforcement["level"], "STOP")
        status, _ = self.call("POST", "/quota/enforcement/reset", {"reason": "cleared"})
        self.assertEqual(status, 200)
        _, enforcement = self.call("GET", "/quota/enforcement")
        self.assertEqual(enforcement["level"], "NONE")

    def test_settlement_actions_land_in_audit(self) -> None:
        self.call("POST", "/quota/add", {"amount": 100.0})
        self.call("POST", "/quota/period/settle", {})
        self.call("POST", "/quota/period/open", {})
        self.call("POST", "/quota/period/rollback", {"seq": 1, "reason": "x"})
        status, summary = self.call("GET", "/audit/summary")
        self.assertEqual(status, 200)
        for kind in ("quota-post", "quota-settle", "quota-open", "quota-rollback", "quota-shed"):
            self.assertIn(kind, summary["by_kind"])

    def test_csv_export_lists_period_balances(self) -> None:
        self.call("POST", "/quota/add", {"amount": 25.0})
        self.call("POST", "/quota/period/settle", {})
        status, csv_text = self.call("GET", "/export/quota")
        self.assertEqual(status, 200)
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], "seq,label,state,limit,carryover,used,balance,average,ratio")
        self.assertIn("shift-1", lines[1])
        self.assertIn("settled", lines[1])


if __name__ == "__main__":
    unittest.main()
