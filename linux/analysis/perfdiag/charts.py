"""matplotlib charts (section 7).

Conventions, from the spec:

* One y-axis per chart; measures on different scales go in small multiples.
* Threshold lines dashed in the status colours, labelled at the right edge.
* Sustained episodes: full-height band in the critical colour at 10 % opacity,
  labelled with the duration. Transient episodes: warning colour at 18 %.
* Series use a fixed categorical order. The status colours are never used for series.
* Text in neutral ink. A legend for two or more series; otherwise the title names the series.
* Throughput axes in MB/s with SI suffixes (EngFormatter), never raw kB/s.
* Gaps longer than 2 x interval break the line.
* Above about 5000 points per series, drawing downsamples with min/max per bucket so
  peaks survive. Analysis always uses the full data.
* Charts render on a light surface in both colour schemes.
"""

import datetime
import io
import math
import re

from .findings import format_threshold_number
from .timeutil import duration_text, zone_label

SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#8e5bd0", "#0e8ea0", "#a8791c")
STATUS_WARNING = "#fab219"
STATUS_CRITICAL = "#d03b3b"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
GRID = "#e4e3df"
SURFACE = "#ffffff"
MAX_POINTS = 5000
HEATMAP_MAX_CPUS = 128

_plt = None
_mdates = None
_ticker = None


def _load_matplotlib():
    """Imports matplotlib with the Agg backend, once. Works without a display (WSL, macOS without a GUI)."""
    global _plt, _mdates, _ticker
    if _plt is not None:
        return
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    matplotlib.rcParams.update({
        "svg.fonttype": "none",              # text stays selectable
        "svg.hashsalt": "perfdiag",          # stable output
        "font.family": "sans-serif",
        "font.size": 9,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_SECONDARY,
        "axes.titlecolor": INK,
        "axes.titlesize": 10,
        "axes.titleweight": "bold",
        "xtick.color": INK_SECONDARY,
        "ytick.color": INK_SECONDARY,
        "text.color": INK,
        "axes.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
    })
    _plt, _mdates, _ticker = plt, mdates, ticker


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def to_local(epoch, zone):
    return datetime.datetime.fromtimestamp(epoch, zone)


def with_gaps(times, values, interval_s):
    """Inserts NaN where consecutive samples are further apart than 2 x interval."""
    if not times:
        return [], []
    out_times, out_values = [times[0]], [values[0]]
    for previous, current, value in zip(times, times[1:], values[1:]):
        if interval_s and current - previous > 2 * interval_s:
            out_times.append(previous + interval_s)
            out_values.append(math.nan)
        out_times.append(current)
        out_values.append(value)
    return out_times, out_values


def downsample(times, values, limit=MAX_POINTS):
    """Min and max of each bucket, in time order, so peaks survive drawing."""
    count = len(times)
    if count <= limit:
        return times, values
    buckets = limit // 2
    size = int(math.ceil(count / float(buckets)))
    out_times, out_values = [], []
    for first in range(0, count, size):
        chunk = [(t, v) for t, v in zip(times[first:first + size], values[first:first + size]) if not math.isnan(v)]
        if not chunk:
            out_times.append(times[first])
            out_values.append(math.nan)
            continue
        low = min(chunk, key=lambda item: item[1])
        high = max(chunk, key=lambda item: item[1])
        for item in sorted({low, high}, key=lambda item: item[0]):
            out_times.append(item[0])
            out_values.append(item[1])
    return out_times, out_values


def prepare(series_times, series_values, interval_s, zone, scale=1.0):
    times, values = with_gaps(series_times, [v * scale for v in series_values], interval_s)
    times, values = downsample(times, values)
    return [to_local(t, zone) for t in times], values


# ---------------------------------------------------------------------------
# Axes helpers
# ---------------------------------------------------------------------------
def new_figure(height=3.2, width=10.0):
    _load_matplotlib()
    figure, axes = _plt.subplots(figsize=(width, height), dpi=110)
    axes.set_axisbelow(True)
    axes.grid(True, color=GRID, linewidth=0.7)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    return figure, axes


def time_axis(axes, result):
    host = result.host
    span = (host.end - host.start) if host.start is not None else 0
    if span > 20 * 3600:
        pattern = "%d %b %H:%M"
    elif span > 3600:
        pattern = "%H:%M"
    else:
        pattern = "%H:%M:%S"
    axes.xaxis.set_major_formatter(_mdates.DateFormatter(pattern, tz=host.zone))
    axes.xaxis.set_major_locator(_mdates.AutoDateLocator(tz=host.zone, maxticks=10))
    if host.start is not None:
        axes.set_xlim(to_local(host.start, host.zone), to_local(host.end, host.zone))
    axes.set_xlabel("Time (%s)" % zone_label(host.zone))


