"""Quota settlement projections and validation rules."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccumState:
    """Current accumulator value."""

    value: float

    def as_dict(self) -> dict[str, float]:
        return {"value": self.value}


@dataclass(frozen=True)
class PeriodView:
    """One shift period, settled or still open."""

    seq: int
    label: str
    limit: float
    carryover: float
    used: float
    balance: float
    average: float
    ratio: float
    state: str  # open | settled | rolled-back
    settled_at: str = ""
    settlement_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "label": self.label,
            "limit": self.limit,
            "carryover": self.carryover,
            "used": self.used,
            "balance": self.balance,
            "average": self.average,
            "ratio": self.ratio,
            "state": self.state,
            "settled_at": self.settled_at,
            "settlement_id": self.settlement_id,
        }


@dataclass(frozen=True)
class RollbackView:
    """One rollback of a previously settled shift."""

    id: str
    time: str
    seq: int
    settlement_id: str
    reason: str
    superseded_balance: float
    reverted_balance: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time,
            "seq": self.seq,
            "settlement_id": self.settlement_id,
            "reason": self.reason,
            "superseded_balance": self.superseded_balance,
            "reverted_balance": self.reverted_balance,
        }


@dataclass(frozen=True)
class LedgerState:
    """Full settlement ledger projection."""

    periods: list[PeriodView] = field(default_factory=list)
    rollbacks: list[RollbackView] = field(default_factory=list)
    open_seq: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "periods": [period.as_dict() for period in self.periods],
            "rollbacks": [rollback.as_dict() for rollback in self.rollbacks],
            "open_seq": self.open_seq,
        }


def validate_amount(amount: float) -> None:
    """Reject amounts outside the supported range."""

    if amount < 0:
        raise ValueError("amount must be non-negative")
    if amount > 1_000_000:
        raise ValueError("amount exceeds the supported range")
