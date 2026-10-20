"""Column-name mapping from sadf and psar exports to canonical metric names.

Rules are written once against the canonical names. The sar variant is detected
from the header columns, never from the version string:

* sysstat < 11.5.7 reports sectors: rd_sec/s, wr_sec/s, avgrq-sz, avgqu-sz.
  Kilobytes are sectors / 2.
* svctm was removed in 12.1.2 and is ignored when present.
* kbavail needs 11.5.3; older files get MemFree + Buffers + Cached, with a note.
"""

import math
from collections import defaultdict

from .model import Series

# canonical metric, candidate columns in preference order as (column, scale)
SADF_COLUMNS = {
    "cpu": [
        ("cpu.idle_pct", [("%idle", 1.0)]),
        ("cpu.steal_pct", [("%steal", 1.0)]),
        ("cpu.iowait_pct", [("%iowait", 1.0)]),
        ("cpu.soft_pct", [("%soft", 1.0)]),
        ("cpu.usr_pct", [("%usr", 1.0)]),
        ("cpu.sys_pct", [("%sys", 1.0)]),
    ],
    "load": [
        ("load.runq", [("runq-sz", 1.0)]),
        ("load.blocked", [("blocked", 1.0)]),
        ("load.avg1", [("ldavg-1", 1.0)]),
        ("load.avg5", [("ldavg-5", 1.0)]),
    ],
    "cswch": [("cpu.cswch_ps", [("cswch/s", 1.0)])],
    "paging": [
        ("paging.majflt_ps", [("majflt/s", 1.0)]),
        ("paging.scan_direct_ps", [("pgscand/s", 1.0)]),
        ("paging.scan_kswapd_ps", [("pgscank/s", 1.0)]),
    ],
    "swap_rate": [
        ("swap.in_pps", [("pswpin/s", 1.0)]),
        ("swap.out_pps", [("pswpout/s", 1.0)]),
    ],
    "swap_space": [("swap.used_pct", [("%swpused", 1.0)])],
    "disk": [
        ("disk.tps", [("tps", 1.0)]),
        ("disk.read_kBps", [("rkB/s", 1.0), ("rd_sec/s", 0.5)]),
        ("disk.write_kBps", [("wkB/s", 1.0), ("wr_sec/s", 0.5)]),
        ("disk.req_kB", [("areq-sz", 1.0), ("avgrq-sz", 0.5)]),
        ("disk.queue", [("aqu-sz", 1.0), ("avgqu-sz", 1.0)]),
        ("disk.await_ms", [("await", 1.0)]),
        ("disk.util_pct", [("%util", 1.0)]),
    ],
    "net_dev": [
        ("net.rx_kBps", [("rxkB/s", 1.0)]),
        ("net.tx_kBps", [("txkB/s", 1.0)]),
    ],
    "net_edev": [
        ("net.rxdrop_ps", [("rxdrop/s", 1.0)]),
        ("net.txdrop_ps", [("txdrop/s", 1.0)]),
    ],
    "net_soft": [
        ("softnet.dropped_ps", [("dropd/s", 1.0)]),
        ("softnet.squeezed_ps", [("squeezd/s", 1.0)]),
    ],
    "psi_cpu": [("psi.cpu_some", [("%scpu", 1.0)])],
    "psi_io": [("psi.io_some", [("%sio", 1.0)]), ("psi.io_full", [("%fio", 1.0)])],
    "psi_mem": [("psi.mem_some", [("%smem", 1.0)]), ("psi.mem_full", [("%fmem", 1.0)])],
    "memory": [
        ("mem.dirty_kB", [("kbdirty", 1.0)]),
        ("mem.commit_pct", [("%commit", 1.0)]),
        ("mem.free_kB", [("kbmemfree", 1.0)]),
    ],
}

# psar column -> canonical, per file (docs/psar.md, columns after ts and key)
PSAR_COLUMNS = {
    "cpu": [
        ("cpu.idle_pct", "idle_pct"), ("cpu.steal_pct", "steal_pct"), ("cpu.iowait_pct", "iowait_pct"),
        ("cpu.soft_pct", "soft_pct"), ("cpu.usr_pct", "usr_pct"), ("cpu.sys_pct", "sys_pct"),
        ("load.runq", "run"), ("load.blocked", "blkd"), ("cpu.cswch_ps", "cs_s"),
    ],
    "mem": [
        ("mem.avail_kB", "availkB"), ("mem.dirty_kB", "dirtykB"),
        ("swap.in_pps", "swpin_s"), ("swap.out_pps", "swpout_s"), ("paging.majflt_ps", "majflt_s"),
    ],
    "disk": [
        ("disk.read_kBps", "rkB_s"), ("disk.write_kBps", "wkB_s"),
        ("disk.queue", "aqu-sz"), ("disk.util_pct", "util_pct"),
        ("disk.r_await_ms", "r_await"), ("disk.w_await_ms", "w_await"),
        ("disk.r_req_kB", "rareq-sz"), ("disk.w_req_kB", "wareq-sz"),
        ("disk.r_s", "r_s"), ("disk.w_s", "w_s"),
    ],
    "net": [
        ("net.rx_kBps", "rxkB_s"), ("net.tx_kBps", "txkB_s"),
        ("net.rxdrop_ps", "rxdrop_s"), ("net.txdrop_ps", "txdrop_s"),
    ],
    "load": [("load.avg1", "1m"), ("load.avg5", "5m")],
}