def threshold_line(axes, value, label, color):
    axes.axhline(value, color=color, linewidth=1.2, linestyle=(0, (4, 3)), zorder=2)
    axes.text(1.005, value, label, transform=axes.get_yaxis_transform(), color=INK_SECONDARY, fontsize=8,
              va="center", ha="left", clip_on=False)


def episode_bands(axes, result, episodes, zone):
    top = axes.get_ylim()[1]
    for episode in episodes:
        start, end = to_local(episode.start, zone), to_local(episode.end, zone)
        if episode.kind == "sustained":
            axes.axvspan(start, end, color=STATUS_CRITICAL, alpha=0.10, linewidth=0, zorder=1)
            axes.annotate(duration_text(episode.duration_s), xy=(start + (end - start) / 2, top),
                          xytext=(0, -3), textcoords="offset points", ha="center", va="top", fontsize=7.5,
                          color=INK_SECONDARY)
        else:
            axes.axvspan(start, end, color=STATUS_WARNING, alpha=0.18, linewidth=0, zorder=1)


def finish(figure, axes, result, title, ylabel, episodes=(), legend=False, right_margin=True):
    time_axis(axes, result)
    axes.set_ylabel(ylabel)
    axes.set_title(title, loc="left")
    if legend:
        axes.legend(loc="upper left", frameon=False, fontsize=8, ncol=3)
    if episodes:
        episode_bands(axes, result, episodes, result.host.zone)
    figure.tight_layout()
    if right_margin:
        figure.subplots_adjust(right=0.86)
    return figure


def render(figure, fmt):
    """SVG text or PNG bytes for a figure; closes it."""
    buffer = io.BytesIO()
    if fmt == "svg":
        figure.savefig(buffer, format="svg")
    else:
        figure.savefig(buffer, format="png", dpi=110)
    _plt.close(figure)
    data = buffer.getvalue()
    if fmt == "svg":
        return data.decode("utf-8")
    return data


def inline_svg(svg, prefix):
    """SVG ready to sit inside HTML: no XML header, element ids made unique per chart."""
    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    # Drop the fixed pt size so the chart scales with the page; the viewBox stays.
    svg = re.sub(r'(<svg[^>]*?)\s+width="[^"]*"\s+height="[^"]*"', r"\1", svg, count=1)
    svg = re.sub(r"<!DOCTYPE[^>]*>\s*", "", svg)
    svg = re.sub(r"<!--.*?-->\s*", "", svg, flags=re.S)
    ids = set(re.findall(r'\bid="([^"]+)"', svg))
    for old in sorted(ids, key=len, reverse=True):
        new = "%s-%s" % (prefix, old)
        svg = svg.replace('id="%s"' % old, 'id="%s"' % new)
        svg = svg.replace("url(#%s)" % old, "url(#%s)" % new)
        svg = svg.replace('href="#%s"' % old, 'href="#%s"' % new)
    return svg


# ---------------------------------------------------------------------------
# Chart catalogue
# ---------------------------------------------------------------------------
class ChartRef:
    """A chart that can be rendered on request."""

    def __init__(self, name, title, builder, tags=()):
        self.name = name
        self.title = title
        self.builder = builder
        self.tags = tags            # hypothesis codes this chart belongs with

    def render(self, fmt):
        return render(self.builder(), fmt)


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "x"


def episodes_for(result, rules, device=None):
    found = []
    for episode in result.episodes:
        if episode.rule in rules and (device is None or episode.device == device):
            found.append(episode)
    return found


def devices_with_findings(result):
    names = []
    for episode in result.episodes:
        if episode.rule.startswith(("disk_", "stack_")) and episode.device not in names \
                and episode.device != "cloud-sum":
            if episode.kind == "sustained" or episode.impact == "impactful":
                names.append(episode.device)
    for hypothesis in result.hypotheses:
        if hypothesis.id in ("H1", "H2", "H7", "H10") and hypothesis.subject not in names \
                and hypothesis.subject in result.host.devices("disk.tps"):
            names.append(hypothesis.subject)
    return names


