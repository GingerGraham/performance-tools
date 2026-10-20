"""Data for the Databases section of both reports (spec sections 12.2 and 12.5).

Pure functions that return plain data (strings, numbers, lists, tuples, dicts) so report_md,
report_html and the findings JSON render it without knowing how a database instance is built.
Nothing here writes Markdown or HTML.

    database_sections(result, catalogue=None)   one dict per instance, see instance_section
    instance_section(result, instance, catalogue=None)
    coverage_lines(instance, zone=None, window=None)    spec 12.5, only the lines that apply
    header_line(host, instance)                  the "Database: ..." line of the text output (12.1)
    databases_header_fact(host)                  the ("Databases", labels) header fact, or None
    database_json(instance)                      the entry of the findings.json `databases` list (12.4)

Where the facts come from: the instance (model.DatabaseInstance) for everything phase 4 stored
on it, and the manifests of its runs for what it did not copy (`server_start`,
`path_namespace`, the collector script and version).
"""

import datetime
import statistics

from .dbdetect import ENGINE_NAMES, engine_family, process_seen_share
from .dbengines import SAMPLED, WAIT_CATEGORIES
from .dbload import V2_MISSING, version_text
from .episodes import percentile
from .findings import format_value
from .loader import partition_parent
from .timeutil import clock, duration_text, stamp, stamp_minutes, zone_label

MULTI_DAY_SPAN_S = 20 * 3600

PLACEMENT_HEADERS = ("Role", "Path", "Device", "Media class")
ACTIVITY_HEADERS = ("Metric", "Basis", "Median", "p95")

# Path roles in the order the report lists them (spec 2.6); unknown roles follow, alphabetically.
ROLE_ORDER = ("data", "wal", "redo", "log", "temp", "tablespace", "asm_disk")
# Roles that hold a log, named as the engines do, for the header line.
HEADER_ROLES = (("data", "data"), ("wal", "WAL"), ("redo", "redo"), ("log", "log"))

# (metric, label, unit) rows of the activity summary (spec 12.2).
ACTIVITY_METRICS = (
    ("db.statements_ps", "Statements per second", "/s"),
    ("db.transactions_ps", "Transactions per second", "/s"),
    ("db.sessions_active", "Active sessions", "sessions"),
) + tuple(("db.wait." + category, "Sessions waiting: %s" % category.replace("_", " "), "sessions")
          for category in WAIT_CATEGORIES) + (
    ("db.read_latency_ms", "Read latency", "ms"),
    ("db.commit_latency_ms", "Commit latency", "ms"),
    ("db.cache_hit_pct", "Cache hit", "%"),
)

# What each engine's server process is called, for the process-only lines (spec 12.1, 12.5).
PROGRAM_NAMES = {"postgresql": "postgres", "mysql": "mysqld", "mariadb": "mariadbd", "mssql": "sqlservr"}
COLLECTOR_SCRIPTS = {"postgresql": "collect-postgresql-metrics.sh", "mysql": "collect-mysql-metrics.sh",
                     "oracle": "collect-oracle-metrics.sh", "mssql": "collect-mssql-metrics.sh"}
OFF_VALUES = ("off", "0", "false", "no")
MAX_RESETS_LISTED = 5
MAX_PROCESS_KEYS_LISTED = 8


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def engine_name(instance):
    return ENGINE_NAMES.get(instance.engine, instance.engine)


def subject(instance):
    """How a coverage line names its instance: 'PostgreSQL pg:5432', 'Oracle ORCL/PDB1', 'SQL Server MSSQLSERVER'.

    PostgreSQL and MySQL are known by their port, which the short name carries with its prefix;
    Oracle and SQL Server are known by the instance name alone.
    """
    if instance.engine in ("postgresql", "mysql", "mariadb"):
        return "%s %s" % (engine_name(instance), instance.short)
    return "%s %s" % (engine_name(instance), instance.short.split(":", 1)[-1])


