"""Process attribution from pidstat (section 6.8).

For a window, aggregate pidstat rows per program (see process_key: PostgreSQL
backends that retitle themselves, or one interpreter running many scripts, would
otherwise split into hundreds of entries) and rank by what the episode is about.
Thread rows (pidstat -t) repeat their process's totals and are not counted here.
"""

import re
from collections import defaultdict

TOP_N = 5
ACTIVE_DEVICE_KBPS = 100.0
MIN_CPU_PCT = 5.0          # a process under 5 % of one CPU is not worth listing

INTERPRETER = re.compile(r"^(python[0-9.]*|perl|ruby|node|java|php[0-9.]*|bash|sh|dash)$")
JAVA_SKIP_NEXT = ("-cp", "-classpath", "--class-path")


def process_key(command):
    """Program a command line belongs to: 'postgres', 'python3 app.py', 'kworker'."""
    text = command.strip()
    if text.startswith("|__"):
        text = text[3:]
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    tokens = text.split()
    if not tokens:
        return text
    first = tokens[0].rstrip(":")
    if first.startswith(("/", "./", "../")):
        name = first.rsplit("/", 1)[-1]
    elif "/" in first:
        name = first.split("/", 1)[0]            # kernel thread: kworker/0:1H-kblockd
    else:
        name = first
    if not INTERPRETER.match(name):
        return name
    skip_next = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in JAVA_SKIP_NEXT:
            skip_next = True
            continue
        if token == "-jar":
            continue
        if token.startswith("-"):
            continue
        return "%s %s" % (name, token.rsplit("/", 1)[-1])
    return name


def _by_command(rows, start, end):
    """{program key: {time: [rows]}} plus the sorted list of sample times in the window."""
    grouped = defaultdict(lambda: defaultdict(list))
    times = set()
    for row in rows:
        if row.is_thread or row.time <= start or row.time > end:
            continue
        times.add(row.time)
        grouped[process_key(row.command)][row.time].append(row)
    return grouped, sorted(times)


def _largest_member(per_time, times, attribute_of):
    """(full command line, value) of the command line with the highest mean value in the group."""
    by_command = defaultdict(lambda: defaultdict(list))
    for time_value, rows in per_time.items():
        for row in rows:
            by_command[row.command][time_value].append(row)
    best = ("", float("-inf"))
    for command, members in by_command.items():
        value = attribute_of(members)
        if value > best[1]:
            best = (command, value)
    return best


def _mean_per_sample(per_time, times, attribute):
    """Mean of the per-sample sum over all samples in the window (absent = 0)."""
    total = 0.0
    for time_value in times:
        for row in per_time.get(time_value, ()):
            value = getattr(row, attribute)
            if value == value:          # NaN check
                total += value
    return total / len(times) if times else 0.0


def _rss_growth(per_time, times):
    totals = []
    for time_value in times:
        rows = per_time.get(time_value)
        if rows:
            totals.append(sum(row.rss_kb for row in rows if row.rss_kb == row.rss_kb))
    if len(totals) < 2:
        return 0.0
    return totals[-1] - totals[0]


def active_physical_devices(host, start, end):
    """Names of non-stacked devices moving at least ACTIVE_DEVICE_KBPS in the window."""
    names = []
    for device in host.devices("disk.read_kBps"):
        disk = host.hardware.find_disk(device) if host.hardware is not None else None
        if disk is not None and disk.media_class == "stacked":
            continue
        read = host.get("disk.read_kBps", device)
        write = host.get("disk.write_kBps", device)
        if read is None or write is None:
            continue
        window = [r + w for t, r, w in zip(read.times, read.values, write.values) if start < t <= end]
        if window and sum(window) / len(window) >= ACTIVE_DEVICE_KBPS:
            names.append(device)
    return names


def device_throughput(host, device, start, end):
    """(mean read kB/s, mean write kB/s) of a device in the window, or None."""
    read = host.get("disk.read_kBps", device)
    write = host.get("disk.write_kBps", device)
    if read is None or write is None:
        return None
    reads = [v for t, v in zip(read.times, read.values) if start < t <= end]
    writes = [v for t, v in zip(write.times, write.values) if start < t <= end]
    if not reads:
        return None
    return sum(reads) / len(reads), sum(writes) / len(writes)