def chart_catalogue(result):
    """All charts that make sense for this host's data, in report order."""
    _load_matplotlib()
    host = result.host
    charts = []
    if result.episodes or host.kernel_events:
        if any(e.kind == "sustained" or e.impact == "impactful" for e in result.episodes) or host.kernel_events:
            charts.append(ChartRef("timeline", "Indicator timeline", lambda: timeline_chart(result)))

    for device in devices_with_findings(result):
        suffix = safe_name(device)
        tags = ("H1", "H2", "H7", "H10")
        charts.append(ChartRef("disk_latency_" + suffix, "%s latency" % device, lambda d=device: latency_chart(result, d), tags))
        charts.append(ChartRef("disk_throughput_" + suffix, "%s throughput" % device, lambda d=device: throughput_chart(result, d), tags))
        charts.append(ChartRef("disk_iops_" + suffix, "%s IOPS" % device, lambda d=device: iops_chart(result, d), tags))
        profile = result.profile_for(device)
        if profile is not None and profile.media_class in ("hdd", "hw-raid") or result.indicator("disk_queue", device):
            charts.append(ChartRef("disk_queue_" + suffix, "%s queue depth" % device, lambda d=device: queue_chart(result, d), tags))

    if host.get("cpu.idle_pct", "all") is not None:
        charts.append(ChartRef("cpu", "CPU", lambda: cpu_chart(result), ("H3", "H4", "H5")))
        cpu_devices = [d for d in host.devices("cpu.idle_pct", ("sar",)) if d != "all"]
        if cpu_devices and len(cpu_devices) <= HEATMAP_MAX_CPUS:
            charts.append(ChartRef("cpu_heatmap", "Per-CPU busy", lambda: cpu_heatmap_chart(result), ("H5", "H9")))
    if host.get("load.runq") is not None or host.get("load.avg1") is not None:
        charts.append(ChartRef("runq_load", "Run queue and load", lambda: runq_load_chart(result), ("H3", "H8")))
    if host.get("mem.avail_kB") is not None and host.mem_total_kb:
        charts.append(ChartRef("memory", "Memory", lambda: memory_chart(result), ("H6",)))
    if any(host.get(m) is not None for m in ("swap.out_pps", "paging.scan_direct_ps", "paging.majflt_ps")):
        charts.append(ChartRef("memory_pressure", "Memory pressure", lambda: memory_pressure_chart(result), ("H6",)))
    if any(host.get(m) is not None for m in ("psi.cpu_some", "psi.io_some", "psi.mem_some")):
        charts.append(ChartRef("psi", "Pressure stall information", lambda: psi_chart(result), ("H1", "H3", "H6", "H7")))
    return charts


def write_all(result, directory, fmt):
    """Renders the whole catalogue to files. Returns the paths."""
    import os

    paths = []
    for chart in chart_catalogue(result):
        path = os.path.join(directory, "%s.%s" % (chart.name, fmt))
        data = chart.render(fmt)
        mode = "w" if fmt == "svg" else "wb"
        with open(path, mode, **({"encoding": "utf-8"} if fmt == "svg" else {})) as handle:
            handle.write(data)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Individual charts
# ---------------------------------------------------------------------------
def line_series(axes, result, series, label, color, scale=1.0):
    times, values = prepare(series.times, series.values, series.interval_s, result.host.zone, scale)
    axes.plot(times, values, color=color, linewidth=1.8, label=label, zorder=3)


def timeline_chart(result):
    from .analysis import key_text

    host, zone = result.host, result.host.zone
    lanes = []
    for episode in result.episodes:
        if episode.kind == "sustained" or episode.impact == "impactful":
            lane = key_text(episode.rule, episode.device)
            if lane not in lanes:
                lanes.append(lane)
    has_events = bool(host.kernel_events)
    rows = len(lanes) + (1 if has_events else 0)
    figure, axes = new_figure(height=max(2.2, 0.42 * rows + 1.3))
    axes.grid(True, axis="x", color=GRID, linewidth=0.7)
    axes.grid(False, axis="y")
    labels = []
    for index, lane in enumerate(lanes):
        for episode in result.episodes:
            if key_text(episode.rule, episode.device) != lane:
                continue
            if not (episode.kind == "sustained" or episode.impact == "impactful"):
                continue
            color = STATUS_CRITICAL if episode.kind == "sustained" else STATUS_WARNING
            start = _mdates.date2num(to_local(episode.start, zone))
            width = _mdates.date2num(to_local(episode.end, zone)) - start
            axes.broken_barh([(start, max(width, 1.0 / 86400.0 * 2))], (index - 0.35, 0.7), facecolors=color, zorder=3)
        labels.append(lane)
    if has_events:
        index = len(lanes)
        times = [to_local(event.time, zone) for event in host.kernel_events]
        axes.scatter(times, [index] * len(times), marker="D", s=28, color=INK, zorder=4)
        labels.append("kernel events")
    axes.set_yticks(range(len(labels)))
    axes.set_yticklabels(labels, fontsize=8)
    axes.set_ylim(len(labels) - 0.4, -0.7)
    axes.set_title("Indicator timeline  (red = sustained, amber = impactful transient)", loc="left")
    time_axis(axes, result)
    figure.tight_layout()
    return figure


