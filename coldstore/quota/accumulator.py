"""Compatibility facade over the shift settlement ledger.

Older callers knew only a wrapping runtime counter. That counter is now the
usage total of the open shift, kept by :class:`~coldstore.quota.settlement.Settlement`.
A bare value found under the legacy key is migrated into the first shift once.
"""

from __future__ import annotations

from coldstore.store.epoch import load_float
from coldstore.store.store import Store

from .report import AccumState
from .settlement import DEFAULT_PERIOD_LIMIT, Settlement

ACCUM_KEY = "quota:runtime-accumulator"
EPOCH_CAP = DEFAULT_PERIOD_LIMIT


class Accumulator:
    """Presents shift usage through the old accumulator interface."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.settlement = Settlement(store)
        self._migrate_legacy()

    def add(self, amount: float) -> float:
        self.settlement.post_usage(amount)
        return self.value()

    def value(self) -> float:
        open_period = self.settlement.open()
        return open_period.used if open_period else 0.0

    def balance(self) -> float:
        open_period = self.settlement.open()
        return open_period.balance if open_period else 0.0

    def remaining(self, limit: float) -> float:
        remaining = limit - self.value()
        return remaining if remaining > 0 else 0.0

    def reset(self) -> None:
        """Back out the open shift's posted usage via an audited adjustment."""

        open_period = self.settlement.open()
        if open_period is not None and open_period.used:
            self.settlement.adjust(open_period.seq, -open_period.used, note="reset")

    def state(self) -> AccumState:
        return AccumState(value=self.value())

    def describe(self) -> str:
        return f"quota accumulator value={self.value():.4f}"

    def _migrate_legacy(self) -> None:
        value, had_value = load_float(self._store, ACCUM_KEY)
        if not had_value:
            return
        view = self.settlement.view()
        if not view.periods and value > 0:
            self.settlement.post_usage(value, note="legacy-migration")
        self._store.delete(ACCUM_KEY)
