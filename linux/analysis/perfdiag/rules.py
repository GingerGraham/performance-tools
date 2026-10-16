"""Indicator rules (section 6.5): which series are tested, against which thresholds.

Each builder turns host series into Indicator objects. An Indicator is a
(possibly derived) series with warn/crit thresholds and a direction; episodes
are detected on it by episodes.detect_episodes.

Principle 3 of the method: a load indicator without a latency indicator is not
a problem. Load-type indicators (throughput, IOPS, %util on non-HDD media) are
therefore either absent here or marked supporting.
"""

import math
import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from .thresholds import resolve_disk_profile

PRIMARY = "primary"
SUPPORTING = "supporting"
INFO = "info"

DISK_RULES = ("disk_await", "disk_r_await", "disk_w_await", "disk_queue", "disk_util",
              "disk_throughput", "disk_read_kBps", "disk_write_kBps", "disk_iops",
              "disk_plateau", "stack_overhead")
HDD_CLASSES = ("hdd", "hw-raid")


@dataclass
class Indicator:
    name: str
    device: str
    title: str
    unit: str
    warn: float
    crit: float
    direction: str
    times: List[int]
    values: List[float]
    interval_s: int
    role: str = PRIMARY
    threshold_source: str = "default"
    crit_values: Optional[List[float]] = None
    labels: List[str] = field(default_factory=list)
    metric: str = ""                      # canonical metric charts should draw, when it differs

    @property
    def key(self):
        return (self.name, self.device)

    def points(self):
        return list(zip(self.times, self.values))


class Context:
    """What the builders need besides the series."""

    def __init__(self, host, settings):
        self.host = host
        self.settings = settings
        self.hardware = host.hardware
        self.cpus = host.cpus
        self.mem_total_kb = host.mem_total_kb
        self.notes = []
        self.profiles = {}

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def profile(self, device):
        if device not in self.profiles:
            self.profiles[device] = resolve_disk_profile(device, self.hardware, self.settings)
        return self.profiles[device]


def _lookup(series):
    return dict(zip(series.times, series.values))


def _finite(value):
    return value is not None and not math.isnan(value)


def derive(base, function, *others):
    """(times, values) from `base` with function(value, *other_values_at_same_time).

    Samples where `base` or any other series has no value at that time are dropped.
    """
    lookups = [_lookup(other) for other in others]
    times, values = [], []
    for time_value, value in zip(base.times, base.values):
        extra = []
        missing = False
        for lookup in lookups:
            if time_value not in lookup:
                missing = True
                break
            extra.append(lookup[time_value])
        if missing:
            continue
        result = function(value, *extra)
        times.append(time_value)
        values.append(result)
    return times, values


def coverage(inner, outer):
    """Share of `inner`'s time span that `outer` covers."""
    if not inner.times or not outer.times:
        return 0.0
    inner_start, inner_end = inner.times[0], inner.times[-1]
    overlap = min(inner_end, outer.times[-1]) - max(inner_start, outer.times[0])
    span = inner_end - inner_start
    if span <= 0:
        return 1.0 if overlap >= 0 else 0.0
    return max(0.0, overlap) / span


