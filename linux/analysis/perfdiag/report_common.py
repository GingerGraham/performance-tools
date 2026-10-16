"""Section data shared by the HTML and Markdown reports (section 9.1)."""

from . import findings
from .charts import chart_catalogue, safe_name
from .inspect_cmd import human_bytes
from .loader import SADF_EXPORTS
from .timeutil import duration_text, stamp, stamp_minutes, zone_label
from . import VERSION

EXPECTED_ACTIVITIES = [name for name, _ in SADF_EXPORTS]
STORAGE_CODES = ("H1", "H2", "H7", "H10")
CPU_CHARTS = {"H3": ("cpu", "runq_load", "psi"), "H4": ("cpu",), "H5": ("cpu", "cpu_heatmap"),
              "H8": ("runq_load", "cpu"), "H9": ("cpu_heatmap",)}
MEMORY_CHARTS = ("memory", "memory_pressure", "psi")
DISK_CHART_PREFIXES = ("disk_latency_", "disk_throughput_", "disk_iops_", "disk_queue_")


def header_facts(result):
    host = result.host
    zone = host.zone
    facts = [("Host", host.host), ("Distribution", host.distro or "unknown"), ("Kernel", host.kernel or "unknown")]
    cpu_text = "%s logical CPUs" % host.cpus if host.cpus else "unknown"
    hardware = host.hardware
    if hardware is not None and hardware.get("cpu.model"):
        cpu_text += ", %s" % hardware.get("cpu.model")
    facts.append(("CPUs", cpu_text))
    facts.append(("Memory", human_bytes(host.mem_total_kb * 1024) if host.mem_total_kb else "unknown"))
    if host.start is not None:
        facts.append(("Collection window", "%s to %s %s (%s)" % (
            stamp(host.start, zone), stamp(host.end, zone), zone_label(zone), duration_text(host.end - host.start))))
    facts.append(("Sample interval", "%d s" % result.interval_s))
    facts.append(("Data sources", "; ".join(host.data_sources) or "none"))
    return facts


def evidence_rows(result, hypothesis):
    """[(role, indicator, device, window text, duration, mean, peak, score)] for a hypothesis."""
    zone = result.host.zone
    rows = []

    def add(role, episode):
        indicator = result.indicator(episode.rule, episode.device)
        unit = indicator.unit if indicator else ""
        title = indicator.title if indicator else episode.rule
        rows.append((role, title, episode.device or "-",
                     "%s to %s" % (stamp_minutes(episode.start, zone), stamp_minutes(episode.end, zone)),
                     duration_text(episode.duration_s), findings.format_value(episode.mean, unit),
                     findings.format_value(episode.peak, unit), "%.1f" % episode.score))
    if hypothesis.primary is not None:
        add("primary", hypothesis.primary)
    for episode in hypothesis.other_primary[:5]:
        add("primary (other window)", episode)
    for episode in hypothesis.supporting:
        add("supporting", episode)
    for episode in hypothesis.contradicting:
        add("against", episode)
    return rows


def charts_for(result, hypothesis, catalogue):
    """Chart names that belong with a hypothesis, from the catalogue."""
    names = {chart.name for chart in catalogue}
    wanted = []
    if hypothesis.id in STORAGE_CODES:
        suffix = safe_name(hypothesis.subject)
        for chart in catalogue:
            if chart.name.startswith(DISK_CHART_PREFIXES) and (
                    hypothesis.subject == "cloud-sum" or chart.name.endswith("_" + suffix)):
                wanted.append(chart.name)
        wanted.append("psi")
    elif hypothesis.id == "H6":
        wanted.extend(MEMORY_CHARTS)
    else:
        wanted.extend(CPU_CHARTS.get(hypothesis.id, ()))
    return [name for name in wanted if name in names]


def time_above_rows(result):
    rows = []
    for key, stats in sorted(result.time_above.items(), key=lambda item: (-item[1]["warn_pct"], item[0])):
        unit = stats.get("unit", "")
        above = stats["direction"] == "above"
        tail = stats["p95"] if above else stats["p5"]
        extreme = stats["max"] if above else stats["min"]
        rows.append((key, stats.get("role", ""), "%.1f %%" % stats["warn_pct"], "%.1f %%" % stats["crit_pct"],
                     findings.format_value(stats["p50"], unit), findings.format_value(tail, unit),
                     findings.format_value(extreme, unit), "below" if not above else "above"))
    return rows


