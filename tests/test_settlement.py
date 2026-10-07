"""Shift quota settlement: carryover, rollback/recompute and enforcement."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.quota import (
    Accumulator,
    Enforcement,
    Settlement,
    SettlementError,
    average_balance,
    closing_balance,
    escalate,
    level_for_ratio,
    policy,
    usage_ratio,
)
from coldstore.quota.policy import LEVEL_NONE, LEVEL_SHED, LEVEL_STOP, LEVEL_WARN
from coldstore.store import Store


class PolicyCase(unittest.TestCase):
    def test_closing_balance_and_carryover_math(self) -> None:
        self.assertEqual(closing_balance(100.0, 10.0, 30.0), 80.0)
        self.assertEqual(closing_balance(100.0, 0.0, 130.0), -30.0)
        self.assertEqual(usage_ratio(80.0, 100.0), 0.8)
        self.assertEqual(usage_ratio(120.0, 100.0), 1.2)
        self.assertEqual(usage_ratio(5.0, 0.0), float("inf"))

    def test_average_is_replayable(self) -> None:
        # balances after each posting: 80, 50, 20
        self.assertEqual(average_balance(100.0, [20.0, 30.0, 30.0]), 50.0)
        self.assertEqual(average_balance(40.0, []), 40.0)

    def test_levels_escalate_only(self) -> None:
        self.assertEqual(level_for_ratio(0.5), LEVEL_NONE)
        self.assertEqual(level_for_ratio(0.8), LEVEL_WARN)
        self.assertEqual(level_for_ratio(1.0), LEVEL_SHED)
        self.assertEqual(level_for_ratio(1.2), LEVEL_STOP)
        self.assertEqual(escalate(LEVEL_NONE, LEVEL_STOP), [LEVEL_WARN, LEVEL_SHED, LEVEL_STOP])
        self.assertEqual(escalate(LEVEL_SHED, LEVEL_WARN), [])


class SettlementCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.settlement = Settlement(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_posting_lazily_opens_first_shift(self) -> None:
        period = self.settlement.post_usage(30.0)
        self.assertEqual(period.seq, 1)
        self.assertEqual(period.used, 30.0)
        self.assertEqual(period.balance, 70.0)
        self.assertEqual(period.state, "open")

    def test_settlement_carries_end_balance_forward(self) -> None:
        self.settlement.post_usage(30.0)
        first = self.settlement.settle()
        self.assertEqual(first.balance, 70.0)
        self.assertEqual(first.state, "settled")

        second = self.settlement.open_period(label="night", limit=100.0)
        self.assertEqual(second.carryover, 70.0)
        self.settlement.post_usage(50.0)
        open_period = self.settlement.open()
        # allowance 170, used 50 -> closing 120
        self.assertEqual(open_period.balance, 120.0)

    def test_overdrawn_balance_carries_as_debt(self) -> None:
        self.settlement.post_usage(130.0)
        first = self.settlement.settle()
        self.assertEqual(first.balance, -30.0)
        second = self.settlement.open_period(limit=100.0)
        # debt rolls straight into the next shift
        self.assertEqual(second.carryover, -30.0)
        self.assertEqual(second.balance, 70.0)

    def test_close_shift_settles_and_opens_atomically(self) -> None:
        self.settlement.post_usage(20.0)
        opened, settled = self.settlement.close_shift(label="next", limit=80.0)
        self.assertEqual(settled.seq, 1)
        self.assertEqual(opened.seq, 2)
        self.assertEqual(opened.limit, 80.0)
        self.assertEqual(opened.carryover, 80.0)
        self.assertEqual(self.settlement.view().open_seq, 2)

    def test_rollback_reopens_and_realigns_balance(self) -> None:
        self.settlement.post_usage(90.0)
        first = self.settlement.settle()
        self.assertEqual(first.balance, 10.0)
        self.settlement.open_period()  # empty successor

        rollback = self.settlement.rollback(1, reason="meter misread")
        self.assertEqual(rollback.superseded_balance, 10.0)
        # no postings removed yet: reverted replay matches the recorded figure
        self.assertEqual(rollback.reverted_balance, 10.0)

        # correct the usage, resettle; balances come straight from replay
        self.settlement.adjust(1, -30.0, note="meter misread")
        corrected = self.settlement.settle(note="resettled")
        self.assertEqual(corrected.used, 60.0)
        self.assertEqual(corrected.balance, 40.0)

        rollbacks = self.settlement.rollbacks()
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(rollbacks[0].reason, "meter misread")

        # successor now carries the corrected closing balance
        successor = self.settlement.open_period()
        self.assertEqual(successor.carryover, 40.0)

    def test_rollback_blocked_once_successor_has_usage(self) -> None:
        self.settlement.post_usage(50.0)
        self.settlement.settle()
        self.settlement.open_period()
        self.settlement.post_usage(10.0)
        with self.assertRaises(SettlementError):
            self.settlement.rollback(1, reason="late correction")

    def test_rollback_only_valid_for_settled_shift(self) -> None:
        self.settlement.post_usage(10.0)
        with self.assertRaises(SettlementError):
            self.settlement.rollback(1, reason="nope")

    def test_adjustment_on_later_shift_realigns_chain(self) -> None:
        self.settlement.post_usage(60.0)
        self.settlement.settle()
        self.settlement.open_period()
        self.settlement.post_usage(40.0)
        self.settlement.settle()
        self.settlement.open_period()

        # period 2 closes at 100 (40 carry + 100 limit - 40 used)
        self.assertEqual(self.settlement.period(2).balance, 100.0)
        # a 20 unit correction against shift 1 lowers every downstream carry
        self.settlement.adjust(1, 20.0, note="missed reading")
        self.assertEqual(self.settlement.period(1).balance, 20.0)
        self.assertEqual(self.settlement.period(2).balance, 80.0)
        self.assertEqual(self.settlement.open().carryover, 80.0)

    def test_recomputed_view_is_identical_after_reload(self) -> None:
        self.settlement.post_usage(20.0)
        self.settlement.settle()
        self.settlement.open_period()
        self.settlement.post_usage(70.0)
        before = self.settlement.view()

        reloaded = Settlement(Store.open(self.store.path))
        after = reloaded.view()
        self.assertEqual(
            [(p.seq, p.used, p.balance, p.carryover, p.state) for p in before.periods],
            [(p.seq, p.used, p.balance, p.carryover, p.state) for p in after.periods],
        )

    def test_accumulator_facade_tracks_open_usage(self) -> None:
        accumulator = Accumulator(self.store)
        self.assertEqual(accumulator.add(25.0), 25.0)
        self.assertEqual(accumulator.value(), 25.0)
        self.assertEqual(accumulator.remaining(100.0), 75.0)
        accumulator.reset()
        self.assertEqual(accumulator.value(), 0.0)
        # reset is an audited adjustment, not a deletion
        self.assertEqual(self.settlement.open().used, 0.0)


class EnforcementCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.settlement = Settlement(self.store)
        self.enforcement = Enforcement(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _levels_until(self, amount: float) -> list[str]:
        self.settlement.post_usage(amount)
        return self.enforcement.evaluate(self.settlement)

    def test_latch_escalates_and_holds(self) -> None:
        self.assertEqual(self._levels_until(80.0), [LEVEL_WARN])
        # staying within WARN re-evaluates to nothing
        self.settlement.post_usage(5.0)
        self.assertEqual(self.enforcement.evaluate(self.settlement), [])
        self.assertEqual(self.enforcement.current_level(1), LEVEL_WARN)
        self.settlement.post_usage(20.0)  # 105 of 100 -> still SHED, not STOP
        self.assertEqual(self.enforcement.evaluate(self.settlement), [LEVEL_SHED])
        self.settlement.post_usage(10.0)  # 115 of 100 -> STOP
        self.assertEqual(self.enforcement.evaluate(self.settlement), [LEVEL_STOP])
        self.assertEqual(self.enforcement.current_level(1), LEVEL_STOP)

    def test_reset_clears_latch_for_operator(self) -> None:
        self._levels_until(90.0)
        self.assertEqual(self.enforcement.current_level(1), LEVEL_WARN)
        self.enforcement.reset(1, reason="cleared")
        self.assertEqual(self.enforcement.current_level(1), LEVEL_NONE)

    def test_each_shift_has_independent_latch(self) -> None:
        self._levels_until(80.0)
        self.settlement.settle()
        self.settlement.open_period()
        self.assertEqual(self.enforcement.current_level(2), LEVEL_NONE)
        self.assertEqual(self.enforcement.current_level(1), LEVEL_WARN)


if __name__ == "__main__":
    unittest.main()
