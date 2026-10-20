"""Database charts (spec section 12.3).

Five charts per database instance, named `db_<kind>_<short>` with the instance's short name
made file-safe (`db_waits_pg_5432`):

    activity    statements/s or transactions/s, active sessions on a second axis
    waits       stacked area of the db.wait.* categories, cpu at the bottom
    latency     db.read_latency_ms and db.commit_latency_ms; for a local instance whose files
                are mapped to devices, the data device's read await and the log device's
                write await on the same axis
    cache       cache hit ratio and physical reads
    processes   CPU and IO of the instance's processes by role, from pidstat

A chart exists only when its data does (the "when" column of the spec); an instance with no
data for any of them adds nothing to the catalogue, and a host without databases is
untouched. `charts.chart_catalogue` appends `database_charts(result)`.

The look is that of charts.py (colours, finish, episode bands, gaps broken with with_gaps).
Two additions are specific to databases: a dotted vertical line where a statistics reset or
server restart blanked the rates, and the title of a chart carries the basis of its data
(sampled, or averaged over each interval) because a snapshot and an average mean different
things on the same axis.

The report layer picks an instance's charts with `charts_for_instance`.
"""

import math
import statistics

from . import charts
from .attribution import process_key
from .charts import (GRID, INK_SECONDARY, SERIES_COLORS, STATUS_CRITICAL, STATUS_WARNING, ChartRef, episode_bands,
                     episodes_for, finish, line_series, new_figure, prepare, safe_name, threshold_line, time_axis,
                     to_local, with_gaps)
from .dbdetect import process_role
from .dbengines import SAMPLED, WAIT_CATEGORIES
from .findings import format_threshold_number
from .model import Series

KIND_ACTIVITY = "activity"
KIND_WAITS = "waits"
KIND_LATENCY = "latency"
KIND_CACHE = "cache"
KIND_PROCESSES = "processes"
KINDS = (KIND_ACTIVITY, KIND_WAITS, KIND_LATENCY, KIND_CACHE, KIND_PROCESSES)
NAME_PREFIXES = tuple("db_%s_" % kind for kind in KINDS)

# Which charts belong with which D code (spec section 11.1); None of them is shown twice by the
# report layer, which tracks the names it has already drawn.
KINDS_FOR_CODE = {
    "D1": (KIND_LATENCY, KIND_CACHE, KIND_WAITS),
    "D2": (KIND_LATENCY, KIND_WAITS),
    "D3": (KIND_WAITS, KIND_ACTIVITY),
    "D4": (KIND_WAITS, KIND_PROCESSES),
    "D5": (KIND_WAITS, KIND_PROCESSES, KIND_ACTIVITY),
    "D6": (KIND_LATENCY, KIND_ACTIVITY, KIND_WAITS),
    "D7": (KIND_WAITS,),
    "D8": (KIND_ACTIVITY, KIND_WAITS),
    "D9": (KIND_ACTIVITY,),
}

# Indicators whose episodes are drawn as bands on each chart (spec 10.1 rule names).
WAIT_RULES = ("db_commit_wait", "db_lock_wait", "db_latch_wait", "db_io_wait", "db_cpu_demand", "db_memory_grants",
              "db_worker_queue")
LATENCY_RULES = ("db_read_latency", "db_commit_latency")
CACHE_RULES = ("db_cache_hit",)

# Fallback thresholds (spec 10.2) for when the settings object has no db.* key yet.
DEFAULT_LATENCY_MS = (10.0, 30.0)
DEFAULT_CACHE_HIT_PCT = (95.0, 90.0)          # warn, crit: the indicator fires below them

# Roles that hold a log: PostgreSQL wal, MySQL and Oracle redo, SQL Server log.
LOG_ROLES = ("wal", "redo", "log")
DATA_ROLES = ("data",)