def device_chain(hardware, kname):
    """'pg-data (dm-2) on nvme1n1'."""
    disk = hardware.disks.get(kname)
    if disk is None:
        return kname or "unknown"
    text = kname
    if disk.display:
        text = "%s (%s)" % (disk.display, kname)
    if disk.slaves:
        text += " on " + ", ".join(device_chain(hardware, slave) for slave in disk.slaves[:3])
    return text


def layout_facts(host):
    hardware = host.hardware
    if hardware is None:
        return None
    facts = []
    cpu = ["%s logical" % hardware.get("cpu.logical", "?"), "%s physical cores" % hardware.get("cpu.physical_cores", "?"),
           "%s socket(s)" % hardware.get("cpu.sockets", "?")]
    if hardware.get("cpu.hypervisor") == "yes":
        cpu.append("virtual machine (vCPU topology as presented by the hypervisor)")
    facts.append(("CPU", ", ".join(cpu)))
    if hardware.get("numa.nodes"):
        facts.append(("NUMA nodes", hardware.get("numa.nodes")))
    facts.append(("Memory settings", "swappiness %s, dirty_ratio %s, dirty_background_ratio %s, THP %s" % (
        hardware.get("vm.swappiness", "?"), hardware.get("vm.dirty_ratio", "?"),
        hardware.get("vm.dirty_background_ratio", "?"), hardware.get("mem.thp_enabled", "?"))))
    if hardware.get("cloud.provider"):
        facts.append(("Cloud", "%s %s" % (hardware.get("cloud.provider"),
                                          hardware.get("cloud.instance_type") or hardware.get("cloud.product_name"))))
    disks = []
    for disk in hardware.disks.values():
        disks.append((disk.name_for_humans, disk.kname, disk.media_class, human_bytes(disk.size_bytes),
                      disk.model or disk.vendor or "-",
                      str(disk.parallelism) if disk.parallelism is not None else "-",
                      ", ".join(disk.slaves) or "-"))
    mounts = [(fs.mountpoint, fs.fstype, device_chain(hardware, fs.kname) if fs.kname else "no block device")
              for fs in hardware.filesystems]
    return facts, disks, mounts


def coverage_lines(result):
    host = result.host
    lines = []
    interval = result.interval_s
    lines.append("Samples are %d-second averages. A stall shorter than %d s is not visible in them. A run counts as "
                 "sustained after %d s (%d samples)." % (interval, interval, result.sustain_s,
                                                          max(1, result.sustain_s // max(interval, 1))))
    if result.interval_limited:
        lines.append("Interval-limited: the sustain window was raised to three samples because the interval is "
                     "coarse. Every finding is labelled interval-limited.")
    if host.activities:
        missing = [name for name in EXPECTED_ACTIVITIES if name not in host.activities]
        if missing:
            lines.append("sadf activities not in the data: %s." % ", ".join(missing))
    has_psi = host.has("psi.cpu_some") or host.has("psi.io_some")
    lines.append("PSI is %s." % ("present" if has_psi else "not available; the hypotheses fall back to latency and blocked tasks"))
    if host.gaps:
        lines.append("%d gap%s in the data longer than twice the interval; lines are broken there and no episode spans a gap." % (
            len(host.gaps), "" if len(host.gaps) == 1 else "s"))
    if host.sysstat_version:
        lines.append("Collected with %s." % host.sysstat_version)
    if not host.process_rows:
        lines.append("No pidstat data: process attribution is unavailable.")
    if not host.kernel_events and host.start is not None:
        lines.append("No kernel log events matched in the window.")
    for note in result.notes + host.notes:
        lines.append("NOTE: " + note)
    return lines


def settings_rows(result):
    settings = result.settings
    rows = [("analysis.sustain_s (effective)", str(result.sustain_s),
             "raised from %s: interval-limited" % settings.values["analysis.sustain_s"] if result.interval_limited else
             settings.sources.get("analysis.sustain_s", "default")),
            ("analysis.gap_samples (effective)", str(result.gap_samples), settings.sources.get("analysis.gap_samples", "default"))]
    for key, (value, source) in sorted(settings.changed.items()):
        rows.append((key, value, source))
    return rows


def summary_hypotheses(result, limit=5):
    return result.hypotheses[:limit]


def anchor(hypothesis):
    return "finding-%s-%s" % (hypothesis.id.lower(), safe_name(hypothesis.subject).lower())


def generated_by():
    return "perfdiag %s" % VERSION


def catalogue_for(result):
    return chart_catalogue(result)
