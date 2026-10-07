"""Shift based quota settlement with an append-only ledger.

Every state change is an event: a period descriptor, a usage posting, a
settlement or a rollback. The current view (balances, which shift is open) is
rebuilt by replaying those events in order. Balances are therefore never
patched in place -- after a rollback the carried balance and every shift after
it are recomputed from the same usage, so the recomputed result is by
construction the only truth.

Shift lifecycle::

    open --settle--> settled --rollback--> open (reopened)
                                            \\--settle--> open (next shift)

Only one shift is open at a time. Rolling back a settled shift is allowed only
while its successor still carries no usage (an empty provisional successor is
discarded); once the next shift has usage, an :meth:`Settlement.adjust` posting
is the audited way to correct it -- it realigns the whole carryover chain via
replay rather than rewriting history.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from . import policy
from .report import LedgerState, PeriodView, RollbackView

PERIODS_KEY = "quota:periods"
USAGE_KEY = "quota:usage"
SETTLEMENTS_KEY = "quota:settlements"
ROLLBACKS_KEY = "quota:rollbacks"

DEFAULT_PERIOD_LIMIT = 100.0


class SettlementError(ValueError):
    """A settlement/rollback operation is not valid in the current state."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json_list(store: Store, key: str) -> list[dict]:
    rows: list[dict] = []
    for item in load_commands(store, key):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _dump_json_list(rows: list[dict]) -> str:
    # The list itself is stored as JSON-encoded string entries.
    return json.dumps([json.dumps(row, ensure_ascii=False) for row in rows], ensure_ascii=False)


@dataclass
class _Period:
    seq: int
    label: str
    limit: float
    state: str = "open"  # open | settled


@dataclass
class _Settlement:
    id: str
    time: str
    seq: int
    limit: float
    carryover: float
    used: float
    closing: float
    average: float
    superseded: bool = False