def _indicator(name, device, title, unit, warn, crit, direction, times, values, interval, **extra):
    return Indicator(name=name, device=device, title=title, unit=unit, warn=warn, crit=crit,
                     direction=direction, times=times, values=values, interval_s=interval, **extra)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def disk_indicators(context):
    host, settings = context.host, context.settings
    min_tps = settings.float("analysis.disk_min_tps")
    indicators = []

    devices = host.devices("disk.tps") or host.devices("disk.read_kBps")
    for device in devices:
        profile = context.profile(device)
        source_text = profile.threshold_source
        sar_tps = host.get("disk.tps", device, ("sar",))
        sar_await = host.get("disk.await_ms", device, ("sar",))
        r_await = host.get("disk.r_await_ms", device, ("psar",))
        w_await = host.get("disk.w_await_ms", device, ("psar",))

        # psar splits read and write latency; prefer it where it covers the sar window.
        psar_latency = r_await is not None and w_await is not None
        sar_latency = sar_await is not None and sar_tps is not None
        if psar_latency and sar_latency and coverage(sar_await, r_await) >= 0.8:
            sar_latency = False
        if sar_latency:
            times, values = derive(sar_await, lambda a, t: a if t >= min_tps else 0.0, sar_tps)
            indicators.append(_indicator(
                "disk_await", device, "%s await" % device, "ms", profile.warn_ms, profile.crit_ms, "above",
                times, values, sar_await.interval_s, threshold_source=source_text, metric="disk.await_ms"))
        if psar_latency:
            for rule, series, rate_metric, word in (("disk_r_await", r_await, "disk.r_s", "read"),
                                                    ("disk_w_await", w_await, "disk.w_s", "write")):
                rate = host.get(rate_metric, device, ("psar",))
                if rate is None:
                    continue
                times, values = derive(series, lambda a, t: a if t >= min_tps else 0.0, rate)
                indicators.append(_indicator(
                    rule, device, "%s %s await" % (device, word), "ms", profile.warn_ms, profile.crit_ms, "above",
                    times, values, series.interval_s, threshold_source=source_text,
                    metric="disk.%s_await_ms" % word[0]))

        if profile.media_class in HDD_CLASSES:
            _queue_indicator(context, indicators, device, profile, sar_await, r_await, w_await, source_text)
            util = host.get("disk.util_pct", device)
            if util is not None:
                indicators.append(_indicator(
                    "disk_util", device, "%s utilisation" % device, "%",
                    settings.float("disk.util.warn_pct"), settings.float("disk.util.crit_pct"), "above",
                    list(util.times), list(util.values), util.interval_s, role=SUPPORTING,
                    threshold_source="default:hdd", metric="disk.util_pct"))

        indicators.extend(_throughput_indicators(context, device, profile))

    indicators.extend(_stack_overhead_indicators(context, devices, min_tps))
    return indicators


def _queue_indicator(context, indicators, device, profile, sar_await, r_await, w_await, source_text):
    host, settings = context.host, context.settings
    queue = host.get("disk.queue", device)
    if queue is None:
        return
    warn_factor = settings.float("disk.queue.warn_factor")
    crit_factor = settings.float("disk.queue.crit_factor")
    parallelism = float(profile.parallelism or 1)
    if sar_await is not None:
        latency = _lookup(sar_await)
    elif r_await is not None and w_await is not None:
        read_lookup, write_lookup = _lookup(r_await), _lookup(w_await)
        latency = {t: max(read_lookup[t], write_lookup.get(t, 0.0)) for t in read_lookup}
    else:
        return
    times, values, crit_values = [], [], []
    for time_value, depth in zip(queue.times, queue.values):
        wait = latency.get(time_value)
        if wait is None:
            continue
        ratio = depth / parallelism
        times.append(time_value)
        values.append(ratio if wait >= profile.warn_ms else 0.0)
        crit_values.append(ratio if wait >= profile.crit_ms else 0.0)
    labels = []
    if not profile.parallelism_known:
        labels.append("parallelism unknown")
    indicators.append(_indicator(
        "disk_queue", device, "%s queue depth per spindle" % device, "x parallelism", warn_factor, crit_factor,
        "above", times, values, queue.interval_s, threshold_source=source_text, crit_values=crit_values,
        labels=labels, metric="disk.queue"))