def latency_chart(result, device):
    host = result.host
    figure, axes = new_figure()
    profile = result.profile_for(device)
    drawn = 0
    sar = host.get("disk.await_ms", device, ("sar",))
    reads = host.get("disk.r_await_ms", device, ("psar",))
    writes = host.get("disk.w_await_ms", device, ("psar",))
    if reads is not None and writes is not None:
        line_series(axes, result, reads, "read await", SERIES_COLORS[0])
        line_series(axes, result, writes, "write await", SERIES_COLORS[1])
        drawn = 2
    if sar is not None and (drawn == 0 or result.indicator("disk_await", device) is not None):
        line_series(axes, result, sar, "await", SERIES_COLORS[2] if drawn else SERIES_COLORS[0])
        drawn += 1
    axes.set_ylim(bottom=0)
    if profile is not None:
        threshold_line(axes, profile.warn_ms, "warn %s ms" % format_threshold_number(profile.warn_ms), STATUS_WARNING)
        threshold_line(axes, profile.crit_ms, "crit %s ms" % format_threshold_number(profile.crit_ms), STATUS_CRITICAL)
        top = axes.get_ylim()[1]
        if top < profile.crit_ms * 1.1:
            axes.set_ylim(top=profile.crit_ms * 1.15)
    title = "%s latency (ms)" % device if drawn > 1 else "%s %s (ms)" % (device, "await")
    return finish(figure, axes, result, title, "ms",
                  episodes_for(result, ("disk_await", "disk_r_await", "disk_w_await"), device), legend=drawn > 1)


def throughput_chart(result, device):
    host = result.host
    figure, axes = new_figure()
    read = host.get("disk.read_kBps", device)
    write = host.get("disk.write_kBps", device)
    bytes_per_kb = 1000.0
    if read is not None:
        line_series(axes, result, read, "read", SERIES_COLORS[0], bytes_per_kb)
    if write is not None:
        line_series(axes, result, write, "write", SERIES_COLORS[1], bytes_per_kb)
    axes.yaxis.set_major_formatter(_ticker.EngFormatter(unit="B/s"))
    axes.set_ylim(bottom=0)
    profile = result.profile_for(device)
    if profile is not None and profile.throughput_kBps_limit:
        limit = profile.throughput_kBps_limit * bytes_per_kb
        threshold_line(axes, limit, "declared limit", STATUS_CRITICAL)
        axes.set_ylim(top=max(axes.get_ylim()[1], limit * 1.1))
    return finish(figure, axes, result, "%s throughput" % device, "",
                  episodes_for(result, ("disk_throughput", "disk_read_kBps", "disk_write_kBps", "disk_plateau"), device),
                  legend=read is not None and write is not None)


def iops_chart(result, device):
    host = result.host
    figure, axes = new_figure()
    reads = host.get("disk.r_s", device, ("psar",))
    writes = host.get("disk.w_s", device, ("psar",))
    drawn = 0
    if reads is not None and writes is not None:
        line_series(axes, result, reads, "reads/s", SERIES_COLORS[0])
        line_series(axes, result, writes, "writes/s", SERIES_COLORS[1])
        drawn = 2
    else:
        tps = host.get("disk.tps", device)
        if tps is not None:
            line_series(axes, result, tps, "tps", SERIES_COLORS[0])
            drawn = 1
    axes.set_ylim(bottom=0)
    profile = result.profile_for(device)
    if profile is not None and profile.iops_limit:
        threshold_line(axes, profile.iops_limit, "declared limit", STATUS_CRITICAL)
        axes.set_ylim(top=max(axes.get_ylim()[1], profile.iops_limit * 1.1))
    return finish(figure, axes, result, "%s IOPS" % device if drawn == 2 else "%s transfers per second" % device,
                  "IO/s", episodes_for(result, ("disk_iops",), device), legend=drawn == 2)


