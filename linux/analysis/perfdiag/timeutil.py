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


def _fixed_zone_from_label(label):
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


def resolve_zone(label, tz_name=""):
    """(zone, note) from the manifest 'timezone' (`date +'%Z %z'`: 'BST +0100') and 'tz_name'.

    With an IANA name (Europe/London) the zone follows daylight-saving changes. Without one,
    or when this machine cannot find it, the zone is the fixed offset and the note says why.
    """
    fixed = _fixed_zone_from_label(label)
    name = (tz_name or "").strip()
    if not name:
        return fixed, ""
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name), ""
    except (ImportError, ValueError, KeyError, OSError) as error:
        # ZoneInfoNotFoundError is a KeyError subclass.
        offset = (label or "").split()[-1] if (label or "").split() else "+0000"
        return fixed, ("time zone %s is not in this machine's time zone database; using the fixed offset %s "
                       "(pip install tzdata)" % (name, offset))


def zone_from_manifest(label, tz_name=""):
    """Zone from the manifest 'timezone' value, or from 'tz_name' when this machine knows it."""
    return resolve_zone(label, tz_name)[0]


def is_named_zone(zone):
    return hasattr(zone, "key")


def zone_label(zone, at_epoch=None):
    """'BST +0100' style label for report text: the zone name and offset at `at_epoch` (default now)."""
    if at_epoch is None and not is_named_zone(zone):
        at_epoch = 946684800                      # a fixed zone has one answer; use a stable date
    if at_epoch is None:
        moment = datetime.datetime.now(zone)
    else:
        moment = datetime.datetime.fromtimestamp(at_epoch, zone)
    offset = moment.utcoffset()
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    name = moment.tzname() or "UTC"
    if name.startswith("UTC") and total_minutes == 0:
        return "UTC"
    return "%s %s%02d%02d" % (name, sign, total_minutes // 60, total_minutes % 60)


def zone_axis_label(zone, at_epoch=None):
    """Label for a chart axis: the IANA name for a named zone, else the fixed-offset label."""
    if is_named_zone(zone):
        return zone.key
    return zone_label(zone, at_epoch)


def zone_short(zone, at_epoch=None):
    """Just the zone name at that time, e.g. 'BST' (used inline after a clock time)."""
    return zone_label(zone, at_epoch).split()[0]


def clock_zone(epoch, zone):
    """'01:30 BST': a clock time with the zone abbreviation in force at that time."""
    return "%s %s" % (clock(epoch, zone), zone_short(zone, epoch))


def dst_change_date(zone, start, end):
    """Local date (YYYY-mm-dd) of the first UTC-offset change in [start, end], or None."""
    if not is_named_zone(zone) or start is None or end is None or end <= start:
        return None

    def offset_at(epoch):
        return datetime.datetime.fromtimestamp(epoch, zone).utcoffset()

    if offset_at(start) == offset_at(end):
        # Two changes inside one window (spring and autumn) cancel out; look hour by hour.
        step = 3600
        moment = start
        while moment < end and offset_at(moment) == offset_at(start):
            moment += step
        if moment >= end:
            return None
        return datetime.datetime.fromtimestamp(moment, zone).strftime("%Y-%m-%d")
    low, high = start, end
    while high - low > 60:
        middle = (low + high) // 2
        if offset_at(middle) == offset_at(low):
            low = middle
        else:
            high = middle
    return datetime.datetime.fromtimestamp(high, zone).strftime("%Y-%m-%d")


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
