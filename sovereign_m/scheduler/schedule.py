"""Schedules: a cron-like ``M H dom mon dow`` subset and ``every 5m`` intervals.

All times here are naive local datetimes; callers inject the clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


class ScheduleError(ValueError):
    """The schedule text is not understood."""


_MONTHS = {n: i + 1 for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DOWS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_INTERVAL = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.I)


def _value(text: str, lo: int, hi: int, names: dict[str, int] | None) -> int:
    key = text.strip().lower()
    if names and key in names:
        return names[key]
    if not key.isdigit():
        raise ScheduleError(f"bad value {text!r}")
    number = int(key)
    if not lo <= number <= hi:
        raise ScheduleError(f"{number} is outside {lo}-{hi}")
    return number


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> tuple[frozenset[int], bool]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"empty list item in {text!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"bad step in {part!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = _value(left, lo, hi, names), _value(right, lo, hi, names)
            if start > end:
                raise ScheduleError(f"backwards range {part!r}")
        else:
            start = _value(base, lo, hi, names)
            end = hi if step_text else start
        values.update(range(start, end + 1, step))
    return frozenset(values), text.startswith("*")


@dataclass(frozen=True)
class CronSchedule:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_star: bool
    dow_star: bool

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.doms
        dow_ok = (day.weekday() + 1) % 7 in self.dows
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, moment: datetime) -> datetime | None:
        """First matching minute strictly after ``moment``, or None within eight years."""
        start = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.date()
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for _ in range(366 * 8):
            if self.day_matches(day):
                for hour in hours:
                    for minute in minutes:
                        if day == start.date() and (hour, minute) < (start.hour, start.minute):
                            continue
                        return datetime.combine(day, time(hour, minute))
            day += timedelta(days=1)
        return None

    def describe(self) -> str:
        return self.text


@dataclass(frozen=True)
class IntervalSchedule:
    text: str
    seconds: int

    def describe(self) -> str:
        return self.text


Schedule = CronSchedule | IntervalSchedule


def parse_schedule(text: str) -> Schedule:
    raw = str(text).strip()
    if not raw:
        raise ScheduleError("empty schedule")
    match = _INTERVAL.match(raw)
    if match:
        seconds = int(match.group(1)) * _UNITS[match.group(2).lower()]
        if seconds < 1:
            raise ScheduleError("interval must be at least one second")
        return IntervalSchedule(raw, seconds)
    expanded = _ALIASES.get(raw.lower(), raw)
    parts = expanded.split()
    if len(parts) != 5:
        raise ScheduleError(f"expected 'M H dom mon dow' or 'every 5m', got {raw!r}")
    minutes, _ = _field(parts[0], 0, 59)
    hours, _ = _field(parts[1], 0, 23)
    doms, dom_star = _field(parts[2], 1, 31)
    months, _ = _field(parts[3], 1, 12, _MONTHS)
    dows, dow_star = _field(parts[4], 0, 7, _DOWS)
    dows = frozenset(0 if d == 7 else d for d in dows)
    return CronSchedule(raw, minutes, hours, doms, months, dows, dom_star, dow_star)


def next_run(schedule: Schedule, last_start: datetime | None, now: datetime) -> datetime | None:
    """When the job should next start.

    Interval jobs with no history are due now. Cron jobs with no history are
    due if a slot fell in the last minute. A result at or before ``now`` means due.
    """
    if isinstance(schedule, IntervalSchedule):
        if last_start is None:
            return now
        return last_start + timedelta(seconds=schedule.seconds)
    base = last_start if last_start is not None else now - timedelta(seconds=60)
    return schedule.next_after(base)


def is_due(schedule: Schedule, last_start: datetime | None, now: datetime) -> bool:
    nxt = next_run(schedule, last_start, now)
    return nxt is not None and nxt <= now