def program_name(instance):
    """'mysqld' for a process-only MySQL; Oracle is known by its SID."""
    if instance.engine == "oracle":
        return "Oracle %s" % instance.instance_name if instance.instance_name else "Oracle"
    return PROGRAM_NAMES.get(instance.engine, engine_name(instance))


def engine_and_version(instance):
    version = version_text(instance.version)
    return "%s %s" % (engine_name(instance), version) if version else engine_name(instance)


# ---------------------------------------------------------------------------
# Small accessors on what phase 4 stored
# ---------------------------------------------------------------------------
def manifest_value(instance, key):
    """The last non-empty value of a manifest key over the instance's runs (later runs win)."""
    found = ""
    for run in instance.runs:
        if run.manifest.get(key):
            found = run.manifest[key]
    return found


def server_start(instance):
    """`server_start` as the collector wrote it ('2026-10-15 08:00:00+00'), or ''."""
    return manifest_value(instance, "server_start")


def collector_text(instance):
    """'collect-postgresql-metrics.sh 3.0.0, schema 3', or '' for a process-only instance."""
    if instance.source != "collector" or not instance.runs:
        return ""
    script = manifest_value(instance, "script") or COLLECTOR_SCRIPTS.get(engine_family(instance.engine), "collector")
    version = manifest_value(instance, "script_version")
    text = "%s %s" % (script, version) if version else script
    return "%s, schema %d" % (text, instance.collector_schema)


def window_covered(instance):
    """(first, last) sample epochs over every series of the instance, or None without series."""
    firsts = [series.times[0] for series in instance.series.values() if series.times]
    lasts = [series.times[-1] for series in instance.series.values() if series.times]
    if not firsts:
        return None
    return min(firsts), max(lasts)


def setting_is_off(instance, name):
    return instance.settings.get(name, "").strip().lower() in OFF_VALUES


def has_waits(instance):
    return any(instance.get("db.wait." + category) is not None for category in WAIT_CATEGORIES)


def sampled_wait_interval(instance):
    """Seconds between wait snapshots when the waits are sampled, else None."""
    if not has_waits(instance):
        return None
    kinds = [kind for metric, kind in instance.basis.items() if metric.startswith("db.wait.")]
    if SAMPLED not in kinds:
        return None
    if instance.interval_s:
        return instance.interval_s
    for category in WAIT_CATEGORIES:
        series = instance.get("db.wait." + category)
        if series is not None and series.interval_s:
            return series.interval_s
    return 0


def clock_texts(epochs, zone, window=None):
    """Clock times of `epochs`; with dates when the window spans days or the epochs fall on different days."""
    dates = {datetime.datetime.fromtimestamp(epoch, zone).date() for epoch in epochs}
    long_window = window is not None and (window[1] - window[0]) > MULTI_DAY_SPAN_S
    if long_window or len(dates) > 1:
        return [stamp_minutes(epoch, zone) for epoch in epochs]
    return [clock(epoch, zone) for epoch in epochs]


def join_and(items):
    if len(items) <= 1:
        return "".join(items)
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


# ---------------------------------------------------------------------------
# Coverage lines (spec 12.5)
# ---------------------------------------------------------------------------
def v2_collector_line(instance):
    """The 2.0.0 line, worded as dbload.add_schema_note words its note but with this module's subject."""
    script = manifest_value(instance, "script") or "the collector"
    version = manifest_value(instance, "script_version") or "2.0.0"
    return "%s: collected with %s %s. %s Re-run with 3.0.0." % (
        subject(instance), script, version, V2_MISSING.get(instance.engine, ""))


def reset_line(instance, zone, window):
    times = clock_texts(instance.resets[:MAX_RESETS_LISTED], zone, window)
    extra = len(instance.resets) - MAX_RESETS_LISTED
    if len(instance.resets) == 1:
        return "%s: statistics reset or server restart at %s; rates are blank for that sample." % (
            subject(instance), times[0])
    listed = join_and(times) + (" and %d more" % extra if extra > 0 else "")
    return "%s: statistics reset or server restart at %s; rates are blank for those samples." % (
        subject(instance), listed)