# One colour per wait category. The first six are the chart series colours in their usual order;
# the rest are muted hues that stay clear of the status colours (amber and red mean thresholds).
WAIT_COLORS = {
    "cpu": SERIES_COLORS[0],
    "io_read": SERIES_COLORS[2],
    "io_write": SERIES_COLORS[4],
    "commit": SERIES_COLORS[1],
    "lock": SERIES_COLORS[3],
    "latch": SERIES_COLORS[5],
    "memory": "#b5538f",
    "workers": "#738a2f",
    "parallel": "#7da0e0",
    "network": "#8c8a85",
    "replica": "#4d7a68",
    "temp": "#d6a07a",
    "other": "#bdbbb4",
}
WAIT_LABELS = {
    "cpu": "CPU", "io_read": "IO read", "io_write": "IO write", "commit": "commit", "lock": "lock",
    "latch": "latch", "memory": "memory", "workers": "workers", "parallel": "parallel", "network": "network",
    "replica": "replica", "temp": "temp", "other": "other",
}

MAX_RESET_MARKS = 8                 # more than this and the lines hide the data
CPU_LINE_MIN_SHARE = 0.3            # draw the CPU-count line when the waits reach this share of it
MAX_ROLE_LINES = 5                  # process roles drawn as their own line; the rest are summed
LEGEND_HEADROOM = 1.3               # room above the data for a legend that sits inside the axes


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def chart_name(kind, instance):
    return "db_%s_%s" % (kind, safe_name(instance.short))


def chart_kind(name):
    """'latency' for 'db_latency_pg_5432'; '' for any other chart."""
    for kind in KINDS:
        if name.startswith("db_%s_" % kind):
            return kind
    return ""


def latency_chart_name(instance):
    """The chart an H hypothesis with database context links to (spec 12.2)."""
    return chart_name(KIND_LATENCY, instance)


def charts_for_instance(catalogue, instance, code=None):
    """Names of an instance's charts in `catalogue`, in report order.

    With a D code, only the charts that belong with it (KINDS_FOR_CODE); without, all of them.
    """
    present = {chart.name for chart in catalogue}
    kinds = KINDS if code is None else KINDS_FOR_CODE.get(code, KINDS)
    names = [chart_name(kind, instance) for kind in KINDS if kind in kinds]
    return [name for name in names if name in present]


# ---------------------------------------------------------------------------
# What each chart needs (the "when" column)
# ---------------------------------------------------------------------------
def wait_series(instance):
    """[(category, series)] of the instance-wide wait series, cpu first (WAIT_CATEGORIES order)."""
    found = []
    for category in WAIT_CATEGORIES:
        series = instance.get("db.wait." + category)
        if series is not None:
            found.append((category, series))
    return found


def activity_rate_series(instance):
    """[(label, series)] of the statement and transaction rates the instance has."""
    found = []
    for metric, label in (("db.statements_ps", "statements/s"), ("db.transactions_ps", "transactions/s")):
        series = instance.get(metric)
        if series is not None:
            found.append((label, series))
    return found


def process_rows_of(host, instance):
    """Non-thread pidstat rows of the programs this instance owns."""
    if not instance.process_keys:
        return []
    keys = set(instance.process_keys)
    return [row for row in host.process_rows if not row.is_thread and process_key(row.command) in keys]


def applicable_kinds(host, instance):
    kinds = []
    if activity_rate_series(instance):
        kinds.append(KIND_ACTIVITY)
    if wait_series(instance):
        kinds.append(KIND_WAITS)
    if instance.get("db.read_latency_ms") is not None or instance.get("db.commit_latency_ms") is not None:
        kinds.append(KIND_LATENCY)
    if instance.get("db.cache_hit_pct") is not None:
        kinds.append(KIND_CACHE)
    if process_rows_of(host, instance):
        kinds.append(KIND_PROCESSES)
    return kinds


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
def tags_for(kind):
    return tuple(code for code, kinds in KINDS_FOR_CODE.items() if kind in kinds)


