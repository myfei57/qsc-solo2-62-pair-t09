"""Latched over-limit enforcement for quota shifts.

The policy thresholds live in :mod:`coldstore.quota.policy`; this module keeps
the persistent latch. Enforcement only escalates -- a WARN grows into SHED and
then STOP as usage climbs, and a recovering shift never clears the latch on
its own. An operator reset is required, and it is audited.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from . import policy
from .settlement import Settlement

LATCH_KEY = "quota:enforcement"


@dataclass(frozen=True)
class EnforcementEvent:
    """One latched escalation or an operator reset."""

    id: str
    time: str
    seq: int
    level: str
    action: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time,
            "seq": self.seq,
            "level": self.level,
            "action": self.action,
            "reason": self.reason,
        }


class Enforcement:
    """Tracks the current latched level and its escalation history."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def evaluate(self, settlement: Settlement) -> list[str]:
        """Escalate the open shift's latch to match its usage ratio.

        Returns the newly latched levels, in order, e.g. ``["SHED"]`` or
        ``["SHED", "STOP"]`` when a single posting jumps past both marks.
        """

        open_period = settlement.open()
        if open_period is None:
            return []
        target = policy.level_for_ratio(open_period.ratio)
        current = self.current_level(open_period.seq)
        new_levels = policy.escalate(current, target)
        for level in new_levels:
            self._record(open_period.seq, level, action="escalate", reason="usage ratio")
        return new_levels

    def reset(self, seq: int, reason: str = "operator reset") -> EnforcementEvent:
        """Clear an escalated latch; the shift starts again from NONE."""

        return self._record(seq, policy.LEVEL_NONE, action="reset", reason=reason)

    def current_level(self, seq: int | None = None) -> str:
        """Highest level still in force for a shift (the latest latch event)."""

        events = self.events(seq)
        for event in reversed(events):
            if event.action == "reset":
                return policy.LEVEL_NONE
            return event.level
        return policy.LEVEL_NONE

    def events(self, seq: int | None = None) ->list[EnforcementEvent]:
        events: list[EnforcementEvent] = []
        for row in self._rows():
            event_seq = int(row.get("seq", 0))
            if seq is not None and event_seq != seq:
                continue
            events.append(
                EnforcementEvent(
                    id=str(row.get("id", "")),
                    time=str(row.get("time", "")),
                    seq=event_seq,
                    level=str(row.get("level", policy.LEVEL_NONE)),
                    action=str(row.get("action", "")),
                    reason=str(row.get("reason", "")),
                )
            )
        return events

    def last_event(self) -> EnforcementEvent | None:
        events = self.events()
        return events[-1] if events else None

    def state(self, settlement: Settlement | None = None) -> dict[str, object]:
        open_period = settlement.open() if settlement else None
        seq = open_period.seq if open_period else 0
        level = self.current_level(seq) if seq else policy.LEVEL_NONE
        return {
            "seq": seq,
            "level": level,
            "ratio": open_period.ratio if open_period else 0.0,
            "balance": open_period.balance if open_period else 0.0,
            "events": [event.as_dict() for event in self.events()],
        }

    def _record(self, seq: int, level: str, action: str, reason: str) -> EnforcementEvent:
        event = {
            "id": uuid.uuid4().hex,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seq": seq,
            "level": level,
            "action": action,
            "reason": reason,
        }
        append_command(self._store, LATCH_KEY, json.dumps(event, ensure_ascii=False))
        return EnforcementEvent(
            id=str(event["id"]),
            time=str(event["time"]),
            seq=seq,
            level=level,
            action=action,
            reason=reason,
        )

    def _rows(self) -> list[dict]:
        rows: list[dict] = []
        for item in load_commands(self._store, LATCH_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
        return rows