def coverage_lines(instance, zone=None, window=None):
    """The coverage sentences that apply to one instance, in the order of spec 12.5.

    `zone` formats the reset times (default UTC); `window` is the report's (start, end), which
    adds dates to them when it spans days.
    """
    zone = zone if zone is not None else datetime.timezone.utc
    name = subject(instance)
    lines = []
    recorded_settings = instance.collector_schema >= 3
    if instance.engine == "postgresql" and recorded_settings and setting_is_off(instance, "track_io_timing"):
        lines.append("%s: track_io_timing is off, so read latency was not measured." % name)
    interval = sampled_wait_interval(instance)
    if interval is not None:
        lines.append("%s: wait events are sampled every %d s, not averaged; short waits between samples are not "
                     "visible." % (name, interval))
    if instance.engine in ("mysql", "mariadb") and recorded_settings and setting_is_off(instance, "performance_schema"):
        lines.append("%s: performance_schema is off, so InnoDB file IO latency was not measured." % name)
    if instance.source == "collector" and instance.collector_schema < 3:
        lines.append(v2_collector_line(instance))
    container = instance.settings.get("con_name", "")
    if instance.engine == "oracle" and container and container.upper() != "CDB$ROOT":
        lines.append("%s: connected to a PDB; instance-wide figures need a CDB$ROOT connection." % name)
    if instance.engine == "mssql" and manifest_value(instance, "path_namespace") == "container":
        lines.append("%s: file paths are inside the container and were mapped to host devices through the mount "
                     "table only when the paths matched." % name)
    if instance.source == "process":
        script = COLLECTOR_SCRIPTS.get(engine_family(instance.engine), "the collector")
        lines.append("%s: process data only. Database-level findings need %s." % (program_name(instance), script))
    if instance.resets:
        lines.append(reset_line(instance, zone, window))
    return lines


# ---------------------------------------------------------------------------
# File placement
# ---------------------------------------------------------------------------
def names_below(hardware, name, depth=0):
    """`name` with its kernel and display names and everything it sits on, partitions' whole disks included."""
    found = {name}
    if depth > 8:
        return found
    disk = hardware.find_disk(name)
    if disk is not None:
        found.update((disk.kname, disk.display))
        for slave in disk.slaves:
            found |= names_below(hardware, slave, depth + 1)
    parent = partition_parent(name, hardware)
    if parent and parent != name:
        found |= names_below(hardware, parent, depth + 1)
    return found


def chain_text(hardware, name, depth=0):
    """'pg-data (dm-2) on nvme1n1' through report_common.device_chain; a partition 'on' its disk."""
    from .report_common import device_chain     # not at the top: report_common may import this module

    disk = hardware.find_disk(name)
    if disk is not None:
        return device_chain(hardware, disk.kname)
    parent = partition_parent(name, hardware)
    if parent and parent != name and depth < 8:
        return "%s on %s" % (name, chain_text(hardware, parent, depth + 1))
    return name


def chains_text(hardware, names):
    """Chains of the mapped device names, leaving out names an earlier chain already shows."""
    texts, covered = [], set()
    for name in names:
        if name in covered:
            continue
        texts.append(chain_text(hardware, name))
        covered |= names_below(hardware, name)
    return ", ".join(texts)


def leaf_classes(hardware, name, depth=0):
    """Media classes of the physical devices below `name` (a stacked device has none of its own)."""
    disk = hardware.find_disk(name)
    if disk is None:
        parent = partition_parent(name, hardware)
        if parent and parent != name and depth < 8:
            return leaf_classes(hardware, parent, depth + 1)
        return []
    found = []
    if disk.slaves and depth < 8:
        for slave in disk.slaves:
            for media_class in leaf_classes(hardware, slave, depth + 1):
                if media_class not in found:
                    found.append(media_class)
    return found or [disk.media_class]


def media_text(hardware, names):
    found = []
    for name in names:
        for media_class in leaf_classes(hardware, name):
            if media_class not in found:
                found.append(media_class)
    return ", ".join(found) or "unknown"


def ordered_roles(instance):
    known = [role for role in ROLE_ORDER if role in instance.paths]
    return known + sorted(role for role in instance.paths if role not in ROLE_ORDER)