def database_charts(result):
    """ChartRefs for every instance of the host; [] when there are none."""
    host = result.host
    refs = []
    for instance in getattr(host, "databases", []):
        for kind in applicable_kinds(host, instance):
            title, builder = _title_and_builder(result, instance, kind)
            refs.append(ChartRef(chart_name(kind, instance), title, builder, tags_for(kind)))
    return refs


def _title_and_builder(result, instance, kind):
    if kind == KIND_ACTIVITY:
        return activity_title(instance), lambda: activity_chart(result, instance)
    if kind == KIND_WAITS:
        return waits_title(instance), lambda: waits_chart(result, instance)
    if kind == KIND_LATENCY:
        return latency_title(result, instance), lambda: latency_chart(result, instance)
    if kind == KIND_CACHE:
        return cache_title(instance), lambda: cache_chart(result, instance)
    return processes_title(instance), lambda: processes_chart(result, instance)


# ---------------------------------------------------------------------------
# Shared drawing helpers
# ---------------------------------------------------------------------------
def basis_of(instance, prefix):
    """'sampled' when any metric starting with `prefix` is a snapshot; else the first basis found or ''."""
    kinds = [kind for metric, kind in instance.basis.items() if metric.startswith(prefix)]
    if SAMPLED in kinds:
        return SAMPLED
    return kinds[0] if kinds else ""


def sample_interval_s(instance, series_list):
    """The collector's interval, else the series' own."""
    if instance.interval_s:
        return instance.interval_s
    for series in series_list:
        if series.interval_s:
            return series.interval_s
    return 0


def reset_markers(axes, result, instance):
    """Dotted vertical lines where a restart or statistics reset blanked the rates."""
    zone = result.host.zone
    for epoch in instance.resets[:MAX_RESET_MARKS]:
        when = to_local(epoch, zone)
        axes.axvline(when, color=INK_SECONDARY, linewidth=0.9, linestyle=(0, (1, 2)), zorder=2)
        axes.annotate("reset", xy=(when, 0), xycoords=("data", "axes fraction"), xytext=(2, 3),
                      textcoords="offset points", fontsize=7, color=INK_SECONDARY, rotation=90, va="bottom")


def reference_line(axes, result, series, label, color, scale=1.0):
    """An operating-system series drawn thinner than the database's own, as the reference for it.

    Solid, not dashed: dashes mean thresholds on these charts.
    """
    times, values = prepare(series.times, series.values, series.interval_s, result.host.zone, scale)
    axes.plot(times, values, color=color, linewidth=1.2, alpha=0.9, label=label, zorder=3)


def panel_figure(result, instance, panels, title, resets=True):
    """Several panels sharing the time axis, one y-axis each (the chart convention for mixed scales).

    panels: list of dicts with `label` (y axis), `lines` [(series, label, color, scale)],
    `thresholds` [(value, label, colour)], `rules` (indicator names whose episodes are shaded),
    `ylim` (bottom, top) to override the automatic range. `resets` False leaves the reset lines
    out (they say nothing about pidstat data).
    """
    charts._load_matplotlib()
    host = result.host
    figure, axes_list = charts._plt.subplots(len(panels), 1, figsize=(10, 1.9 * len(panels) + 0.6), dpi=110,
                                             sharex=True, squeeze=False)
    axes_list = [row[0] for row in axes_list]
    any_threshold = False
    for axes, panel in zip(axes_list, panels):
        axes.set_axisbelow(True)
        axes.grid(True, color=GRID, linewidth=0.7)
        for side in ("top", "right"):
            axes.spines[side].set_visible(False)
        for series, label, color, scale in panel["lines"]:
            line_series(axes, result, series, label, color, scale)
        axes.set_ylim(bottom=0)
        top = axes.get_ylim()[1]
        for value, label, color in panel.get("thresholds", ()):
            threshold_line(axes, value, label, color)
            top = max(top, value * 1.15)
            any_threshold = True
        legend = len(panel["lines"]) > 1
        axes.set_ylim(top=max(top * (LEGEND_HEADROOM if legend else 1.0), 1e-9))
        if panel.get("ylim"):
            axes.set_ylim(*panel["ylim"])
        axes.set_ylabel(panel["label"], fontsize=8)
        if legend:
            axes.legend(loc="upper left", frameon=False, fontsize=8, ncol=3)
        if resets:
            reset_markers(axes, result, instance)
        episode_bands(axes, result, episodes_for(result, panel.get("rules", ()), instance.short), host.zone)
    time_axis(axes_list[-1], result)
    axes_list[0].set_title(title, loc="left")
    figure.tight_layout()
    if any_threshold:
        figure.subplots_adjust(right=0.86)
    return figure


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------
def activity_title(instance):
    sessions = instance.get("db.sessions_active")
    text = "%s activity" % instance.short
    if sessions is not None and instance.basis.get("db.sessions_active") == SAMPLED:
        text += " (sessions are sampled, not averaged)"
    return text


