"""Time zones and time formatting.

All data is held as epoch seconds. A zone is only used to display times and to
read user-supplied --from / --to values.
"""

import datetime
import re

UTC = datetime.timezone.utc

_OFFSET_PATTERN = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?$")


class TimeError(ValueError):
    """A time or zone value could not be understood."""


def offset_zone(text, name=None):
    """Zone for '+0100', '+01:00', '-0530' or 'UTC+1'. `name` labels it."""
    stripped = text.strip()
    if stripped.upper() in ("UTC", "GMT", "Z"):
        return datetime.timezone.utc if name is None else datetime.timezone(datetime.timedelta(0), name)
    match = _OFFSET_PATTERN.match(stripped)
    if not match:
        raise TimeError("cannot read time zone '%s': use UTC or an offset such as +01:00" % text)
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    delta = datetime.timedelta(hours=hours, minutes=minutes) * sign
    if name:
        return datetime.timezone(delta, name)
    return datetime.timezone(delta)


def zone_from_manifest(label):
    """Zone from the manifest 'timezone' value, which is `date +'%Z %z'`: 'BST +0100'."""
    parts = (label or "").split()
    if len(parts) == 2:
        try:
            return offset_zone(parts[1], parts[0])
        except TimeError:
            return UTC
    if len(parts) == 1:
        try:
            return offset_zone(parts[0])
        except TimeError:
            return UTC
    return UTC


def zone_label(zone):
    """'BST +0100' style label for axis titles and report text."""
    now = datetime.datetime(2000, 1, 1, tzinfo=zone)
    offset = zone.utcoffset(now)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    name = zone.tzname(now) or "UTC"
    if name.startswith("UTC") and total_minutes == 0:
        return "UTC"
    return "%s %s%02d%02d" % (name, sign, total_minutes // 60, total_minutes % 60)


def zone_short(zone):
    """Just the zone name, e.g. 'BST' (used inline after a clock time)."""
    label = zone_label(zone)
    return label.split()[0]


def to_datetime(epoch, zone):
    return datetime.datetime.fromtimestamp(epoch, zone)


def clock(epoch, zone):
    """HH:MM"""
    return to_datetime(epoch, zone).strftime("%H:%M")


def clock_seconds(epoch, zone):
    return to_datetime(epoch, zone).strftime("%H:%M:%S")


def stamp(epoch, zone):
    """YYYY-mm-dd HH:MM:SS"""
    return to_datetime(epoch, zone).strftime("%Y-%m-%d %H:%M:%S")


def stamp_minutes(epoch, zone):
    return to_datetime(epoch, zone).strftime("%Y-%m-%d %H:%M")


MULTI_DAY_SPAN_S = 20 * 3600


def range_text(zone, start, end, span_start=None, span_end=None):
    """(first, last) display texts for a time range inside the analysed span.

    Clock times when the span is a day or less and the range stays on one day.
    Otherwise the first carries its date, and the last does too unless it is the same day.
    """
    first_day = to_datetime(start, zone).date()
    last_day = to_datetime(end, zone).date()
    long_span = span_start is not None and span_end is not None and (span_end - span_start) > MULTI_DAY_SPAN_S
    if not long_span and first_day == last_day:
        return clock(start, zone), clock(end, zone)
    last = clock(end, zone) if first_day == last_day else stamp_minutes(end, zone)
    return stamp_minutes(start, zone), last


def parse_iso(text):
    """ISO 8601 with an offset (`date -Iseconds`, psar ts) to epoch seconds."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # Python 3.9 fromisoformat needs a colon in the offset: +0100 -> +01:00
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise TimeError("timestamp '%s' has no UTC offset" % text)
    return int(parsed.timestamp())


def parse_when(text, zone, anchor_epoch):
    """--from / --to value to epoch.

    Accepts epoch seconds, 'YYYY-mm-dd HH:MM[:SS]' and 'HH:MM[:SS]' in `zone`.
    A bare clock time uses the date of `anchor_epoch` (the data's first sample).
    """
    value = text.strip()
    if re.fullmatch(r"\d{9,11}", value):
        return int(value)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.datetime.strptime(value, pattern)
            return int(parsed.replace(tzinfo=zone).timestamp())
        except ValueError:
            pass
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            clock_time = datetime.datetime.strptime(value, pattern).time()
        except ValueError:
            continue
        anchor = to_datetime(anchor_epoch, zone)
        combined = datetime.datetime.combine(anchor.date(), clock_time, tzinfo=zone)
        return int(combined.timestamp())
    raise TimeError("cannot read time '%s': use HH:MM, 'YYYY-mm-dd HH:MM' or epoch seconds" % text)


def duration_text(seconds):
    """'29 min', '45 s', '2 h 5 min'."""
    seconds = int(round(seconds))
    if seconds < 90:
        return "%d s" % seconds
    minutes = int(round(seconds / 60.0))
    if minutes < 120:
        return "%d min" % minutes
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return "%d h" % hours
    return "%d h %d min" % (hours, rest)


def parse_duration(text):
    """'10m', '2h', '3d', '45' (seconds) to seconds."""
    value = text.strip().lower()
    if re.fullmatch(r"\d+", value):
        return int(value)
    match = re.fullmatch(r"(\d+)\s*(s|m|h|d)", value)
    if not match:
        raise TimeError("cannot read duration '%s': use 30s, 10m, 2h or 1d" % text)
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(match.group(1)) * factor