def queue_chart(result, device):
    figure, axes = new_figure()
    series = result.host.get("disk.queue", device)
    if series is not None:
        line_series(axes, result, series, "aqu-sz", SERIES_COLORS[0])
    axes.set_ylim(bottom=0)
    profile = result.profile_for(device)
    if profile is not None and profile.media_class in ("hdd", "hw-raid"):
        parallelism = profile.parallelism or 1
        warn = result.settings.float("disk.queue.warn_factor") * parallelism
        threshold_line(axes, warn, "2 x parallelism (%d)" % parallelism, STATUS_WARNING)
        axes.set_ylim(top=max(axes.get_ylim()[1], warn * 1.2))
    return finish(figure, axes, result, "%s average queue depth (aqu-sz)" % device, "requests",
                  episodes_for(result, ("disk_queue",), device))


def cpu_chart(result):
    host = result.host
    figure, axes = new_figure()
    idle = host.get("cpu.idle_pct", "all")
    busy_values = [100.0 - v for v in idle.values]
    times, values = prepare(idle.times, busy_values, idle.interval_s, host.zone)
    axes.plot(times, values, color=SERIES_COLORS[0], linewidth=1.8, label="busy (100 - idle)", zorder=3)
    count = 1
    steal = host.get("cpu.steal_pct", "all")
    if steal is not None:
        line_series(axes, result, steal, "steal", SERIES_COLORS[1])
        count += 1
    iowait = host.get("cpu.iowait_pct", "all")
    if iowait is not None:
        line_series(axes, result, iowait, "iowait", SERIES_COLORS[2])
        count += 1
    axes.set_ylim(0, 105)
    settings = result.settings
    threshold_line(axes, 100 - settings.float("cpu.busy.warn_idle_pct"), "busy warn", STATUS_WARNING)
    threshold_line(axes, 100 - settings.float("cpu.busy.crit_idle_pct"), "busy crit", STATUS_CRITICAL)
    return finish(figure, axes, result, "CPU (% of all CPUs)", "%",
                  episodes_for(result, ("cpu_busy", "cpu_steal")), legend=count > 1)


def cpu_heatmap_chart(result):
    import numpy

    host = result.host
    devices = sorted((d for d in host.devices("cpu.idle_pct", ("sar",)) if d != "all"),
                     key=lambda name: int(name) if name.isdigit() else 0)
    all_times = sorted({t for d in devices for t in host.get("cpu.idle_pct", d, ("sar",)).times})
    index = {t: i for i, t in enumerate(all_times)}
    matrix = numpy.full((len(devices), len(all_times)), numpy.nan)
    for row, device in enumerate(devices):
        series = host.get("cpu.idle_pct", device, ("sar",))
        for time_value, value in zip(series.times, series.values):
            matrix[row, index[time_value]] = 100.0 - value
    figure, axes = new_figure(height=max(2.4, 0.18 * len(devices) + 1.4))
    axes.grid(False)
    numbers = [_mdates.date2num(to_local(t, host.zone)) for t in all_times]
    if len(numbers) > 1:
        step = (numbers[-1] - numbers[0]) / (len(numbers) - 1)
    else:
        step = 1.0 / 1440
    image = axes.imshow(matrix, aspect="auto", cmap="Blues", vmin=0, vmax=100, interpolation="nearest",
                        extent=(numbers[0] - step / 2, numbers[-1] + step / 2, len(devices) - 0.5, -0.5))
    axes.xaxis_date(tz=host.zone)
    axes.set_yticks(range(len(devices)))
    axes.set_yticklabels(["CPU %s" % d for d in devices], fontsize=7)
    figure.colorbar(image, ax=axes, label="% busy", pad=0.01, fraction=0.03)
    axes.set_title("Per-CPU busy (%)", loc="left")
    time_axis(axes, result)
    axes.set_xlim(numbers[0] - step / 2, numbers[-1] + step / 2)
    figure.tight_layout()
    return figure