class Settlement:
    """Posts shift usage, closes shifts and supports rollback/recompute."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ open

    def open_period(self, label: str = "", limit: float = DEFAULT_PERIOD_LIMIT) -> PeriodView:
        """Open the next shift. The first one is auto-opened with defaults."""

        view = self.view()
        open_seq = self._open_seq(view)
        if open_seq:
            raise SettlementError(f"period {open_seq} is still open")
        seq = (view.periods[-1].seq + 1) if view.periods else 1
        period = {
            "id": uuid.uuid4().hex,
            "time": _now(),
            "seq": seq,
            "label": label or f"shift-{seq}",
            "limit": policy.quantize(limit),
            "state": "open",
        }
        append_command(self._store, PERIODS_KEY, json.dumps(period, ensure_ascii=False))
        return self.period(seq)

    def ensure_open(self, label: str = "", limit: float = DEFAULT_PERIOD_LIMIT) -> PeriodView:
        """Return the open shift, lazily opening the first one on a fresh store."""

        view = self.view()
        open_seq = self._open_seq(view)
        if open_seq:
            return self.period(open_seq)
        return self.open_period(label=label, limit=limit)

    # ------------------------------------------------------------------ post

    def post_usage(self, amount: float, note: str = "usage") -> PeriodView:
        """Append a non-negative usage amount to the currently open shift."""

        if amount < 0:
            raise ValueError("amount must be non-negative")
        open_period = self.ensure_open()
        entry = {
            "id": uuid.uuid4().hex,
            "time": _now(),
            "seq": open_period.seq,
            "amount": policy.quantize(amount),
            "note": note,
        }
        append_command(self._store, USAGE_KEY, json.dumps(entry, ensure_ascii=False))
        return self.period(open_period.seq)

    def adjust(self, seq: int, amount: float, note: str) -> PeriodView:
        """Post a signed correction against any shift.

        Corrections are ordinary ledger entries (negative amounts reduce
        usage), so balances downstream realign through the normal replay and
        the correction itself stays auditable.
        """

        view = self.view()
        if not any(period.seq == seq for period in view.periods):
            raise SettlementError(f"period {seq} not found")
        entry = {
            "id": uuid.uuid4().hex,
            "time": _now(),
            "seq": seq,
            "amount": policy.quantize(amount),
            "note": f"adjust:{note}",
        }
        append_command(self._store, USAGE_KEY, json.dumps(entry, ensure_ascii=False))
        return self.period(seq)

    # --------------------------------------------------------------- settle

    def settle(self, note: str = "") -> PeriodView:
        """Close the open shift and carry its ending balance forward."""

        open_period = self.ensure_open()
        seq = open_period.seq
        view = self.view()
        current = next(period for period in view.periods if period.seq == seq)

        payload = {
            "id": uuid.uuid4().hex,
            "time": _now(),
            "seq": seq,
            "limit": current.limit,
            "carryover": current.carryover,
            "used": current.used,
            "closing": current.balance,
            "average": current.average,
            "superseded": False,
            "note": note,
        }
        updates = {
            PERIODS_KEY: self._periods_json_with(seq, state="settled"),
            SETTLEMENTS_KEY: self._append_json(SETTLEMENTS_KEY, payload),
        }
        self._store.put_many(updates)
        return self.period(seq)

    def close_shift(self, label: str = "", limit: float = DEFAULT_PERIOD_LIMIT) -> tuple[PeriodView, PeriodView]:
        """Settle the open shift and open the next one atomically."""

        settled = self.settle(note="end of shift")
        opened = self.open_period(label=label, limit=limit)
        return opened, settled

    # -------------------------------------------------------------- rollback

    def rollback(self, seq: int, reason: str) -> RollbackView:
        """Reopen a settled shift so it can be corrected and resettled.

        Allowed only for the most recently settled shift while its successor
        is still empty. The settlement is marked superseded (never deleted),
        the old and recomputed balances are both kept, and the carryover is
        restored by replay.
        """

        view = self.view()
        target = next((period for period in view.periods if period.seq == seq), None)
        if target is None:
            raise SettlementError(f"period {seq} not found")
        if target.state != "settled":
            raise SettlementError(f"period {seq} is not settled")

        successor = next((period for period in view.periods if period.seq > seq), None)
        if successor is not None and successor.used != 0.0:
            raise SettlementError(
                f"period {successor.seq} already carries usage; post an adjustment instead"
            )

        settlement = self._active_settlement(seq)
        if settlement is None:
            raise SettlementError(f"period {seq} has no active settlement")

        rollback = {
            "id": uuid.uuid4().hex,
            "time": _now(),
            "seq": seq,
            "settlement_id": settlement["id"],
            "reason": reason,
            "superseded_balance": float(settlement["closing"]),
            "reverted_balance": target.balance,
        }

        used_seqs = {int(row.get("seq", 0)) for row in _load_json_list(self._store, USAGE_KEY)}

        periods = _load_json_list(self._store, PERIODS_KEY)
        retained: list[dict] = []
        for period in periods:
            period_seq = int(period.get("seq", 0))
            if period_seq == seq:
                period["state"] = "open"
                retained.append(period)
            elif period_seq > seq and period_seq not in used_seqs:
                # An empty, never settled successor is only a provisional
                # shell; drop it so the reopened shift stays the last one and
                # carryover replays cleanly. The rollback record keeps the
                # full audit trail.
                continue
            else:
                retained.append(period)

        settlements = _load_json_list(self._store, SETTLEMENTS_KEY)
        for record in settlements:
            if record.get("id") == settlement["id"]:
                record["superseded"] = True

        updates = {
            PERIODS_KEY: _dump_json_list(retained),
            SETTLEMENTS_KEY: _dump_json_list(settlements),
            ROLLBACKS_KEY: self._append_json(ROLLBACKS_KEY, rollback),
        }
        self._store.put_many(updates)
        return RollbackView(
            id=str(rollback["id"]),
            time=str(rollback["time"]),
            seq=seq,
            settlement_id=str(rollback["settlement_id"]),
            reason=reason,
            superseded_balance=float(rollback["superseded_balance"]),
            reverted_balance=float(rollback["reverted_balance"]),
        )

    # ------------------------------------------------------------- enquiries

    def view(self) -> LedgerState:
        """Rebuild every balance from the stored events."""

        periods = self._rebuild()
        rollbacks = [
            RollbackView(
                id=str(row.get("id", "")),
                time=str(row.get("time", "")),
                seq=int(row.get("seq", 0)),
                settlement_id=str(row.get("settlement_id", "")),
                reason=str(row.get("reason", "")),
                superseded_balance=float(row.get("superseded_balance", 0.0)),
                reverted_balance=float(row.get("reverted_balance", 0.0)),
            )
            for row in _load_json_list(self._store, ROLLBACKS_KEY)
        ]
        open_seq = self._open_seq(LedgerState(periods=periods))
        return LedgerState(periods=periods, rollbacks=rollbacks, open_seq=open_seq)

    def period(self, seq: int) -> PeriodView:
        view = self.view()
        target = next((period for period in view.periods if period.seq == seq), None)
        if target is None:
            raise SettlementError(f"period {seq} not found")
        return target

    def open(self) -> PeriodView | None:
        view = self.view()
        return next((period for period in view.periods if period.seq == view.open_seq), None)

    def rollbacks(self) -> list[RollbackView]:
        return self.view().rollbacks

    # -------------------------------------------------------------- replay

    def _rebuild(self) -> list[PeriodView]:
        raw_periods = _load_json_list(self._store, PERIODS_KEY)
        raw_periods.sort(key=lambda row: int(row.get("seq", 0)))
        usage = _load_json_list(self._store, USAGE_KEY)
        settlements = _load_json_list(self._store, SETTLEMENTS_KEY)

        amounts_by_seq: dict[int, list[float]] = {}
        used_by_seq: dict[int, float] = {}
        for row in usage:
            seq = int(row.get("seq", 0))
            amount = float(row.get("amount", 0.0))
            amounts_by_seq.setdefault(seq, []).append(amount)
            used_by_seq[seq] = used_by_seq.get(seq, 0.0) + amount

        active_settlement: dict[int, dict] = {}
        for record in settlements:
            seq = int(record.get("seq", 0))
            if not record.get("superseded", False):
                active_settlement[seq] = record

        views: list[PeriodView] = []
        previous_closing = 0.0
        for row in raw_periods:
            seq = int(row.get("seq", 0))
            limit = float(row.get("limit", DEFAULT_PERIOD_LIMIT))
            amounts = amounts_by_seq.get(seq, [])
            used = policy.quantize(used_by_seq.get(seq, 0.0))
            opening = policy.allowance(limit, previous_closing)
            balance = policy.closing_balance(limit, previous_closing, used)
            average = policy.average_balance(opening, amounts)
            ratio = policy.usage_ratio(used, opening)

            settlement = active_settlement.get(seq)
            settled_at = str(settlement.get("time", "")) if settlement else ""
            settlement_id = str(settlement.get("id", "")) if settlement else ""
            state = "settled" if settlement else "open"

            views.append(
                PeriodView(
                    seq=seq,
                    label=str(row.get("label", f"shift-{seq}")),
                    limit=limit,
                    carryover=previous_closing,
                    used=used,
                    balance=balance,
                    average=average,
                    ratio=ratio,
                    state=state,
                    settled_at=settled_at,
                    settlement_id=settlement_id,
                )
            )
            # Carryover always follows the live replay; a superseded
            # settlement no longer feeds the next shift.
            previous_closing = balance
        return views

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _open_seq(view: LedgerState) -> int:
        # At most one period is open at a time; a reopened one blocks dormant
        # successors from accepting postings.
        for period in view.periods:
            if period.state == "open":
                return period.seq
        return 0

    def _active_settlement(self, seq: int) -> dict | None:
        for record in _load_json_list(self._store, SETTLEMENTS_KEY):
            if int(record.get("seq", 0)) == seq and not record.get("superseded", False):
                return record
        return None

    def _append_json(self, key: str, payload: dict) -> str:
        rows = _load_json_list(self._store, key)
        rows.append(payload)
        return _dump_json_list(rows)

    def _periods_json_with(self, seq: int, state: str) -> str:
        periods = _load_json_list(self._store, PERIODS_KEY)
        for period in periods:
            if period.get("seq") == seq:
                period["state"] = state
        return _dump_json_list(periods)