def _throughput_indicators(context, device, profile):
    host, settings = context.host, context.settings
    result = []
    read = host.get("disk.read_kBps", device)
    write = host.get("disk.write_kBps", device)
    tps = host.get("disk.tps", device)
    fraction_warn = settings.float("disk.limit.warn_fraction")
    fraction_crit = settings.float("disk.limit.crit_fraction")

    if read is not None and write is not None:
        times, values = derive(read, lambda r, w: r + w, write)
        warn = crit = None
        source = ""
        if profile.throughput_kBps_limit:
            warn = profile.throughput_kBps_limit * fraction_warn
            crit = profile.throughput_kBps_limit * fraction_crit
            source = "limit:%.0f MiB/s" % (profile.throughput_kBps_limit / 1024.0)
        elif settings.has("disk.throughput_kBps.warn"):
            warn = settings.float("disk.throughput_kBps.warn")
            crit = settings.float("disk.throughput_kBps.crit", warn * 2)
            source = "config"
        if warn is not None:
            result.append(_indicator(
                "disk_throughput", device, "%s throughput" % device, "kB/s", warn, crit, "above",
                times, values, read.interval_s, role=SUPPORTING, threshold_source=source, metric="disk.throughput_kBps"))
    for rule, series, key, word in (("disk_read_kBps", read, "disk.read_kBps", "read"),
                                    ("disk_write_kBps", write, "disk.write_kBps", "write")):
        if series is not None and settings.has(key + ".warn"):
            warn = settings.float(key + ".warn")
            crit = settings.float(key + ".crit", warn * 2)
            result.append(_indicator(
                rule, device, "%s %s throughput" % (device, word), "kB/s", warn, crit, "above",
                list(series.times), list(series.values), series.interval_s, role=SUPPORTING,
                threshold_source="config", metric=key))
    if tps is not None:
        warn = crit = None
        source = ""
        if profile.iops_limit:
            warn = profile.iops_limit * fraction_warn
            crit = profile.iops_limit * fraction_crit
            source = "limit:%.0f IOPS" % profile.iops_limit
        elif settings.has("disk.iops.warn"):
            warn = settings.float("disk.iops.warn")
            crit = settings.float("disk.iops.crit", warn * 2)
            source = "config"
        if warn is not None:
            result.append(_indicator(
                "disk_iops", device, "%s IOPS" % device, "IO/s", warn, crit, "above",
                list(tps.times), list(tps.values), tps.interval_s, role=SUPPORTING,
                threshold_source=source, metric="disk.tps"))
    return result


def _stack_overhead_indicators(context, devices, min_tps):
    """dm/md await far above its slowest slave: queueing in the stacking layer."""
    host, settings = context.host, context.settings
    result = []
    for device in devices:
        profile = context.profile(device)
        if profile.media_class != "stacked" or not profile.slaves:
            continue
        top = host.get("disk.await_ms", device, ("sar",))
        top_tps = host.get("disk.tps", device, ("sar",))
        slave_awaits = [host.get("disk.await_ms", slave, ("sar",)) for slave in profile.slaves]
        slave_awaits = [series for series in slave_awaits if series is not None]
        if top is None or top_tps is None or not slave_awaits:
            continue
        slave_lookups = [_lookup(series) for series in slave_awaits]
        tps_lookup = _lookup(top_tps)
        times, values = [], []
        for time_value, wait in zip(top.times, top.values):
            if tps_lookup.get(time_value, 0.0) < min_tps:
                times.append(time_value)
                values.append(0.0)
                continue
            slowest = max(lookup.get(time_value, 0.0) for lookup in slave_lookups)
            extra = wait - slowest
            times.append(time_value)
            values.append(extra if wait > 2 * slowest else 0.0)
        result.append(_indicator(
            "stack_overhead", device, "%s await above its slowest member" % device, "ms",
            settings.float("disk.stack.warn_ms"), settings.float("disk.stack.crit_ms"), "above",
            times, values, top.interval_s, threshold_source="default"))
    return result