def runq_load_chart(result):
    host = result.host
    figure, axes = new_figure()
    cpus = host.cpus or 1
    count = 0
    runq = host.get("load.runq")
    if runq is not None:
        times, values = prepare(runq.times, runq.values, runq.interval_s, host.zone, 1.0 / cpus)
        axes.plot(times, values, color=SERIES_COLORS[0], linewidth=1.6, label="run queue / CPUs", zorder=3)
        count += 1
    load = host.get("load.avg1")
    if load is not None:
        times, values = prepare(load.times, load.values, load.interval_s, host.zone, 1.0 / cpus)
        axes.plot(times, values, color=SERIES_COLORS[1], linewidth=1.6, label="1-min load / CPUs", zorder=3)
        count += 1
    axes.set_ylim(bottom=0)
    settings = result.settings
    warn, crit = settings.float("cpu.runq.warn_per_cpu"), settings.float("cpu.runq.crit_per_cpu")
    threshold_line(axes, warn, "%s" % format_threshold_number(warn), STATUS_WARNING)
    threshold_line(axes, crit, "%s" % format_threshold_number(crit), STATUS_CRITICAL)
    axes.set_ylim(top=max(axes.get_ylim()[1], crit * 1.15))
    return finish(figure, axes, result, "Run queue and load per CPU", "per CPU",
                  episodes_for(result, ("runq", "load")), legend=count > 1)


def memory_chart(result):
    host = result.host
    figure, axes = new_figure()
    avail = host.get("mem.avail_kB")
    total = host.mem_total_kb
    times, values = prepare(avail.times, avail.values, avail.interval_s, host.zone, 100.0 / total)
    axes.plot(times, values, color=SERIES_COLORS[0], linewidth=1.8, zorder=3)
    axes.set_ylim(0, 100)
    settings = result.settings
    threshold_line(axes, settings.float("mem.avail.warn_pct"), "warn %s %%" % format_threshold_number(
        settings.float("mem.avail.warn_pct")), STATUS_WARNING)
    threshold_line(axes, settings.float("mem.avail.crit_pct"), "crit %s %%" % format_threshold_number(
        settings.float("mem.avail.crit_pct")), STATUS_CRITICAL)
    return finish(figure, axes, result, "Available memory (% of RAM)", "%", episodes_for(result, ("mem_avail",)))


def small_multiples(result, panels, title):
    """panels: [(label, [(series, line_label)], rule_names)]; one y-axis per panel."""
    _load_matplotlib()
    host = result.host
    figure, axes_list = _plt.subplots(len(panels), 1, figsize=(10, 1.9 * len(panels) + 0.6), dpi=110, sharex=True)
    if len(panels) == 1:
        axes_list = [axes_list]
    for axes, (label, lines, rules) in zip(axes_list, panels):
        axes.set_axisbelow(True)
        axes.grid(True, color=GRID, linewidth=0.7)
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
        for number, (series, line_label) in enumerate(lines):
            times, values = prepare(series.times, series.values, series.interval_s, host.zone)
            axes.plot(times, values, color=SERIES_COLORS[number], linewidth=1.6, label=line_label, zorder=3)
        axes.set_ylim(0, max(axes.get_ylim()[1], 1.0))
        axes.set_ylabel(label, fontsize=8)
        if len(lines) > 1:
            axes.legend(loc="upper left", frameon=False, fontsize=8, ncol=2)
        episode_bands(axes, result, episodes_for(result, rules), host.zone)
    time_axis(axes_list[-1], result)
    axes_list[0].set_title(title, loc="left")
    figure.tight_layout()
    return figure


def memory_pressure_chart(result):
    host = result.host
    panels = []
    for metric, label, rules in (("swap.out_pps", "swap-out pages/s", ("swap_out",)),
                                 ("paging.scan_direct_ps", "direct reclaim pages/s", ("direct_reclaim",)),
                                 ("paging.majflt_ps", "major faults/s", ("major_faults",))):
        series = host.get(metric)
        if series is not None:
            panels.append((label, [(series, label)], rules))
    return small_multiples(result, panels, "Memory pressure indicators")


def psi_chart(result):
    host = result.host
    panels = []
    for resource, some, full, rules in (("CPU %", "psi.cpu_some", None, ("psi_cpu",)),
                                        ("IO %", "psi.io_some", "psi.io_full", ("psi_io_some", "psi_io_full")),
                                        ("memory %", "psi.mem_some", "psi.mem_full", ("psi_mem_some", "psi_mem_full"))):
        lines = []
        some_series = host.get(some)
        if some_series is not None:
            lines.append((some_series, "some"))
        full_series = host.get(full) if full else None
        if full_series is not None:
            lines.append((full_series, "full"))
        if lines:
            panels.append((resource, lines, rules))
    return small_multiples(result, panels, "Pressure stall information (share of time stalled)")