def placement(host, instance):
    """(rows, note): rows are (role, path, device chain, media class) as PLACEMENT_HEADERS; note says why there are none or few.

    Each path is mapped again with dbload.map_path rather than read from instance.devices, which
    holds one list per role and cannot say which path is on which device.
    """
    from . import dbload

    if instance.local is False:
        return [], ("remote server: its file paths belong to the database host, not to the host the OS data comes "
                    "from, so they are not placed on devices")
    if not instance.paths:
        return [], "no database file locations were recorded"
    hardware = host.hardware
    disk_names = dbload.disk_names_of(host) if hardware is not None else set()
    rows = []
    for role in ordered_roles(instance):
        for path in instance.paths[role]:
            names = dbload.map_path(path, hardware, disk_names)[0] if hardware is not None else []
            if names:
                rows.append((role, path, chains_text(hardware, names), media_text(hardware, names)))
            else:
                rows.append((role, path, "not placed", "-"))
    if hardware is None:
        return rows, "no hardware inventory: files could not be placed on devices"
    return rows, ""


# ---------------------------------------------------------------------------
# Activity summary and settings
# ---------------------------------------------------------------------------
def activity_summary(instance):
    """[{metric, label, unit, basis, median, p95}] for the metrics of spec 12.2 the instance has."""
    rows = []
    for metric, label, unit in ACTIVITY_METRICS:
        series = instance.get(metric)
        values = series.finite_values() if series is not None else []
        if not values:
            continue
        rows.append({"metric": metric, "label": label, "unit": unit, "basis": instance.basis.get(metric, ""),
                     "median": statistics.median(values), "p95": percentile(values, 95)})
    return rows


def activity_rows(summary):
    """The summary as table rows of text, as ACTIVITY_HEADERS."""
    return [(row["label"], row["basis"] or "-", format_value(row["median"], row["unit"]),
             format_value(row["p95"], row["unit"])) for row in summary]


def settings_rows(instance):
    """[(setting, value)] sorted by name: the `setting.*` keys the collector recorded."""
    return sorted(instance.settings.items())


# ---------------------------------------------------------------------------
# Header line, facts, section
# ---------------------------------------------------------------------------
def device_phrase(instance):
    """'data on nvme1n1, WAL on nvme1n1'; roles with no mapped device are left out."""
    parts = []
    for role, text in HEADER_ROLES:
        names = instance.devices.get(role)
        if names:
            parts.append("%s on %s" % (text, "/".join(names)))
    return ", ".join(parts)


def location_text(instance):
    """'on this host (socket)', 'remote', 'location unknown'; a basis of 'none' (a remote server) is not shown."""
    if instance.local is True:
        text = "on this host"
    elif instance.local is False:
        text = "remote"
    else:
        text = "location unknown"
    if instance.locality_basis and instance.locality_basis != "none":
        text += " (%s)" % instance.locality_basis
    return text


def locality_word(instance):
    if instance.local is True:
        return "local"
    if instance.local is False:
        return "remote"
    return "location unknown"


def header_line(host, instance):
    """'Database: PostgreSQL 16.4 (pg:5432), collector, local, data on nvme1n1, WAL on nvme1n1' (spec 12.1)."""
    if instance.source == "collector":
        parts = ["%s (%s)" % (engine_and_version(instance), instance.short), "collector", locality_word(instance)]
    else:
        parts = ["%s (process data only)" % program_name(instance)]
        share = process_seen_share(host, instance.process_keys)
        if share is not None:
            parts.append("seen in %d %% of pidstat samples" % round(100 * share))
    devices = device_phrase(instance)
    if devices:
        parts.append(devices)
    return "Database: " + ", ".join(parts)


def databases_header_fact(host):
    """The ('Databases', 'PostgreSQL 16.4 (localhost:5432); ...') header fact, or None without instances."""
    if not host.databases:
        return None
    return ("Databases", "; ".join(instance.label for instance in host.databases))


