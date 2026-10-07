"""Graduated over-limit enforcement.

Policy is pinned to **shed-first**: a refrigeration line that trips hard loses
product, so the cheapest response that restores headroom is taken first. The
three tiers are fixed; the warn/shed trigger ratios are configurable:

* ``warn``  - usage is past the warning ratio. Nothing is touched.
* ``shed``  - stage down the most loaded running compressor (one stage). A
  machine shed to zero is stopped and its supply valve is closed.
* ``hard``  - the balance is exhausted (including an overdrawn carry over).
  Every running compressor is stopped and its supply valve closed.

Every shed/hard disposition is registered in the settlement ledger with its
reason and on the runtime audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coldstore.comp import Bank
from coldstore.valve import SupplyValve

from .report import PeriodView
from .settlement import HARD_RATIO, Settlement

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_SHED = "shed"
LEVEL_HARD = "hard"


@dataclass(frozen=True)
class Disposition:
    """What the over-limit policy decided and what it did."""

    level: str
    reason: str
    shift: str
    balance: float
    used: float
    allowance: float
    limit: float
    ratio: float
    shed: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        return self.level in (LEVEL_SHED, LEVEL_HARD)

    def as_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "reason": self.reason,
            "shift": self.shift,
            "balance": self.balance,
            "used": self.used,
            "allowance": self.allowance,
            "limit": self.limit,
            "ratio": round(self.ratio, 4),
            "shed": self.shed,
            "stopped": self.stopped,
            "acted": self.acted,
        }


def level_for(used: float, allowance: float, warn_ratio: float, shed_ratio: float) -> tuple[str, float]:
    """Classify usage into a tier, returning ``(level, ratio)``.

    The ratio is usage over the opening allowance plus this period's
    adjustments. A non-positive allowance (an overdrawn shift handed over) is
    already at the hard tier regardless of new usage.
    """

    if allowance <= 0:
        return LEVEL_HARD, HARD_RATIO
    ratio = used / allowance if allowance > 0 else HARD_RATIO
    if ratio >= HARD_RATIO:
        return LEVEL_HARD, ratio
    if ratio >= shed_ratio:
        return LEVEL_SHED, ratio
    if ratio >= warn_ratio:
        return LEVEL_WARN, ratio
    return LEVEL_OK, ratio


def enforce(
    settlement: Settlement,
    bank: Bank,
    supply: SupplyValve,
    view: PeriodView | None = None,
) -> Disposition:
    """Apply the shed-first policy to the current (or given) period."""

    if view is None:
        view = settlement.balance()
    config = settlement.config()
    level, ratio = level_for(
        view.total_charges,
        view.allowance,
        config["warn_ratio"],
        config["shed_ratio"],
    )

    shed: list[str] = []
    stopped: list[str] = []
    reason = ""

    if level == LEVEL_HARD:
        reason = (
            f"quota exhausted shift={view.shift} used={view.total_charges:.4f} "
            f"allowance={view.allowance:.4f}"
        )
        for comp_id in bank.running_ids():
            supply.stop_with_supply(bank, comp_id)
            stopped.append(comp_id)
    elif level == LEVEL_SHED:
        reason = (
            f"quota shed tier shift={view.shift} ratio={ratio:.4f} "
            f"used={view.total_charges:.4f} allowance={view.allowance:.4f}"
        )
        # Highest stage first: bank.order sorts running machines by load.
        for comp_id in bank.order():
            stage, present = bank.stage(comp_id)
            if present and stage > 0:
                bank.stage_down(comp_id)
                shed.append(comp_id)
                if bank.stage(comp_id)[0] == 0:
                    supply.stop_with_supply(bank, comp_id)
                    stopped.append(comp_id)
                break
    elif level == LEVEL_WARN:
        reason = f"quota warn tier shift={view.shift} ratio={ratio:.4f}"

    disposition = Disposition(
        level=level,
        reason=reason,
        shift=view.shift,
        balance=view.closing_balance,
        used=view.total_charges,
        allowance=view.allowance,
        limit=config["limit"],
        ratio=ratio,
        shed=shed,
        stopped=stopped,
    )
    if level in (LEVEL_SHED, LEVEL_HARD):
        settlement.record_shutdown(disposition.as_dict())
    return disposition