def activity_chart(result, instance):
    figure, axes = new_figure()
    rates = activity_rate_series(instance)
    for number, (label, series) in enumerate(rates):
        line_series(axes, result, series, label, SERIES_COLORS[number])
    axes.set_ylim(bottom=0)
    axes.set_ylabel("per second")
    handles, _ = axes.get_legend_handles_labels()
    sessions = instance.get("db.sessions_active")
    if sessions is not None:
        # Sessions are a count, not a rate: their own axis, so a busy server's rate does not flatten them.
        side = axes.twinx()
        side.spines["top"].set_visible(False)
        times, values = prepare(sessions.times, sessions.values, sessions.interval_s, result.host.zone)
        color = SERIES_COLORS[len(rates)]
        line, = side.plot(times, values, color=color, linewidth=1.6, label="active sessions (right axis)", zorder=3)
        side.set_ylim(bottom=0)
        side.set_ylabel("active sessions")
        handles.append(line)
    for drawn_axes in figure.axes:
        drawn_axes.set_ylim(top=drawn_axes.get_ylim()[1] * LEGEND_HEADROOM)      # room for the legend
    axes.legend(handles=handles, loc="upper left", frameon=False, fontsize=8, ncol=3)
    reset_markers(axes, result, instance)
    return finish(figure, axes, result, activity_title(instance), "per second")


# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------
def waits_title(instance):
    """The title is the caption: it states the basis of the figures (spec 12.3)."""
    series = [item for _, item in wait_series(instance)]
    if instance.engine == "mssql":
        return "%s tasks waiting, by category (wait time is per task: parallel tasks are counted individually)" % (
            instance.short)
    if basis_of(instance, "db.wait.") == SAMPLED:
        interval = sample_interval_s(instance, series)
        every = "every %d s" % interval if interval else "at each sample"
        return "%s sessions waiting, by category (sampled %s, not averaged)" % (instance.short, every)
    return "%s sessions waiting, by category (average over each interval)" % instance.short


def stack_rows(plan, interval_s):
    """[(time, [value per category]) or (time, None) for a gap] on the union of the series' times.

    A category with no value at a time counts as 0 there; a time at which every category is NaN
    (a reset, which blanks all rates) stays NaN so the area is broken, not drawn as zero.
    """
    by_category = [dict(zip(series.times, series.values)) for _, series in plan]
    times = sorted({time_value for lookup in by_category for time_value in lookup})
    rows = []
    for time_value in times:
        values = [lookup.get(time_value, math.nan) for lookup in by_category]
        if all(math.isnan(value) for value in values):
            rows.append((time_value, [math.nan] * len(values)))
        else:
            rows.append((time_value, [0.0 if math.isnan(value) else value for value in values]))
    # Break the area where the data has a hole, as the line charts do.
    positions, markers = with_gaps(times, list(range(len(times))), interval_s)
    expanded = []
    for time_value, position in zip(positions, markers):
        if math.isnan(position):
            expanded.append((time_value, [math.nan] * len(plan)))
        else:
            expanded.append(rows[int(position)])
    return expanded


