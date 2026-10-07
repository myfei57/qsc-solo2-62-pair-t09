"""Settlement projections, dataclasses and pure arithmetic.

The balance rule is pinned on purpose: every period settles to its **closing
balance** and the closing balance is always the same arithmetic identity::

    closing = opening + adjustments - charges

A period mean is computed as well, but it is a reporting-only number. It never
feeds carry over and never feeds an over-limit decision: a mean cannot be
inverted back into a balance, so using it would break rollback alignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLOSING_BASIS = "closing"
SHED_FIRST = "shed_first"


@dataclass(frozen=True)
class PeriodEntry:
    """One immutable ledger line: a charge or a signed adjustment."""

    id: str
    seq: int
    time: str
    kind: str  # "charge" | "adjust"
    amount: float
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "seq": self.seq,
            "time": self.time,
            "kind": self.kind,
            "amount": self.amount,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PeriodView:
    """A period as an operator reads it, open or frozen."""

    shift: str
    status: str  # "open" | "settled" | "superseded"
    opening_balance: float
    total_charges: float
    total_adjustments: float
    closing_balance: float
    mean_balance: float
    mean_samples: int
    entry_count: int
    settled_at: str = ""
    settlement_id: str = ""
    superseded_by: str = ""
    entries: list[dict[str, object]] = field(default_factory=list)

    @property
    def used(self) -> float:
        """Gross runtime consumed in the period."""

        return self.total_charges

    @property
    def allowance(self) -> float:
        """Opening allowance corrected by the period's adjustments."""

        return self.opening_balance + self.total_adjustments

    def as_dict(self) -> dict[str, object]:
        return {
            "shift": self.shift,
            "status": self.status,
            "opening_balance": self.opening_balance,
            "total_charges": self.total_charges,
            "total_adjustments": self.total_adjustments,
            "used": self.used,
            "allowance": self.allowance,
            "closing_balance": self.closing_balance,
            "balance_basis": CLOSING_BASIS,
            "mean_balance": self.mean_balance,
            "mean_samples": self.mean_samples,
            "entry_count": self.entry_count,
            "settled_at": self.settled_at,
            "settlement_id": self.settlement_id,
            "superseded_by": self.superseded_by,
        }


def summarize_entries(
    opening_balance: float, entries: list[PeriodEntry]
) -> tuple[float, float, float, float, int]:
    """Return ``(charges, adjustments, closing, mean_balance, samples)``.

    The mean is the simple mean of the running balance sampled after every
    entry. It is deliberately report-only; periods without entries report the
    opening balance as the mean with zero samples.
    """

    balance = opening_balance
    charges = 0.0
    adjustments = 0.0
    sampled: list[float] = []
    for entry in entries:
        if entry.kind == "charge":
            charges += entry.amount
            balance -= entry.amount
        else:
            adjustments += entry.amount
            balance += entry.amount
        sampled.append(balance)
    mean_balance = sum(sampled) / len(sampled) if sampled else opening_balance
    return (
        round(charges, 4),
        round(adjustments, 4),
        round(balance, 4),
        round(mean_balance, 4),
        len(sampled),
    )


def validate_shift_id(shift: str) -> str:
    """Reject shift identifiers that cannot key a period."""

    value = (shift or "").strip()
    if not value:
        raise ValueError("shift id is required")
    if len(value) > 64:
        raise ValueError("shift id must be 64 characters or fewer")
    return value


def validate_ratio(name: str, value: float) -> float:
    """Validate a tier threshold sits inside the closed unit interval."""

    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def validate_limit(value: float) -> float:
    """Quota limits are positive and bounded by the supported range."""

    if value <= 0:
        raise ValueError("limit must be positive")
    if value > 1_000_000:
        raise ValueError("limit exceeds the supported range")
    return value


def validate_adjustment(amount: float, reason: str) -> float:
    """Adjustments are signed and always carry an operator reason."""

    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise ValueError("adjustment must be a number")
    if amount == 0:
        raise ValueError("adjustment must be non-zero")
    if amount < -1_000_000 or amount > 1_000_000:
        raise ValueError("adjustment exceeds the supported range")
    if not reason or not reason.strip():
        raise ValueError("adjustment reason is required")
    return float(amount)
