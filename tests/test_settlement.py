"""Behavioural tests for shift settlement, rollback and over-limit control."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.comp import Bank
from coldstore.settlement import (
    LEVEL_HARD,
    LEVEL_OK,
    LEVEL_SHED,
    LEVEL_WARN,
    Settlement,
    enforce,
    level_for,
    summarize_entries,
)
from coldstore.settlement.report import PeriodEntry
from coldstore.store import Store
from coldstore.valve import SupplyValve


def _entry(seq: int, kind: str, amount: float, detail: str = "") -> PeriodEntry:
    return PeriodEntry(id=f"e{seq}", seq=seq, time="t", kind=kind, amount=amount, detail=detail)


class ArithmeticCase(unittest.TestCase):
    def test_closing_identity_is_pinned(self) -> None:
        entries = [
            _entry(1, "charge", 30.0),
            _entry(2, "charge", 20.0),
            _entry(3, "adjust", -5.0, "refund"),
        ]
        charges, adjustments, closing, mean, samples = summarize_entries(100.0, entries)
        self.assertEqual((charges, adjustments, closing), (50.0, -5.0, 45.0))
        self.assertEqual(samples, 3)
        # Mean is a report-only extra, never used for carry over.
        self.assertEqual(mean, (70.0 + 50.0 + 45.0) / 3)

    def test_empty_period_keeps_opening_balance(self) -> None:
        charges, adjustments, closing, mean, samples = summarize_entries(80.0, [])
        self.assertEqual((charges, adjustments, closing, samples), (0.0, 0.0, 80.0, 0))
        self.assertEqual(mean, 80.0)

    def test_tier_classification(self) -> None:
        self.assertEqual(level_for(70, 100, 0.8, 0.9)[0], LEVEL_OK)
        self.assertEqual(level_for(80, 100, 0.8, 0.9)[0], LEVEL_WARN)
        self.assertEqual(level_for(90, 100, 0.8, 0.9)[0], LEVEL_SHED)
        self.assertEqual(level_for(100, 100, 0.8, 0.9)[0], LEVEL_HARD)
        # An overdrawn shift handed over starts straight at the hard tier.
        self.assertEqual(level_for(5, 0, 0.8, 0.9)[0], LEVEL_HARD)
        self.assertEqual(level_for(5, -10, 0.8, 0.9)[0], LEVEL_HARD)


class SettlementCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.settlement = Settlement(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_first_charge_bootstraps_a_shift(self) -> None:
        view = self.settlement.charge(10.0)
        self.assertEqual(view.shift, "shift-0001")
        self.assertEqual(view.status, "open")
        self.assertEqual(view.opening_balance, 100.0)
        self.assertEqual(view.closing_balance, 90.0)
        self.assertEqual(view.allowance, 100.0)

    def test_shift_carry_over_and_cross_shift_chain(self) -> None:
        self.settlement.charge(40.0)
        first = self.settlement.settle()
        self.assertEqual(first.closing_balance, 60.0)
        self.assertTrue(first.settled_at)

        second = self.settlement.start_shift("night")
        self.assertEqual(second.opening_balance, 60.0)
        self.settlement.charge(70.0)  # overdraws across the shift boundary
        night = self.settlement.settle()
        self.assertEqual(night.closing_balance, -10.0)

        chain = self.settlement.verify_chain()
        self.assertTrue(chain["consistent"], chain["issues"])

        # The next shift inherits the debt rather than wrapping back to 100.
        third = self.settlement.start_shift("morning")
        self.assertEqual(third.opening_balance, -10.0)

    def test_settle_twice_and_start_without_closing(self) -> None:
        self.settlement.charge(5.0)
        with self.assertRaises(ValueError):
            self.settlement.start_shift("other")
        with self.assertRaises(ValueError):
            self.settlement.charge(1.0, shift="night")

    def test_period_lookup_returns_versions_in_order(self) -> None:
        self.settlement.charge(90.0)
        self.settlement.settle()
        self.settlement.start_shift("night")
        self.settlement.rollback(correction=-10.0, reason="meter misread")
        views = self.settlement.find_shift("shift-0001")
        self.assertEqual([view.status for view in views], ["superseded", "settled"])
        self.assertEqual(views[-1].closing_balance, 0.0)
        # Latest version is the canonical read.
        self.assertEqual(self.settlement.period("shift-0001").closing_balance, 0.0)

    def test_rollback_realigns_open_successor_and_keeps_alignment(self) -> None:
        self.settlement.charge(90.0)
        self.settlement.settle()  # closing 10
        self.settlement.start_shift("night")
        self.settlement.charge(5.0)  # live balance 5, opening 10

        # Correct the first shift: it really consumed 80, so closing must be 20.
        recomputed = self.settlement.rollback(correction=10.0, reason="meter over-read")
        self.assertEqual(recomputed.closing_balance, 20.0)
        night = self.settlement.period("night")
        self.assertEqual(night.opening_balance, 20.0)
        self.assertEqual(night.closing_balance, 15.0)

        chain = self.settlement.verify_chain()
        self.assertTrue(chain["consistent"], chain["issues"])

        # Balance read after rollback equals the recomputed result, not 10.
        self.assertEqual(self.settlement.balance("shift-0001").closing_balance, 20.0)
        self.assertEqual(self.settlement.balance("night").closing_balance, 15.0)

    def test_rollback_realignment_propagates_through_settled_successor(self) -> None:
        self.settlement.charge(90.0)
        self.settlement.settle()          # shift-0001 closing 10
        self.settlement.start_shift("b")
        self.settlement.charge(5.0)
        self.settlement.settle()          # b opening 10, closing 5
        self.settlement.start_shift("c")
        self.settlement.charge(2.0)       # c opening 5, live 3

        # A +10 correction refunds an over-read 10 units of runtime; targeting
        # the ancestor cascades the realignment through b and c.
        recomputed = self.settlement.rollback(
            correction=10.0, reason="refund meter over-read", shift="shift-0001"
        )
        self.assertEqual(recomputed.closing_balance, 20.0)
        self.assertEqual(self.settlement.period("b").opening_balance, 20.0)
        self.assertEqual(self.settlement.period("b").closing_balance, 15.0)
        self.assertEqual(self.settlement.period("c").opening_balance, 15.0)
        self.assertEqual(self.settlement.period("c").closing_balance, 13.0)
        self.assertTrue(self.settlement.verify_chain()["consistent"])

    def test_rollback_without_correction_recomputes_identically(self) -> None:
        self.settlement.charge(40.0)
        settled = self.settlement.settle()
        recomputed = self.settlement.rollback()
        self.assertEqual(recomputed.closing_balance, settled.closing_balance)
        self.assertTrue(self.settlement.verify_chain()["consistent"])

    def test_any_settled_shift_can_be_rolled_back_with_cascade(self) -> None:
        self.settlement.charge(1.0)
        self.settlement.settle()       # a closing 99
        self.settlement.start_shift("b")
        self.settlement.charge(1.0)
        self.settlement.settle()       # b opening 99, closing 98
        self.settlement.start_shift("c")
        self.settlement.charge(1.0)    # c live, opening 98

        # Targeting the ancestor recomputes it and cascades through b and c.
        view = self.settlement.rollback(correction=2.0, reason="late meter pulse", shift="shift-0001")
        self.assertEqual(view.shift, "shift-0001")
        self.assertEqual(self.settlement.period("shift-0001").closing_balance, 101.0)
        self.assertEqual(self.settlement.period("b").opening_balance, 101.0)
        self.assertEqual(self.settlement.period("b").closing_balance, 100.0)
        self.assertEqual(self.settlement.period("c").opening_balance, 100.0)
        self.assertEqual(self.settlement.period("c").closing_balance, 99.0)
        self.assertTrue(self.settlement.verify_chain()["consistent"])

        # The latest settlement still rolls back when no shift is given.
        self.assertEqual(self.settlement.rollback().shift, "b")

    def test_rollback_unknown_shift(self) -> None:
        self.settlement.charge(1.0)
        self.settlement.settle()
        with self.assertRaises(ValueError):
            self.settlement.rollback(shift="ghost")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                Settlement(Store.open(f"{tmp}/fresh.json")).rollback()

    def test_rollback_requires_reason_for_correction(self) -> None:
        self.settlement.charge(10.0)
        self.settlement.settle()
        with self.assertRaises(ValueError):
            self.settlement.rollback(correction=-5.0, reason="")

    def test_adjustments_are_signed_and_justified(self) -> None:
        self.settlement.charge(50.0)
        with self.assertRaises(ValueError):
            self.settlement.adjust(0.0, "noop")
        with self.assertRaises(ValueError):
            self.settlement.adjust(5.0, "")
        view = self.settlement.adjust(-20.0, "refund bad meter pulse")
        self.assertEqual(view.closing_balance, 30.0)

    def test_config_validation_and_ordering(self) -> None:
        config = self.settlement.configure(limit=200.0, warn_ratio=0.5, shed_ratio=0.75)
        self.assertEqual(config["limit"], 200.0)
        with self.assertRaises(ValueError):
            self.settlement.configure(warn_ratio=0.95)
        with self.assertRaises(ValueError):
            self.settlement.configure(limit=0)
        with self.assertRaises(ValueError):
            self.settlement.configure(shed_ratio=1.5)

    def test_persistence_round_trip(self) -> None:
        self.settlement.charge(35.0)
        self.settlement.settle()
        self.settlement.start_shift("night")
        self.settlement.charge(8.0)
        reopened = Settlement(Store.open(self.store.path))
        self.assertEqual(reopened.open_period().shift, "night")
        self.assertEqual(reopened.open_period().opening_balance, 65.0)
        self.assertEqual(reopened.balance().closing_balance, 57.0)
        self.assertTrue(reopened.verify_chain()["consistent"])


class EnforcementCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.settlement = Settlement(self.store)
        self.bank = Bank()
        self.bank.add_compressor("c1", 4)
        self.bank.add_compressor("c2", 2)
        self.supply = SupplyValve(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _start_all(self) -> None:
        self.supply.start_with_supply(self.bank, "c1")
        self.supply.start_with_supply(self.bank, "c2")
        self.bank.set_stage("c1", 4)
        self.bank.set_stage("c2", 2)

    def test_warn_does_not_touch_machines(self) -> None:
        self._start_all()
        self.settlement.charge(80.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_WARN)
        self.assertEqual(self.bank.running_count(), 2)
        self.assertEqual(self.settlement.shutdowns(), [])

    def test_shed_drops_highest_stage_one_step(self) -> None:
        self._start_all()
        self.settlement.charge(90.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_SHED)
        self.assertEqual(disposition.shed, ["c1"])  # c1 carried stage 4
        self.assertEqual(self.bank.stage("c1")[0], 3)
        self.assertTrue(self.bank.compressor("c1").running)
        self.assertTrue(self.supply.is_open("c1"))
        shutdowns = self.settlement.shutdowns()
        self.assertEqual(len(shutdowns), 1)
        self.assertEqual(shutdowns[0]["level"], LEVEL_SHED)
        self.assertIn("shed tier", shutdowns[0]["reason"])

    def test_shed_to_zero_stops_and_closes_valve(self) -> None:
        self.supply.start_with_supply(self.bank, "c2")
        self.bank.set_stage("c2", 1)
        self.settlement.charge(90.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        # c1 (stage 0) is not running, so the most loaded running machine is c2.
        self.assertEqual(disposition.level, LEVEL_SHED)
        self.assertEqual(disposition.shed, ["c2"])
        self.assertEqual(disposition.stopped, ["c2"])
        self.assertFalse(self.bank.compressor("c2").running)
        self.assertFalse(self.supply.is_open("c2"))

    def test_hard_stops_every_running_machine(self) -> None:
        self._start_all()
        self.settlement.charge(100.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_HARD)
        self.assertEqual(set(disposition.stopped), {"c1", "c2"})
        self.assertEqual(self.bank.running_count(), 0)
        self.assertEqual(self.supply.opened_ids(), [])
        shutdowns = self.settlement.shutdowns()
        self.assertEqual(len(shutdowns), 1)
        self.assertIn("quota exhausted", shutdowns[0]["reason"])
        self.assertEqual(shutdowns[0]["shift"], "shift-0001")

    def test_overdrawn_carry_over_hard_trips_immediately(self) -> None:
        self._start_all()
        self.settlement.charge(100.0)
        self.settlement.settle()
        self.settlement.start_shift("night")
        # No charge at all: inherited debt trips hard before new runtime lands.
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_HARD)
        self.assertEqual(self.bank.running_count(), 0)

    def test_custom_tiers(self) -> None:
        self._start_all()
        self.settlement.configure(warn_ratio=0.5, shed_ratio=0.6)
        self.settlement.charge(55.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_WARN)
        self.settlement.charge(6.0)
        disposition = enforce(self.settlement, self.bank, self.supply)
        self.assertEqual(disposition.level, LEVEL_SHED)


if __name__ == "__main__":
    unittest.main()