def resets_text(instance, zone, window):
    if not instance.resets:
        return "none"
    times = clock_texts(instance.resets[:MAX_RESETS_LISTED], zone, window)
    more = len(instance.resets) - MAX_RESETS_LISTED
    return "%d (at %s)" % (len(instance.resets), join_and(times) + (" and %d more" % more if more > 0 else ""))


def facts(result, instance):
    """[(label, value)] shown at the top of an instance's section."""
    host = result.host
    zone = host.zone
    window = (host.start, host.end) if host.start is not None else None
    rows = [("Engine and version", engine_and_version(instance)),
            ("Source", "collector run" if instance.source == "collector" else "process data only (no collector run)"),
            ("Location", location_text(instance))]
    collector = collector_text(instance)
    if collector:
        rows.append(("Collector", collector))
    if instance.source == "collector":
        every = "every %d s" % instance.interval_s if instance.interval_s else "interval unknown"
        rows.append(("Samples", "%d samples, %s" % (instance.sample_count, every)))
    covered = window_covered(instance)
    if covered is not None:
        rows.append(("Window covered", "%s to %s %s (%s)" % (
            stamp(covered[0], zone), stamp(covered[1], zone), zone_label(zone, covered[0]),
            duration_text(covered[1] - covered[0]))))
    start = server_start(instance)
    if start:
        rows.append(("Server start", start))
    if instance.source == "collector":
        rows.append(("Resets in window", resets_text(instance, zone, window)))
    if instance.process_keys:
        text = ", ".join(instance.process_keys[:MAX_PROCESS_KEYS_LISTED])
        share = process_seen_share(host, instance.process_keys)
        if share is not None:
            text += "; seen in %d %% of pidstat samples" % round(100 * share)
        rows.append(("Processes", text))
    return rows


def section_notes(instance):
    """The instance's notes without the 2.0.0 line, which coverage_lines words itself."""
    return [note for note in instance.notes if ": collected with " not in note or "Re-run with" not in note]


def instance_section(result, instance, catalogue=None):
    """Everything the Databases section shows for one instance.

    Keys: id, short, label, engine, header_line, facts [(label, value)], placement_headers,
    placement_rows [(role, path, device chain, media class)], placement_note, activity
    [{metric, label, unit, basis, median, p95}], activity_headers, activity_rows (text),
    settings [(name, value)], charts [chart names; needs the catalogue], coverage [sentences],
    notes [instance notes not already covered].
    """
    from .dbcharts import charts_for_instance

    host = result.host
    window = (host.start, host.end) if host.start is not None else None
    rows, note = placement(host, instance)
    summary = activity_summary(instance)
    return {
        "id": instance.id,
        "short": instance.short,
        "label": instance.label,
        "engine": instance.engine,
        "header_line": header_line(host, instance),
        "facts": facts(result, instance),
        "placement_headers": PLACEMENT_HEADERS,
        "placement_rows": rows,
        "placement_note": note,
        "activity": summary,
        "activity_headers": ACTIVITY_HEADERS,
        "activity_rows": activity_rows(summary),
        "settings": settings_rows(instance),
        "charts": charts_for_instance(catalogue, instance) if catalogue is not None else [],
        "coverage": coverage_lines(instance, host.zone, window),
        "notes": section_notes(instance),
    }


def database_sections(result, catalogue=None):
    """One section dict per instance, in host.databases order; [] when the host has none."""
    return [instance_section(result, instance, catalogue) for instance in result.host.databases]


# ---------------------------------------------------------------------------
# findings.json
# ---------------------------------------------------------------------------
def database_json(instance):
    """The `databases` entry of findings.json for one instance (spec 12.4)."""
    return {
        "id": instance.id,
        "short": instance.short,
        "engine": instance.engine,
        "label": instance.label,
        "source": instance.source,
        "local": instance.local,
        "version_num": instance.version_num,
        "collector_schema": instance.collector_schema,
        "devices": {role: list(names) for role, names in instance.devices.items()},
        "resets": list(instance.resets),
        "settings": dict(instance.settings),
        "notes": list(instance.notes),
    }
