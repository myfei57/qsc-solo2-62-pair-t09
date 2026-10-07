"""Pure quota settlement arithmetic.

Nothing in here touches the store: every balance is a deterministic function
of the shift limit, the balance carried over from the previous shift and the
usage amounts posted during the shift. That keeps settlement replayable, which
is what lets a rolled back shift be recomputed to exactly the same numbers.

Balance policy (fixed on purpose):
    * the authoritative balance is the **period end closing balance**; it is
      the only figure carried into the next shift and used for enforcement;
    * the period average is kept as a display-only projection (mean of the
      running balance after each posting) and never feeds carryover.

Enforcement policy (shed-first):
    * WARN at 80% allowance use,
    * SHED at 100% (shed load, do not stop the machines),
    * STOP at 110% (stop whole compressor sets).
"""

from __future__ import annotations

WARN_RATIO = 0.8
SHED_RATIO = 1.0
STOP_RATIO = 1.1

LEVEL_NONE = "NONE"
LEVEL_WARN = "WARN"
LEVEL_SHED = "SHED"
LEVEL_STOP = "STOP"

LEVEL_ORDER = (LEVEL_NONE, LEVEL_WARN, LEVEL_SHED, LEVEL_STOP)

QUANTUM = 0.0001


def quantize(value: float) -> float:
    """Round money/runtime figures to the persisted four decimal quantum."""

    return round(value, 4)


def closing_balance(limit: float, carryover: float, used: float) -> float:
    """Period end balance: this shift's allowance minus what was consumed."""

    return quantize(limit + carryover - used)


def allowance(limit: float, carryover: float) -> float:
    """Opening allowance granted to a shift, including the carried balance."""

    return quantize(limit + carryover)


def average_balance(opening_allowance: float, amounts: list[float]) -> float:
    """Mean of the running balance observed after each posting.

    With no postings the balance never moves off the opening allowance. The
    result depends only on stored amounts, so it replays identically after a
    rollback and recompute.
    """

    if not amounts:
        return quantize(opening_allowance)
    running = opening_allowance
    total = 0.0
    for amount in amounts:
        running -= amount
        total += running
    return quantize(total / len(amounts))


def usage_ratio(used: float, opening_allowance: float) -> float:
    """Consumed fraction of the opening allowance.

    A non-positive allowance means the shift already starts overdrawn, so any
    consumption is treated as critically over the limit.
    """

    if opening_allowance <= 0:
        return float("inf") if used > 0 else 0.0
    return quantize(used / opening_allowance)


def level_for_ratio(ratio: float) -> str:
    """Map a consumed fraction to its latched enforcement level."""

    if ratio >= STOP_RATIO:
        return LEVEL_STOP
    if ratio >= SHED_RATIO:
        return LEVEL_SHED
    if ratio >= WARN_RATIO:
        return LEVEL_WARN
    return LEVEL_NONE


def level_rank(level: str) -> int:
    """Ordinal position of a level, unknown levels ranking below NONE."""

    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return -1


def escalate(current: str, target: str) -> list[str]:
    """Levels to latch when moving from ``current`` to ``target``.

    Levels only escalate (never auto-recover), and every intermediate level is
    reported so load is shed before any machine is stopped.
    """

    start = level_rank(current)
    end = level_rank(target)
    if end <= start:
        return []
    return list(LEVEL_ORDER[start + 1 : end + 1])
