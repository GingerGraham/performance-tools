"""Reader for `pidstat -h -l` logs (collect-os-metrics.sh).

Header lines start with '#'. Data lines start with an integer: epoch seconds
when pidstat ran with -H, otherwise HH:MM:SS. The first N whitespace-separated
fields are fixed numeric columns, where N is the header's column count minus
one; the rest of the line is the command. No 'Average:' block is printed with -h.
"""

import math
import re

from ..model import ProcessRow

COMMAND_WIDTH = 60
_DATE_LINE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _number(text):
    try:
        return float(text)
    except ValueError:
        return math.nan


def _column(names, *candidates):
    for candidate in candidates:
        if candidate in names:
            return names.index(candidate)
    return None


def _clock_seconds(text):
    parts = text.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def parse_pidstat_text(text, zone=None):
    """List of ProcessRow. `zone` is only needed for logs written without -H."""
    import datetime

    rows = []
    names = None
    fixed = 0
    columns = {}
    base_date = None
    previous_clock = None
    day_offset = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            header = line[1:].split()
            if not header or header[0] != "Time":
                continue
            names = header
            fixed = len(names) - 1
            columns = {
                "pid": _column(names, "PID", "TID"),
                "cpu": _column(names, "%CPU"),
                "rss": _column(names, "RSS"),
                "majflt": _column(names, "majflt/s"),
                "rd": _column(names, "kB_rd/s"),
                "wr": _column(names, "kB_wr/s"),
                "iodelay": _column(names, "iodelay"),
            }
            continue
        if names is None:
            if base_date is None:
                match = _DATE_LINE.search(line)
                if match:
                    base_date = datetime.date.fromisoformat(match.group(1))
            continue
        first = line.split(None, 1)[0]
        if first.isdigit() and len(first) > 6:
            epoch = int(first)
        elif _clock_seconds(first) is not None and base_date is not None and zone is not None:
            seconds = _clock_seconds(first)
            if previous_clock is not None and seconds < previous_clock:
                day_offset += 1
            previous_clock = seconds
            midnight = datetime.datetime.combine(base_date, datetime.time(0), tzinfo=zone)
            epoch = int(midnight.timestamp()) + day_offset * 86400 + seconds
        else:
            continue
        parts = line.split(None, fixed)
        if len(parts) <= fixed:
            continue
        command = parts[fixed].strip()
        values = parts[:fixed]

        def fetch(key):
            index = columns.get(key)
            if index is None or index >= len(values):
                return math.nan
            return _number(values[index])

        pid_index = columns.get("pid")
        pid_text = values[pid_index] if pid_index is not None and pid_index < len(values) else ""
        pid = int(pid_text) if pid_text.isdigit() else 0
        rows.append(ProcessRow(
            time=epoch, pid=pid, cpu_pct=fetch("cpu"), rss_kb=fetch("rss"), majflt_ps=fetch("majflt"),
            rd_kBps=fetch("rd"), wr_kBps=fetch("wr"), iodelay=fetch("iodelay"),
            command=command[:COMMAND_WIDTH],
        ))
    return rows


def read_pidstat_file(path, zone=None):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_pidstat_text(handle.read(), zone)