# ---------------------------------------------------------------------------
# CPU and load
# ---------------------------------------------------------------------------
def cpu_indicators(context):
    host, settings = context.host, context.settings
    result = []
    idle = host.get("cpu.idle_pct", "all")
    if idle is not None:
        result.append(_indicator(
            "cpu_busy", "all", "all-CPU idle", "% idle",
            settings.float("cpu.busy.warn_idle_pct"), settings.float("cpu.busy.crit_idle_pct"), "below",
            list(idle.times), list(idle.values), idle.interval_s, metric="cpu.idle_pct"))
        floor = settings.float("cpu.single.others_idle_min_pct")
        for device in host.devices("cpu.idle_pct", ("sar",)):
            if device == "all":
                continue
            one = host.get("cpu.idle_pct", device, ("sar",))
            times, values = derive(one, lambda own, all_idle: own if all_idle > floor else 100.0, idle)
            result.append(_indicator(
                "cpu_single", device, "CPU %s idle while the others are not busy" % device, "% idle",
                settings.float("cpu.single.warn_idle_pct"), settings.float("cpu.single.crit_idle_pct"),
                "below", times, values, one.interval_s, metric="cpu.idle_pct"))
    steal = host.get("cpu.steal_pct", "all")
    if steal is not None:
        result.append(_indicator(
            "cpu_steal", "all", "CPU steal", "%", settings.float("cpu.steal.warn_pct"),
            settings.float("cpu.steal.crit_pct"), "above", list(steal.times), list(steal.values),
            steal.interval_s, metric="cpu.steal_pct"))
    for device in host.devices("cpu.soft_pct", ("sar",)):
        if device == "all":
            continue
        soft = host.get("cpu.soft_pct", device, ("sar",))
        result.append(_indicator(
            "cpu_soft_single", device, "CPU %s softirq" % device, "%", settings.float("cpu.soft_single.warn_pct"),
            settings.float("cpu.soft_single.crit_pct"), "above", list(soft.times), list(soft.values),
            soft.interval_s, metric="cpu.soft_pct"))
    iowait = host.get("cpu.iowait_pct", "all")
    if iowait is not None:
        result.append(_indicator(
            "iowait", "all", "iowait", "%", settings.float("cpu.iowait.warn_pct"),
            settings.float("cpu.iowait.crit_pct"), "above", list(iowait.times), list(iowait.values),
            iowait.interval_s, role=SUPPORTING, metric="cpu.iowait_pct"))

    cpus = context.cpus
    if cpus:
        runq = host.get("load.runq")
        if runq is not None:
            result.append(_indicator(
                "runq", "", "run queue per CPU", "tasks / CPU", settings.float("cpu.runq.warn_per_cpu"),
                settings.float("cpu.runq.crit_per_cpu"), "above", list(runq.times),
                [value / cpus for value in runq.values], runq.interval_s, metric="load.runq"))
        load = host.get("load.avg1")
        if load is not None:
            result.append(_indicator(
                "load", "", "1-minute load average per CPU", "load / CPU", settings.float("cpu.load.warn_per_cpu"),
                settings.float("cpu.load.crit_per_cpu"), "above", list(load.times),
                [value / cpus for value in load.values], load.interval_s, role=SUPPORTING, metric="load.avg1"))
        blocked = host.get("load.blocked")
        if blocked is not None:
            result.append(_indicator(
                "blocked", "", "blocked tasks", "tasks", settings.float("cpu.blocked.warn"),
                max(settings.float("cpu.blocked.warn"), settings.float("cpu.blocked.crit_per_cpu") * cpus),
                "above", list(blocked.times), list(blocked.values), blocked.interval_s, role=SUPPORTING,
                metric="load.blocked"))
    else:
        context.note("CPU count unknown: run queue and load rules skipped")

    cswch = host.get("cpu.cswch_ps")
    if cswch is not None:
        finite = cswch.finite_values()
        median = statistics.median(finite) if finite else 0.0
        if median > 0:
            result.append(_indicator(
                "cswch", "", "context switches", "/s", median * settings.float("cpu.cswch.warn_ratio"),
                median * settings.float("cpu.cswch.crit_ratio"), "above", list(cswch.times), list(cswch.values),
                cswch.interval_s, role=SUPPORTING, threshold_source="relative to run median",
                metric="cpu.cswch_ps"))

    psi = host.get("psi.cpu_some")
    if psi is not None:
        result.append(_indicator(
            "psi_cpu", "", "CPU pressure (some)", "%", settings.float("cpu.psi.warn_pct"),
            settings.float("cpu.psi.crit_pct"), "above", list(psi.times), list(psi.values), psi.interval_s,
            metric="psi.cpu_some"))
    return result