HOST_WIDE_METRICS = ("load.runq", "load.blocked", "cpu.cswch_ps")
INTERFACE_SKIP = ("lo",)        # the loopback interface says nothing about the network


def _median_int(values, default):
    cleaned = sorted(int(round(v)) for v in values if not math.isnan(v) and v > 0)
    if not cleaned:
        return default
    return cleaned[len(cleaned) // 2]


def _pick(block, candidates):
    for column, scale in candidates:
        index = block.column_index(column)
        if index is not None:
            return index, scale
    return None, 1.0


def series_from_sadf(tables, manifest_interval, host):
    """Series list from {file stem: [SadfBlock]}. Adds notes to `host` for variants."""
    collected = defaultdict(lambda: ([], []))
    intervals = []
    units = {}

    def add(metric, device, epoch, value):
        times, values = collected[(metric, device)]
        times.append(epoch)
        values.append(value)

    for stem, mappings in SADF_COLUMNS.items():
        blocks = tables.get(stem)
        if not blocks:
            continue
        for block in blocks:
            picks = []
            for metric, candidates in mappings:
                index, scale = _pick(block, candidates)
                if index is not None:
                    picks.append((metric, index, scale))
                    if scale != 1.0:
                        units[metric] = "sectors"
            for epoch, interval, key, values in block.rows:
                intervals.append(interval)
                if block.key_name == "IFACE" and key in INTERFACE_SKIP:
                    continue
                for metric, index, scale in picks:
                    if index < len(values):
                        add(metric, key if block.key_name else "", epoch, values[index] * scale)

    if "sectors" in units.values():
        host.note("sysstat reports disk columns in sectors (before 11.5.7); converted to kB as sectors / 2")

    # Available memory: kbavail, or the older approximation with a note.
    for block in tables.get("memory", []):
        avail = block.column_index("kbavail")
        if avail is not None:
            for epoch, _, _, values in block.rows:
                add("mem.avail_kB", "", epoch, values[avail])
        else:
            free = block.column_index("kbmemfree")
            buffers = block.column_index("kbbuffers")
            cached = block.column_index("kbcached")
            if None not in (free, buffers, cached):
                host.note("sysstat before 11.5.3 has no kbavail; available memory is MemFree + Buffers + Cached")
                for epoch, _, _, values in block.rows:
                    add("mem.avail_kB", "", epoch, values[free] + values[buffers] + values[cached])

    interval = manifest_interval or _median_int(intervals, 0)
    result = []
    for (metric, device), (times, values) in collected.items():
        ordered = _sorted_unique(times, values)
        result.append(Series(metric, device, ordered[0], ordered[1], interval, "sar"))
    return result


def series_from_psar(tables, manifest_interval):
    collected = defaultdict(lambda: ([], []))
    for stem, mappings in PSAR_COLUMNS.items():
        table = tables.get(stem)
        if table is None:
            continue
        columns, rows = table
        present = [(metric, column) for metric, column in mappings if column in columns]
        for epoch, key, values in rows:
            if stem in ("disk", "net"):
                device = key
            elif stem == "cpu":
                device = "all"
            else:
                device = ""
            if stem == "net" and device in INTERFACE_SKIP:
                continue
            for metric, column in present:
                # Run queue, blocked tasks and context switches are host-wide, so they are
                # keyed "" exactly like their sadf counterparts. Only the CPU percentages
                # are keyed "all" (sadf's CPU -1).
                series_device = "" if metric in HOST_WIDE_METRICS else device
                times, vals = collected[(metric, series_device)]
                times.append(epoch)
                vals.append(values[column])
            if stem == "disk":
                # tps from the two request rates
                if "r_s" in columns and "w_s" in columns:
                    times, vals = collected[("disk.tps", device)]
                    times.append(epoch)
                    vals.append(values["r_s"] + values["w_s"])
    result = []
    interval = manifest_interval or 0
    for (metric, device), (times, values) in collected.items():
        ordered = _sorted_unique(times, values)
        result.append(Series(metric, device, ordered[0], ordered[1], interval, "psar"))
    return result


def _sorted_unique(times, values):
    """Sort by time; if a time repeats the last value wins."""
    merged = {}
    for time_value, value in zip(times, values):
        merged[time_value] = value
    ordered_times = sorted(merged)
    return ordered_times, [merged[t] for t in ordered_times]