def thin_rows(rows, limit=charts.MAX_POINTS):
    """Keeps the tallest stack of each bucket, and every break, so peaks survive drawing."""
    if len(rows) <= limit:
        return rows
    size = int(math.ceil(len(rows) / float(limit // 2)))
    kept = []
    for first in range(0, len(rows), size):
        chunk = rows[first:first + size]
        solid = [row for row in chunk if not math.isnan(row[1][0])]
        kept.extend(row for row in chunk if math.isnan(row[1][0]))
        if solid:
            kept.append(max(solid, key=lambda row: sum(row[1])))
    return sorted(kept, key=lambda row: row[0])


def drop_empty_categories(plan):
    """Categories that never rose above zero stay out of the legend."""
    return [(category, series) for category, series in plan if any(value > 0 for value in series.finite_values())]


def waits_chart(result, instance):
    host = result.host
    figure, axes = new_figure(height=3.6)
    everything = wait_series(instance)
    plan = drop_empty_categories(everything)
    if not plan:
        # An empty stacked area would only show an axis; say what the data says instead.
        axes.text(0.5, 0.5, "no sessions waiting in the window", transform=axes.transAxes, ha="center", va="center",
                  color=INK_SECONDARY, fontsize=9)
        axes.set_ylim(0, 1)
        reset_markers(axes, result, instance)
        return finish(figure, axes, result, waits_title(instance), "sessions")
    interval = statistics.median([series.interval_s for _, series in plan if series.interval_s] or [0])
    rows = thin_rows(stack_rows(plan, interval))
    times = [to_local(time_value, host.zone) for time_value, _ in rows]
    stacks = [[values[index] for _, values in rows] for index in range(len(plan))]
    axes.stackplot(times, *stacks, labels=[WAIT_LABELS[category] for category, _ in plan],
                   colors=[WAIT_COLORS[category] for category, _ in plan], linewidth=0, alpha=0.9, zorder=3)
    totals = [sum(values) for _, values in rows if not math.isnan(values[0])]
    peak = max(totals or [0.0])
    top = max(peak * LEGEND_HEADROOM, 1.0)
    cpus = host.cpus
    if instance.local is True and cpus and peak >= CPU_LINE_MIN_SHARE * cpus:
        top = max(top, cpus * 1.15)
        threshold_line(axes, cpus, "%d CPUs" % cpus, INK_SECONDARY)
    axes.set_ylim(0, top)
    axes.legend(loc="upper left", frameon=False, fontsize=8, ncol=4)
    reset_markers(axes, result, instance)
    return finish(figure, axes, result, waits_title(instance), "sessions",
                  episodes_for(result, WAIT_RULES, instance.short))


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------
def first_mapped_series(host, instance, roles, metrics):
    """(device, series, metric) for the first device mapped to one of `roles` that has one of `metrics`.

    Only a local instance has devices (dbload maps nothing for a remote server).
    """
    if instance.local is not True:
        return None
    for role in roles:
        for device in instance.devices.get(role, ()):
            for metric in metrics:
                series = host.get(metric, device)
                if series is not None:
                    return device, series, metric
    return None


def mapped_device(instance, roles):
    for role in roles:
        if instance.devices.get(role):
            return instance.devices[role][0]
    return ""


def device_overlays(result, instance):
    """[(series, label, colour)] of the operating-system latency of the data and log devices."""
    host = result.host
    overlays = []
    data = first_mapped_series(host, instance, DATA_ROLES, ("disk.r_await_ms", "disk.await_ms"))
    if data is not None:
        device, series, metric = data
        overlays.append((series, "%s %s" % (device, "read await" if metric == "disk.r_await_ms" else "await"),
                         SERIES_COLORS[2]))
    log = first_mapped_series(host, instance, LOG_ROLES, ("disk.w_await_ms", "disk.await_ms"))
    if log is not None:
        device, series, metric = log
        overlays.append((series, "%s %s" % (device, "write await" if metric == "disk.w_await_ms" else "await"),
                         SERIES_COLORS[3]))
    return overlays


def latency_thresholds(result, instance, indicator_name, roles, key_prefix):
    """(warn, crit) ms for a latency series: the indicator's, else the mapped device class's, else the settings."""
    indicator = result.indicator(indicator_name, instance.short)
    if indicator is not None and indicator.warn is not None and indicator.crit is not None:
        return float(indicator.warn), float(indicator.crit)
    device = mapped_device(instance, roles) if instance.local is True else ""
    if device:
        profile = result.profile_for(device)
        if profile is None and result.host.hardware is not None:
            from .thresholds import resolve_disk_profile
            profile = resolve_disk_profile(device, result.host.hardware, result.settings)
        if profile is not None:
            return float(profile.warn_ms), float(profile.crit_ms)
    settings = result.settings
    return (settings.float(key_prefix + ".warn_ms", DEFAULT_LATENCY_MS[0]),
            settings.float(key_prefix + ".crit_ms", DEFAULT_LATENCY_MS[1]))


def latency_title(result, instance):
    names = []
    if instance.get("db.read_latency_ms") is not None:
        names.append("read")
    if instance.get("db.commit_latency_ms") is not None:
        names.append("commit")
    text = "%s %s latency (ms)" % (instance.short, " and ".join(names))
    if device_overlays(result, instance):
        text += " against device await"
    return text


def latency_chart(result, instance):
    figure, axes = new_figure()
    drawn = 0
    limits = []
    for metric, label, color, rule, roles, key in (
            ("db.read_latency_ms", "database read latency", SERIES_COLORS[0], "db_read_latency", DATA_ROLES,
             "db.read_latency"),
            ("db.commit_latency_ms", "database commit latency", SERIES_COLORS[1], "db_commit_latency", LOG_ROLES,
             "db.commit_latency")):
        series = instance.get(metric)
        if series is None:
            continue
        line_series(axes, result, series, label, color)
        limits.append((label.split()[1], latency_thresholds(result, instance, rule, roles, key)))
        drawn += 1
    for series, label, color in device_overlays(result, instance):
        reference_line(axes, result, series, label, color)
        drawn += 1
    axes.set_ylim(bottom=0)
    draw_latency_thresholds(axes, limits)
    if drawn > 1:
        axes.set_ylim(top=axes.get_ylim()[1] * 1.15)                             # room for the legend
    reset_markers(axes, result, instance)
    return finish(figure, axes, result, latency_title(result, instance), "ms",
                  episodes_for(result, LATENCY_RULES, instance.short), legend=drawn > 1)


def draw_latency_thresholds(axes, limits):
    """Threshold lines, labelled by series only when read and commit have different ones."""
    if not limits:
        return
    distinct = {pair for _, pair in limits}
    top = axes.get_ylim()[1]
    for name, (warn, crit) in limits:
        prefix = "" if len(distinct) == 1 else name + " "
        threshold_line(axes, warn, "%swarn %s ms" % (prefix, format_threshold_number(warn)), STATUS_WARNING)
        threshold_line(axes, crit, "%scrit %s ms" % (prefix, format_threshold_number(crit)), STATUS_CRITICAL)
        top = max(top, crit * 1.15)
        if len(distinct) == 1:
            break
    axes.set_ylim(top=top)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def cache_title(instance):
    text = "%s buffer cache hit and physical reads" % instance.short
    if instance.basis.get("db.cache_hit_pct") == SAMPLED:
        text += " (hit ratio is sampled, not averaged)"
    return text


def cache_axis_range(series, crit):
    """A hit ratio lives between 90 and 100 %: start the axis 5 points under the lowest value or crit line."""
    low = min(series.finite_values() + [crit])
    return max(0.0, math.floor((low - 5.0) / 5.0) * 5.0), 101.0


def cache_chart(result, instance):
    settings = result.settings
    warn = settings.float("db.cache_hit.warn_pct", DEFAULT_CACHE_HIT_PCT[0])
    crit = settings.float("db.cache_hit.crit_pct", DEFAULT_CACHE_HIT_PCT[1])
    indicator = result.indicator("db_cache_hit", instance.short)
    if indicator is not None and indicator.warn is not None and indicator.crit is not None:
        warn, crit = float(indicator.warn), float(indicator.crit)
    panels = [{
        "label": "cache hit %", "lines": [(instance.get("db.cache_hit_pct"), "cache hit", SERIES_COLORS[0], 1.0)],
        "thresholds": [(warn, "warn %s %%" % format_threshold_number(warn), STATUS_WARNING),
                       (crit, "crit %s %%" % format_threshold_number(crit), STATUS_CRITICAL)],
        "rules": CACHE_RULES, "ylim": cache_axis_range(instance.get("db.cache_hit_pct"), crit),
    }]
    reads = instance.get("db.read_kBps")
    if reads is not None:
        # sysstat-style kB (1024 bytes) shown as MiB/s, like every throughput axis in the report.
        panels.append({"label": "MiB/s", "lines": [(reads, "physical reads", SERIES_COLORS[1], 1.0 / 1024)]})
    return panel_figure(result, instance, panels, cache_title(instance))


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------
def processes_title(instance):
    return "%s processes by role (CPU as %% of one CPU, IO from pidstat)" % instance.short


def role_of(instance, command):
    return instance.process_roles.get(command) or process_role(instance.engine, command)


def role_series(host, instance):
    """{role: (cpu Series, io Series)} summed per pidstat sample, absent rows counting as 0.

    pidstat lists only tasks that did something, so a role missing from a sample was idle in it.
    Roles beyond MAX_ROLE_LINES (by CPU, then IO) are summed into "other roles".
    """
    rows = process_rows_of(host, instance)
    sample_times = sorted({row.time for row in host.process_rows if not row.is_thread})
    index = {time_value: position for position, time_value in enumerate(sample_times)}
    spacings = sorted(b - a for a, b in zip(sample_times, sample_times[1:]))
    interval = spacings[len(spacings) // 2] if spacings else (host.interval_s or 0)
    cpu, io = {}, {}
    for row in rows:
        role = role_of(instance, row.command)
        cpu_values = cpu.setdefault(role, [0.0] * len(sample_times))
        io_values = io.setdefault(role, [0.0] * len(sample_times))
        position = index[row.time]
        if row.cpu_pct == row.cpu_pct and row.cpu_pct > 0:
            cpu_values[position] += row.cpu_pct
        for rate in (row.rd_kBps, row.wr_kBps):
            if rate == rate and rate > 0:
                io_values[position] += rate

    def weight(role):
        return (sum(cpu[role]), sum(io[role]))
    ranked = sorted(cpu, key=weight, reverse=True)
    if len(ranked) > MAX_ROLE_LINES + 1:
        rest = ranked[MAX_ROLE_LINES:]
        ranked = ranked[:MAX_ROLE_LINES] + ["other roles"]
        cpu["other roles"] = [sum(values) for values in zip(*(cpu[role] for role in rest))]
        io["other roles"] = [sum(values) for values in zip(*(io[role] for role in rest))]
    return {role: (Series("process.cpu_pct", role, sample_times, cpu[role], interval, "pidstat"),
                   Series("process.io_kBps", role, sample_times, io[role], interval, "pidstat"))
            for role in ranked}, ranked


def processes_chart(result, instance):
    by_role, ranked = role_series(result.host, instance)
    panels = [
        {"label": "CPU % of one CPU",
         "lines": [(by_role[role][0], role, SERIES_COLORS[number], 1.0) for number, role in enumerate(ranked)]},
        {"label": "IO MiB/s (read + write)",
         "lines": [(by_role[role][1], role, SERIES_COLORS[number], 1.0 / 1024) for number, role in enumerate(ranked)]},
    ]
    return panel_figure(result, instance, panels, processes_title(instance), resets=False)