# ---------------------------------------------------------------------------
# Memory and pressure (PSI)
# ---------------------------------------------------------------------------
def memory_indicators(context):
    host, settings = context.host, context.settings
    result = []
    total = context.mem_total_kb
    avail = host.get("mem.avail_kB")
    if avail is not None and total:
        result.append(_indicator(
            "mem_avail", "", "available memory", "% of RAM", settings.float("mem.avail.warn_pct"),
            settings.float("mem.avail.crit_pct"), "below", list(avail.times),
            [100.0 * value / total for value in avail.values], avail.interval_s, role=SUPPORTING, metric="mem.avail_kB"))
    elif avail is not None:
        context.note("MemTotal unknown: available-memory rule skipped")

    simple = (
        ("swap_out", "swap.out_pps", "swap-out", "pages/s", "mem.swap_out.warn_pps", "mem.swap_out.crit_pps", PRIMARY),
        ("swap_in", "swap.in_pps", "swap-in", "pages/s", "mem.swap_in.warn_pps", "mem.swap_in.crit_pps", SUPPORTING),
        ("direct_reclaim", "paging.scan_direct_ps", "direct reclaim scanning", "pages/s",
         "mem.direct_reclaim.warn_pps", "mem.direct_reclaim.crit_pps", PRIMARY),
        ("major_faults", "paging.majflt_ps", "major page faults", "/s", "mem.major_faults.warn_ps",
         "mem.major_faults.crit_ps", SUPPORTING),
        ("psi_mem_some", "psi.mem_some", "memory pressure (some)", "%", "mem.psi_some.warn_pct",
         "mem.psi_some.crit_pct", PRIMARY),
        ("psi_mem_full", "psi.mem_full", "memory pressure (full)", "%", "mem.psi_full.warn_pct",
         "mem.psi_full.crit_pct", PRIMARY),
        ("psi_io_some", "psi.io_some", "IO pressure (some)", "%", "io.psi_some.warn_pct", "io.psi_some.crit_pct", PRIMARY),
        ("psi_io_full", "psi.io_full", "IO pressure (full)", "%", "io.psi_full.warn_pct", "io.psi_full.crit_pct", PRIMARY),
    )
    for name, metric, title, unit, warn_key, crit_key, role in simple:
        series = host.get(metric)
        if series is None:
            continue
        result.append(_indicator(
            name, "", title, unit, settings.float(warn_key), settings.float(crit_key), "above",
            list(series.times), list(series.values), series.interval_s, role=role, metric=metric))

    dirty = host.get("mem.dirty_kB")
    if dirty is not None and total:
        result.append(_indicator(
            "dirty", "", "dirty pages", "% of RAM", settings.float("mem.dirty.warn_pct"),
            settings.float("mem.dirty.crit_pct"), "above", list(dirty.times),
            [100.0 * value / total for value in dirty.values], dirty.interval_s, role=SUPPORTING,
            metric="mem.dirty_kB"))
    commit = host.get("mem.commit_pct")
    if commit is not None:
        strict = context.hardware is not None and context.hardware.get("vm.overcommit_memory") == "2"
        result.append(_indicator(
            "commit", "", "committed memory", "% of RAM + swap", settings.float("mem.commit.warn_pct"),
            settings.float("mem.commit.crit_pct"), "above", list(commit.times), list(commit.values),
            commit.interval_s, role=SUPPORTING if strict else INFO, metric="mem.commit_pct"))
    return result


def softnet_indicators(context):
    """Softnet drops: supporting evidence for the softirq-concentration hypothesis."""
    series = context.host.get("softnet.dropped_ps", "all")
    if series is None:
        return []
    return [_indicator("softnet_drop", "", "softnet drops", "/s", 0.0, 1.0, "above", list(series.times),
                       list(series.values), series.interval_s, role=SUPPORTING, threshold_source="any drop",
                       metric="softnet.dropped_ps")]


def build_indicators(context):
    indicators = []
    indicators.extend(disk_indicators(context))
    indicators.extend(cpu_indicators(context))
    indicators.extend(memory_indicators(context))
    indicators.extend(softnet_indicators(context))
    host = context.host
    if not host.has("psi.cpu_some") and not host.has("psi.io_some"):
        if host.psi_expected:
            context.note("PSI not available in the sysstat export (sysstat %s; PSI needs 12.3.3)" %
                         (host.sysstat_version.split()[-1] if host.sysstat_version else "unknown"))
        else:
            context.note("PSI not available: the kernel does not provide /proc/pressure")
    if host.has("paging.scan_direct_ps", ("sar",)) is False and host.has("swap.out_pps", ("psar",)):
        context.note("direct reclaim (pgscand/s) is not in psar data; that rule needs the sar export")
    return indicators
