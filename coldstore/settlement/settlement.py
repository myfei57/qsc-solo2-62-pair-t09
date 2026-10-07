"""Shift based quota settlement ledger.

The ledger is the single source of truth for quota across shifts. It replaces
the old wrapping counter: runtime is charged into the **open** period, a
period is settled at the end of a shift, and its closing balance becomes the
next shift's opening balance (carry over). A settled period may be rolled back
and recomputed; the recomputed closing balance is propagated forward so the
whole chain stays aligned.

Everything the ledger owns lives under one store key as one JSON document, so
a settle or a rollback commits atomically with the store's temp-file-and-rename
write. There is no multi-key window in which consistency could be broken.

Balances follow the pinned identity ``closing = opening + adjustments -
charges`` (see :mod:`coldstore.settlement.report`).
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from coldstore.store.store import Store

from .report import (
    PeriodEntry,
    PeriodView,
    summarize_entries,
    validate_adjustment,
    validate_limit,
    validate_ratio,
    validate_shift_id,
)

LEDGER_KEY = "settlement:ledger"

DEFAULT_LIMIT = 100.0
DEFAULT_WARN_RATIO = 0.80
DEFAULT_SHED_RATIO = 0.90
HARD_RATIO = 1.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _entry_from_payload(payload: dict[str, object]) -> PeriodEntry:
    return PeriodEntry(
        id=str(payload.get("id", "")),
        seq=int(payload.get("seq", 0)),
        time=str(payload.get("time", "")),
        kind=str(payload.get("kind", "")),
        amount=float(payload.get("amount", 0.0)),
        detail=str(payload.get("detail", "")),
    )


def _view(period: dict[str, object]) -> PeriodView:
    entries = [_entry_from_payload(item) for item in period.get("entries", [])]  # type: ignore[arg-type]
    opening = float(period["opening_balance"])
    charges, adjustments, closing, mean_balance, samples = summarize_entries(opening, entries)
    if period.get("status") != "open":
        stored_closing = period.get("closing_balance")
        if stored_closing is not None:
            closing = float(stored_closing)
    return PeriodView(
        shift=str(period["shift"]),
        status=str(period["status"]),
        opening_balance=opening,
        total_charges=charges,
        total_adjustments=adjustments,
        closing_balance=closing,
        mean_balance=mean_balance,
        mean_samples=samples,
        entry_count=len(entries),
        settled_at=str(period.get("settled_at", "")),
        settlement_id=str(period.get("settlement_id", "")),
        superseded_by=str(period.get("superseded_by", "")),
        entries=[entry.as_dict() for entry in entries],
    )


class Settlement:
    """Owns periods, rollback versions and quota configuration."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ state

    def _document(self) -> dict[str, object]:
        raw, present = self._store.get(LEDGER_KEY)
        if not present:
            return self._default_document()
        try:
            document = json.loads(raw)
        except ValueError:
            return self._default_document()
        if not isinstance(document, dict) or not isinstance(document.get("periods"), list):
            return self._default_document()
        document.setdefault("config", self._default_config())
        document.setdefault("shutdowns", [])
        document.setdefault("seq", 0)
        return document

    @staticmethod
    def _default_config() -> dict[str, float]:
        return {
            "limit": DEFAULT_LIMIT,
            "warn_ratio": DEFAULT_WARN_RATIO,
            "shed_ratio": DEFAULT_SHED_RATIO,
            "hard_ratio": HARD_RATIO,
        }

    def _default_document(self) -> dict[str, object]:
        return {"config": self._default_config(), "periods": [], "shutdowns": [], "seq": 0}

    def _save(self, document: dict[str, object]) -> None:
        self._store.put(LEDGER_KEY, json.dumps(document, ensure_ascii=False, sort_keys=True))

    def config(self) -> dict[str, float]:
        with self._lock:
            return dict(self._document()["config"])  # type: ignore[arg-type]

    def configure(
        self,
        limit: float | None = None,
        warn_ratio: float | None = None,
        shed_ratio: float | None = None,
    ) -> dict[str, float]:
        with self._lock:
            document = self._document()
            config = document["config"]  # type: ignore[assignment]
            assert isinstance(config, dict)
            if limit is not None:
                config["limit"] = validate_limit(limit)
            if warn_ratio is not None:
                config["warn_ratio"] = validate_ratio("warn_ratio", warn_ratio)
            if shed_ratio is not None:
                config["shed_ratio"] = validate_ratio("shed_ratio", shed_ratio)
            if config["warn_ratio"] > config["shed_ratio"]:
                raise ValueError("warn_ratio must not exceed shed_ratio")
            if config["shed_ratio"] > HARD_RATIO:
                raise ValueError("shed_ratio must not exceed the hard limit ratio 1.0")
            self._save(document)
            return dict(config)

    # ---------------------------------------------------------------- periods

    def open_period(self) -> PeriodView | None:
        with self._lock:
            periods = self._document()["periods"]
            assert isinstance(periods, list)
            for period in reversed(periods):
                if period["status"] == "open":  # type: ignore[index]
                    return _view(period)  # type: ignore[arg-type]
            return None

    def periods(self) -> list[PeriodView]:
        with self._lock:
            periods = self._document()["periods"]
            assert isinstance(periods, list)
            return [_view(period) for period in periods]

    def find_shift(self, shift: str) -> list[PeriodView]:
        """Every record version of one shift, in version order."""

        shift_id = validate_shift_id(shift)
        with self._lock:
            periods = self._document()["periods"]
            assert isinstance(periods, list)
            return [_view(period) for period in periods if period["shift"] == shift_id]  # type: ignore[index]

    def period(self, shift: str) -> PeriodView | None:
        """Latest record version of a shift."""

        views = self.find_shift(shift)
        return views[-1] if views else None

    def start_shift(self, shift: str | None = None) -> PeriodView:
        """Carry the last closing balance into a new open period."""

        with self._lock:
            document = self._document()
            periods = document["periods"]
            assert isinstance(periods, list)
            for period in periods:
                if period["status"] == "open":  # type: ignore[index]
                    raise ValueError(f"shift {period['shift']} is still open")  # type: ignore[index]

            if shift is None or not str(shift).strip():
                shift = f"shift-{len(periods) + 1:04d}"
            shift_id = validate_shift_id(shift)
            if any(period["shift"] == shift_id for period in periods):  # type: ignore[index]
                raise ValueError(f"shift {shift_id} already exists")

            opening = float(periods[-1]["closing_balance"]) if periods else float(document["config"]["limit"])  # type: ignore[index]
            periods.append(
                {
                    "shift": shift_id,
                    "status": "open",
                    "opening_balance": round(opening, 4),
                    "entries": [],
                }
            )
            self._save(document)
            return _view(periods[-1])

    def _require_open(self, periods: list[dict[str, object]]) -> dict[str, object]:
        for period in reversed(periods):
            if period["status"] == "open":
                return period
        raise ValueError("no open shift; start a shift first")

    # ---------------------------------------------------------------- posting

    def charge(self, amount: float, detail: str = "runtime", shift: str | None = None) -> PeriodView:
        """Post non-negative runtime usage against an open period."""

        if amount < 0:
            raise ValueError("amount must be non-negative")
        if amount > 1_000_000:
            raise ValueError("amount exceeds the supported range")
        with self._lock:
            document = self._document()
            periods = document["periods"]
            assert isinstance(periods, list)
            if not periods:
                periods.append(
                    {
                        "shift": "shift-0001",
                        "status": "open",
                        "opening_balance": float(document["config"]["limit"]),
                        "entries": [],
                    }
                )
            period = self._require_open(periods)
            if shift is not None and str(shift).strip() and period["shift"] != validate_shift_id(shift):
                raise ValueError(f"shift {shift} is not the open shift")
            seq = int(document.get("seq", 0)) + 1
            document["seq"] = seq
            period["entries"].append(  # type: ignore[attr-defined]
                {
                    "id": uuid.uuid4().hex,
                    "seq": seq,
                    "time": _now(),
                    "kind": "charge",
                    "amount": round(float(amount), 4),
                    "detail": str(detail)[:200],
                }
            )
            self._save(document)
            return _view(period)

    def adjust(self, amount: float, reason: str, shift: str | None = None) -> PeriodView:
        """Post a signed correction (refund or debit) against an open period."""

        signed = validate_adjustment(amount, reason)
        with self._lock:
            document = self._document()
            periods = document["periods"]
            assert isinstance(periods, list)
            period = self._require_open(periods)
            if shift is not None and str(shift).strip() and period["shift"] != validate_shift_id(shift):
                raise ValueError(f"shift {shift} is not the open shift")
            seq = int(document.get("seq", 0)) + 1
            document["seq"] = seq
            period["entries"].append(  # type: ignore[attr-defined]
                {
                    "id": uuid.uuid4().hex,
                    "seq": seq,
                    "time": _now(),
                    "kind": "adjust",
                    "amount": round(signed, 4),
                    "detail": reason.strip()[:200],
                }
            )
            self._save(document)
            return _view(period)

    # -------------------------------------------------------------- settling

    def settle(self) -> PeriodView:
        """Freeze the open period and lock its closing balance."""

        with self._lock:
            document = self._document()
            periods = document["periods"]
            assert isinstance(periods, list)
            period = self._require_open(periods)
            view = _view(period)
            period["status"] = "settled"
            period["closing_balance"] = view.closing_balance
            period["settled_at"] = _now()
            period["settlement_id"] = uuid.uuid4().hex
            self._save(document)
            return _view(period)

    def rollback(
        self,
        correction: float = 0.0,
        reason: str = "rollback recompute",
        shift: str | None = None,
    ) -> PeriodView:
        """Roll back a settlement, apply a correction, and recompute.

        Any settled shift may be targeted; by default the latest settlement is
        used. The frozen period becomes a ``superseded`` audit record and a
        fresh settled version carries the recomputed closing balance. Every
        later live settled shift is superseded and recomputed in turn with the
        carried delta arriving through its new opening balance, and a trailing
        open period has its opening balance realigned. After the cascade every
        link of the chain satisfies the balance identity, and balances read
        exactly like a fresh run.
"""

        signed = float(correction)
        if signed != 0.0:
            validate_adjustment(signed, reason)
        with self._lock:
            document = self._document()
            periods = document["periods"]
            assert isinstance(periods, list)
            target_index = self._settled_index(periods, shift)
            if target_index is None:
                raise ValueError("no settled shift to roll back")
            target = periods[target_index]
            target_shift = str(target["shift"])
            rollback_id = uuid.uuid4().hex
            stamped_reason = reason.strip()[:200]

            # Recompute the target period before mutating anything.
            entries = [_entry_from_payload(item) for item in target["entries"]]
            if signed != 0.0:
                seq = int(document.get("seq", 0)) + 1
                document["seq"] = seq
                entries.append(
                    PeriodEntry(
                        id=uuid.uuid4().hex,
                        seq=seq,
                        time=_now(),
                        kind="adjust",
                        amount=round(signed, 4),
                        detail=f"rollback: {stamped_reason[:188]}",
                    )
                )
            _, _, recomputed_closing, _, _ = summarize_entries(
                float(target["opening_balance"]), entries
            )
            delta = round(recomputed_closing - float(target["closing_balance"]), 4)

            replacement: dict[str, object] = {
                "shift": target_shift,
                "status": "settled",
                "opening_balance": target["opening_balance"],
                "entries": [entry.as_dict() for entry in entries],
                "closing_balance": recomputed_closing,
                "settled_at": _now(),
                "settlement_id": uuid.uuid4().hex,
                "rollback_of": target["settlement_id"],
                "rollback_id": rollback_id,
                "reason": stamped_reason if signed != 0.0 else "rollback recompute",
            }
            target["status"] = "superseded"
            target["superseded_by"] = replacement["settlement_id"]
            target["rollback_id"] = rollback_id
            periods.insert(target_index + 1, replacement)

            self._realign_successors(periods, target_index + 1, delta, target_shift, rollback_id)
            self._save(document)
            return _view(replacement)

    def _settled_index(self, periods: list[dict[str, object]], shift: str | None) -> int | None:
        """Index of the latest live settled version, optionally for one shift."""

        wanted = validate_shift_id(shift) if shift is not None and str(shift).strip() else None
        for index in range(len(periods) - 1, -1, -1):
            period = periods[index]
            if period["status"] != "settled":
                continue
            if wanted is None or period["shift"] == wanted:
                return index
        return None

    def _realign_successors(
        self,
        periods: list[dict[str, object]],
        anchor: int,
        delta: float,
        origin_shift: str,
        rollback_id: str,
    ) -> None:
        """Carry a recomputed delta through every live period after ``anchor``.

        Settled successors cannot be silently rewritten: each is superseded by
        a recomputed version whose own charges are untouched and whose carried
        delta arrives through the new opening balance. The trailing open period
        simply inherits the new opening balance. Superseded originals already
        carry their stale closing balance as an audit record and are skipped.
        """

        if round(delta, 4) == 0.0:
            return
        expected_opening = periods[anchor]["closing_balance"]
        index = anchor + 1
        while index < len(periods):
            period = periods[index]
            if period["status"] == "superseded":
                index += 1
                continue
            if period["status"] == "open":
                period["opening_balance"] = expected_opening
                index += 1
                continue

            entries = [_entry_from_payload(item) for item in period["entries"]]
            _, _, closing, _, _ = summarize_entries(float(expected_opening), entries)
            replacement = {
                "shift": period["shift"],
                "status": "settled",
                "opening_balance": expected_opening,
                "entries": [entry.as_dict() for entry in entries],
                "closing_balance": closing,
                "rollback_of": period["settlement_id"],
                "rollback_id": rollback_id,
                "realign_from": origin_shift,
                "reason": f"realigned by rollback of {origin_shift}",
                "settled_at": _now(),
                "settlement_id": uuid.uuid4().hex,
            }
            period["status"] = "superseded"
            period["superseded_by"] = replacement["settlement_id"]
            period["rollback_id"] = rollback_id
            periods.insert(index + 1, replacement)
            expected_opening = closing
            index += 2

    # ------------------------------------------------------------- shutdowns

    def record_shutdown(self, disposition: dict[str, object]) -> dict[str, object]:
        with self._lock:
            document = self._document()
            shutdowns = document["shutdowns"]
            assert isinstance(shutdowns, list)
            entry = {
                "id": uuid.uuid4().hex,
                "time": _now(),
                "shift": disposition.get("shift", ""),
                "level": disposition.get("level", ""),
                "reason": disposition.get("reason", ""),
                "balance": disposition.get("balance", 0.0),
                "used": disposition.get("used", 0.0),
                "limit": disposition.get("limit", 0.0),
                "ratio": disposition.get("ratio", 0.0),
                "stopped": list(disposition.get("stopped", [])),
                "shed": list(disposition.get("shed", [])),
            }
            shutdowns.append(entry)
            self._save(document)
            return entry

    def shutdowns(self, shift: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            shutdowns = self._document()["shutdowns"]
            assert isinstance(shutdowns, list)
            if shift is not None and str(shift).strip():
                shift_id = validate_shift_id(shift)
                return [dict(entry) for entry in shutdowns if entry["shift"] == shift_id]
            return [dict(entry) for entry in shutdowns]

    # ------------------------------------------------------------- readings

    def balance(self, shift: str | None = None) -> PeriodView:
        """Closing balance for a shift, or the current open period."""

        with self._lock:
            if shift is not None and str(shift).strip():
                view = self.period(shift)
                if view is None:
                    raise ValueError(f"shift {shift} not found")
                return view
            open_view = self.open_period()
            if open_view is not None:
                return open_view
            periods = self.periods()
            if periods:
                return periods[-1]
            raise ValueError("no shifts recorded yet")

    def verify_chain(self) -> dict[str, object]:
        """Check every carry-over link against the balance identity."""

        with self._lock:
            issues: list[str] = []
            previous_closing: float | None = None
            previous_shift: str | None = None
            for view in self.periods():
                if view.status == "superseded":
                    continue
                if previous_closing is not None:
                    if round(view.opening_balance, 4) != round(previous_closing, 4):
                        issues.append(
                            f"{view.shift} opening {view.opening_balance:.4f} != "
                            f"{previous_shift} closing {previous_closing:.4f}"
                        )
                identity = round(view.opening_balance + view.total_adjustments - view.total_charges, 4)
                if identity != round(view.closing_balance, 4):
                    issues.append(f"{view.shift} breaks the closing balance identity")
                previous_closing = view.closing_balance
                previous_shift = view.shift
            return {"consistent": not issues, "issues": issues, "periods": len(self.periods())}

    def state(self) -> dict[str, object]:
        with self._lock:
            document = self._document()
            periods = self.periods()
            shutdowns = document["shutdowns"]
            assert isinstance(shutdowns, list)
            current = self.open_period()
            return {
                "balance_basis": "closing",
                "overlimit_policy": "shed_first",
                "config": self.config(),
                "current_shift": current.shift if current else "",
                "current_balance": current.closing_balance if current else 0.0,
                "periods": [view.as_dict() for view in periods],
                "period_count": len(periods),
                "shutdown_count": len(shutdowns),
                "chain": self.verify_chain(),
            }

    def describe(self) -> str:
        with self._lock:
            current = self.open_period()
            if current is None:
                return "settlement no open shift periods=0"
            return (
                f"settlement shift={current.shift} balance={current.closing_balance:.4f} "
                f"used={current.total_charges:.4f} periods={len(self.periods())}"
            )
