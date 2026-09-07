"""Pure parsing and small domain types used by the bot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta


Action = str
ALLOWED_ACTIONS = frozenset({"sleep", "shutdown"})


@dataclass(frozen=True)
class ScheduledTask:
    action: Action
    deadline: float
    owner_id: int
    reason: str = "timer"


@dataclass(frozen=True)
class Proposal:
    action: Action
    deadline: float
    owner_id: int
    reason: str
    token: str


def parse_duration(value: str) -> int | None:
    """Parse a deliberately small duration grammar into seconds."""
    compact = " ".join(value.casefold().strip().split())
    match = re.fullmatch(
        r"(?:через\s+)?([1-9][0-9]*)\s*(m|min|мин(?:ут(?:а|ы)?)?|h|час(?:а|ов)?)",
        compact,
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * (3600 if unit.startswith(("h", "час")) else 60)
    return seconds if seconds <= 7 * 24 * 3600 else None


def parse_clock(value: str, now: datetime | None = None) -> float | None:
    """Parse local HH:MM, choosing the next occurrence of that clock time."""
    match = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", value.strip())
    if not match:
        return None
    current = now or datetime.now().astimezone()
    candidate = current.replace(
        hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0
    )
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.timestamp()