def _add_examples(entries, times, kind):
    """example: the full command line inside each group with the highest ranking value."""
    for entry in entries:
        per_time = entry.pop("_per_time")
        if kind == "disk":
            measure = lambda members: _mean_per_sample(members, times, "rd_kBps") + _mean_per_sample(members, times, "wr_kBps")
        elif kind == "cpu":
            measure = lambda members: _mean_per_sample(members, times, "cpu_pct")
        else:
            measure = lambda members: _rss_growth(members, times)
        entry["example"], entry["example_value"] = _largest_member(per_time, times, measure)


def top_processes(host, start, end, kind, device=None):
    """Top commands in [start, end]. kind: 'disk', 'cpu' or 'memory'. [] without pidstat data."""
    if not host.process_rows:
        return []
    grouped, times = _by_command(host.process_rows, start, end)
    if not times:
        return []
    entries = []
    for command, per_time in grouped.items():
        entries.append({
            "command": command,
            "processes": len({row.pid for rows in per_time.values() for row in rows}),
            "cpu_pct": _mean_per_sample(per_time, times, "cpu_pct"),
            "rd_kBps": _mean_per_sample(per_time, times, "rd_kBps"),
            "wr_kBps": _mean_per_sample(per_time, times, "wr_kBps"),
            "iodelay": _mean_per_sample(per_time, times, "iodelay"),
            "majflt_ps": _mean_per_sample(per_time, times, "majflt_ps"),
            "rss_growth_kB": _rss_growth(per_time, times),
            "_per_time": per_time,
        })

    if kind == "disk":
        for entry in entries:
            entry["io_kBps"] = entry["rd_kBps"] + entry["wr_kBps"]
        entries.sort(key=lambda e: (-e["io_kBps"], -e["iodelay"]))
        entries = [e for e in entries if e["io_kBps"] > 0][:TOP_N]
        _add_examples(entries, times, kind)
        total_process_io = sum(e["io_kBps"] for e in entries) or 0.0
        active = active_physical_devices(host, start, end)
        stats = device_throughput(host, device, start, end) if device else None
        for entry in entries:
            entry["metric"] = "kB_rd/s + kB_wr/s"
            if stats and len(active) <= 1 and (stats[0] + stats[1]) > 0:
                writes_dominate = entry["wr_kBps"] >= entry["rd_kBps"]
                denominator = stats[1] if writes_dominate and stats[1] > 0 else stats[0] + stats[1]
                numerator = entry["wr_kBps"] if writes_dominate and stats[1] > 0 else entry["io_kBps"]
                entry["share_pct"] = min(100.0, 100.0 * numerator / denominator)
                entry["share_basis"] = "of device writes" if denominator == stats[1] and writes_dominate else "of device IO"
            else:
                entry["share_pct"] = 100.0 * entry["io_kBps"] / total_process_io if total_process_io else 0.0
                entry["share_basis"] = "of total process IO"
        return entries

    if kind == "cpu":
        entries.sort(key=lambda e: -e["cpu_pct"])
        entries = [e for e in entries if e["cpu_pct"] >= MIN_CPU_PCT][:TOP_N]
        _add_examples(entries, times, kind)
        for entry in entries:
            entry["metric"] = "%CPU"
        return entries

    if kind == "memory":
        entries.sort(key=lambda e: (-e["rss_growth_kB"], -e["majflt_ps"]))
        entries = [e for e in entries if e["rss_growth_kB"] > 0 or e["majflt_ps"] > 0][:TOP_N]
        _add_examples(entries, times, kind)
        for entry in entries:
            entry["metric"] = "RSS growth"
        return entries
    return []


def single_hot_process(host, start, end, minimum_pct=80.0):
    """The process, or with thread rows the thread, averaging at least `minimum_pct` of one CPU.

    Thread rows are used when the log has any in the window: one busy thread is the
    point of a single-threaded bottleneck, and a process row would hide which one.
    """
    thread_rows = [row for row in host.process_rows if row.is_thread and start < row.time <= end]
    if thread_rows:
        times = sorted({row.time for row in thread_rows})
        grouped = defaultdict(lambda: defaultdict(list))
        for row in thread_rows:
            grouped[(row.tgid, row.command)][row.time].append(row)
        best = None
        for (tgid, command), per_time in grouped.items():
            value = _mean_per_sample(per_time, times, "cpu_pct")
            if best is None or value > best["cpu_pct"]:
                best = {"command": "thread %s (TGID %d)" % (command, tgid), "cpu_pct": value, "thread": True}
        if best is not None and best["cpu_pct"] >= minimum_pct:
            return best
        return None
    entries = top_processes(host, start, end, "cpu")
    if entries and entries[0]["cpu_pct"] >= minimum_pct:
        return entries[0]
    return None
