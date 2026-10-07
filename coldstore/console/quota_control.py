"""Quota over-limit enforcement actions against the compressor bank.

Policy choice is shed-first: on SHED the running machines lose one load stage
each, most loaded first, and they keep running; STOP is reserved for continued
overrun and shuts machines down completely (closing their supply valves).
Actions are recorded in the runtime audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coldstore.quota import Enforcement, Settlement, policy
from coldstore.quota.enforcement import EnforcementEvent

from .runtime import Runtime

SHED_STAGES_PER_STEP = 1


@dataclass(frozen=True)
class EnforcementResult:
    """What the latest usage posting forced the line to do."""

    seq: int
    ratio: float
    level: str
    escalated: list[str]
    shed: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "ratio": self.ratio,
            "level": self.level,
            "escalated": self.escalated,
            "shed": self.shed,
            "stopped": self.stopped,
        }


def _shed_load(rt: Runtime) -> list[str]:
    """Drop one stage off every running compressor, most loaded first."""

    bank = rt.compressors
    affected: list[str] = []
    for comp_id in bank.order():
        stage, present = bank.stage(comp_id)
        if present and stage > 0:
            bank.stage_down(comp_id)
            affected.append(comp_id)
    return affected


def _stop_units(rt: Runtime) -> list[str]:
    """Stop every running compressor and close its supply valve."""

    bank = rt.compressors
    stopped: list[str] = []
    for comp_id in bank.running_ids():
        rt.supply.stop_with_supply(bank, comp_id)
        stopped.append(comp_id)
    return stopped


def apply_enforcement(
    rt: Runtime,
    settlement: Settlement | None = None,
    enforcement: Enforcement | None = None,
) -> EnforcementResult:
    """Latch the current level and execute any newly reached hard actions."""

    settlement = settlement or rt.settlement
    enforcement = enforcement or rt.enforcement
    open_period = settlement.open()

    escalated = enforcement.evaluate(settlement)
    shed: list[str] = []
    stopped: list[str] = []

    for level in escalated:
        if level == policy.LEVEL_WARN:
            rt.auditor.record("quota-warn", _audit_detail(open_period))
        elif level == policy.LEVEL_SHED:
            shed = _shed_load(rt)
            rt.auditor.record("quota-shed", f"seq={open_period.seq} units={','.join(shed) or '-'}")
        elif level == policy.LEVEL_STOP:
            stopped = _stop_units(rt)
            rt.auditor.record("quota-stop", f"seq={open_period.seq} units={','.join(stopped) or '-'}")

    level = enforcement.current_level(open_period.seq) if open_period else policy.LEVEL_NONE
    return EnforcementResult(
        seq=open_period.seq if open_period else 0,
        ratio=open_period.ratio if open_period else 0.0,
        level=level,
        escalated=escalated,
        shed=shed,
        stopped=stopped,
    )


def reset_enforcement(rt: Runtime, reason: str) -> EnforcementEvent:
    """Operator reset of the open shift's latch, recorded in the audit trail."""

    open_period = rt.settlement.open()
    seq = open_period.seq if open_period else 0
    event = rt.enforcement.reset(seq, reason=reason)
    rt.auditor.record("quota-reset", f"seq={seq} reason={reason}")
    return event


def _audit_detail(open_period: object) -> str:
    return (
        f"seq={open_period.seq} ratio={open_period.ratio:.4f} "  # type: ignore[attr-defined]
        f"balance={open_period.balance:.4f}"  # type: ignore[attr-defined]
    )
