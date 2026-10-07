"""Human readable component summaries."""

from __future__ import annotations

from coldstore.ns import control_line
from coldstore.store import describe_store

from .runtime import Runtime


def collect(rt: Runtime) -> dict[str, str]:
    """Return one description line per component."""

    return {
        "pipeline": control_line().describe(),
        "store": describe_store(rt.store),
        "rooms": rt.rooms.describe(),
        "compressors": rt.compressors.describe(),
        "supply": rt.supply.describe(),
        "temp": rt.temp.describe(),
        "ammonia": rt.ammonia.describe(),
        "interlock": rt.interlock.describe(),
        "cold_fans": rt.cold_fans.describe(),
        "evap": rt.evap.describe(),
        "defrost": rt.defrost.describe(),
        "load": rt.load.describe(rt.rooms),
        "quota": rt.accumulator.describe(),
        "quota_settlement": _describe_settlement(rt),
        "quota_enforcement": (
            f"quota enforcement level={rt.enforcement.state(rt.settlement)['level']}"
        ),
        "audit": rt.auditor.describe(),
        "schedule": rt.scheduler.describe(rt.rooms),
    }


def _describe_settlement(rt: Runtime) -> str:
    view = rt.settlement.view()
    open_period = next((period for period in view.periods if period.seq == view.open_seq), None)
    if open_period is None:
        return f"quota settlement periods={len(view.periods)} open=none"
    return (
        f"quota settlement seq={open_period.seq} used={open_period.used:.4f} "
        f"balance={open_period.balance:.4f}"
    )
