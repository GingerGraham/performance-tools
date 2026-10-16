"""Process attribution from pidstat (section 6.8).

For a window, aggregate pidstat rows per command (the first 60 characters of the
redacted command line) and rank by what the episode is about.
"""

from collections import defaultdict

TOP_N = 5
ACTIVE_DEVICE_KBPS = 100.0
MIN_CPU_PCT = 5.0          # a process under 5 % of one CPU is not worth listing


def _by_command(rows, start, end):
    """{command: {time: [rows]}} plus the sorted list of sample times in the window."""
    grouped = defaultdict(lambda: defaultdict(list))
    times = set()
    for row in rows:
        if row.time <= start or row.time > end:
            continue
        times.add(row.time)
        grouped[row.command][row.time].append(row)
    return grouped, sorted(times)


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
            "cpu_pct": _mean_per_sample(per_time, times, "cpu_pct"),
            "rd_kBps": _mean_per_sample(per_time, times, "rd_kBps"),
            "wr_kBps": _mean_per_sample(per_time, times, "wr_kBps"),
            "iodelay": _mean_per_sample(per_time, times, "iodelay"),
            "majflt_ps": _mean_per_sample(per_time, times, "majflt_ps"),
            "rss_growth_kB": _rss_growth(per_time, times),
        })

    if kind == "disk":
        for entry in entries:
            entry["io_kBps"] = entry["rd_kBps"] + entry["wr_kBps"]
        entries.sort(key=lambda e: (-e["io_kBps"], -e["iodelay"]))
        entries = [e for e in entries if e["io_kBps"] > 0][:TOP_N]
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
        for entry in entries:
            entry["metric"] = "%CPU"
        return entries

    if kind == "memory":
        entries.sort(key=lambda e: (-e["rss_growth_kB"], -e["majflt_ps"]))
        entries = [e for e in entries if e["rss_growth_kB"] > 0 or e["majflt_ps"] > 0][:TOP_N]
        for entry in entries:
            entry["metric"] = "RSS growth"
        return entries
    return []


def single_hot_process(host, start, end, minimum_pct=80.0):
    """The command averaging at least `minimum_pct` of one CPU in the window, if any."""
    entries = top_processes(host, start, end, "cpu")
    if entries and entries[0]["cpu_pct"] >= minimum_pct:
        return entries[0]
    return None
