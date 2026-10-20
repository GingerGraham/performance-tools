"""Loads database instances into a HostData (spec sections 8.1, 8.2 and 8.5).

`load_databases(host, db_runs)` is called by loader.load_host after the hardware inventory and
the pidstat logs are loaded. It

1. reads every DB collector run and groups the runs of one target into one instance
   (source "collector"),
2. finds database server processes in the pidstat rows and the hardware inventory
   (dbdetect) and attaches them to a local collector instance of the same engine, or keeps
   them as instances of their own (source "process"),
3. maps the instance's file paths to the host's disk devices (map_path),
4. stores the result in host.databases. Database series never go into host.series.

Short names (used as the indicator device and in the text tables):

    PostgreSQL          pg:<port>          pg:5432        (db_instance "localhost:5432")
    MySQL / MariaDB     my:<port>          my:3306
    Oracle              ora:<instance>     ora:ORCL/PDB1  (instance name / container name)
    SQL Server          mssql:<instance>   mssql:MSSQLSERVER
    process only        pg:proc, my:proc, mssql:proc, ora:<SID>

When two instances of one host would get the same short name (the same port on two remote
servers), the later one uses the full db_instance text (pg:dbhost2:5432).
"""

import os
import re

from . import dbdetect, dbengines
from .dbdetect import ENGINE_NAMES, engine_family
from .model import DatabaseInstance, Series
from .readers import dbcsv
from .readers.manifest import read_key_values

DB_TAGS = ("postgresql", "mysql", "oracle", "mssql")
PATH_KEY = re.compile(r"^path\.([a-z_]+)\.(\d+)$")
PORT_AT_END = re.compile(r":(\d+)$")
VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+")
NOTE_SEPARATOR = "|"
MAX_UNMAPPED_PATHS_SHOWN = 3

# What a 2.0.0 collector could not record, per engine (spec 12.5 gives the PostgreSQL text).
V2_MISSING = {
    "postgresql": "Database counters cover the postgres database only; waits, IO timing and file placement "
                  "are not available.",
    "mysql": "InnoDB file IO latency, replica lag and file placement are not available.",
    "mariadb": "InnoDB file IO latency, replica lag and file placement are not available.",
    "oracle": "The time model, wait events and file placement are not available.",
    "mssql": "Wait statistics, file IO latency and file placement are not available.",
}


# ---------------------------------------------------------------------------
# Path to device mapping (spec 8.5)
# ---------------------------------------------------------------------------
def _expand_device(name, hardware, disk_names, found, depth=0):
    """Adds `name`, its display name, its whole disk and the devices below it, when they are in the disk data."""
    from .loader import partition_parent

    if depth > 8 or not name:
        return
    names = [name]
    disk = hardware.find_disk(name)
    if disk is not None:
        names = [disk.kname, disk.display]
    parent = partition_parent(name, hardware)
    if parent:
        parent_disk = hardware.find_disk(parent)
        names += [parent, parent_disk.display if parent_disk is not None else ""]
    for candidate in names:
        if candidate and candidate in disk_names and candidate not in found:
            found.append(candidate)
    if disk is not None:
        for slave in disk.slaves:
            _expand_device(slave, hardware, disk_names, found, depth + 1)


def map_path(path, hardware, disk_names):
    """(device names, problem) for a path on the database server's file system.

    The names are those of the disk data (`disk_names`), the filesystem's own device first, then
    the devices below it (dm, md, partitions' whole disks). `problem` is a note when the path
    cannot be resolved by design, otherwise "". No filesystem access: the path belongs to the server.
    """
    if hardware is None or not path:
        return [], ""
    if path.startswith("AFD:"):
        return [], "ASM filter driver label %s cannot be mapped to a device" % path
    if path.startswith("+"):
        return [], ""                       # an ASM disk group; its disks are listed as asm_disk paths
    normalised = os.path.normpath(path)
    found = []
    if normalised.startswith("/dev/"):
        if normalised.startswith("/dev/oracleasm/"):
            return [], "oracleasm disk %s cannot be mapped to a device" % path
        _expand_device(normalised.rsplit("/", 1)[-1], hardware, disk_names, found)
        return found, ""
    best = None
    for filesystem in hardware.filesystems:
        mountpoint = filesystem.mountpoint
        if not mountpoint:
            continue
        prefix = mountpoint if mountpoint.endswith("/") else mountpoint + "/"
        if normalised == mountpoint or normalised.startswith(prefix):
            if best is None or len(mountpoint) > len(best.mountpoint):
                best = filesystem
    if best is not None and best.kname:
        _expand_device(best.kname, hardware, disk_names, found)
    return found, ""


def disk_names_of(host):
    """Names of the devices that have disk series."""
    return set(host.devices("disk.read_kBps")) | set(host.devices("disk.tps"))


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def run_engine(run):
    """db_engine from the manifest (mysql or mariadb for the mysql tag), else the run tag (2.0.0 runs)."""
    engine = run.manifest.get("db_engine", "")
    return engine if engine in ENGINE_NAMES else run.tag


def _int_or(text, default):
    return int(text) if (text or "").isdigit() else default


def v2_instance_text(run):
    """db_instance for a 2.0.0 run, from its `target` key (user@host:port, user@server/database)."""
    target = run.manifest.get("target", "")
    target = target.split("@", 1)[-1]
    if run_engine(run) == "mssql":
        target = target.split("/", 1)[0]
    return target


def instance_text_of(run):
    return run.manifest.get("db_instance") or v2_instance_text(run)


def short_name(engine, instance_text):
    port = PORT_AT_END.search(instance_text)
    if engine == "postgresql":
        prefix = "pg"
    elif engine in ("mysql", "mariadb"):
        prefix = "my"
    elif engine == "oracle":
        prefix = "ora"
    else:
        prefix = "mssql"
    if engine in ("postgresql", "mysql", "mariadb") and port:
        return "%s:%s" % (prefix, port.group(1))
    return "%s:%s" % (prefix, instance_text) if instance_text else prefix


def version_text(raw):
    """The dotted number out of a server_version text, else the text cut to a readable length."""
    found = VERSION_TOKEN.search(raw or "")
    if found:
        return found.group(0)
    return (raw or "").strip()[:30] if (raw or "").strip() not in ("", "unknown") else ""


def label_for(engine, raw_version, instance_text):
    parts = [ENGINE_NAMES.get(engine, engine)]
    version = version_text(raw_version)
    if version:
        parts.append(version)
    label = " ".join(parts)
    return "%s (%s)" % (label, instance_text) if instance_text else label


def locality_of(manifest):
    """(local, basis) from target_local and target_local_basis; a 2.0.0 run is judged by its target host."""
    value = manifest.get("target_local", "")
    if value == "yes":
        return True, manifest.get("target_local_basis", "")
    if value == "no":
        return False, manifest.get("target_local_basis", "")
    if value == "unknown":
        return None, manifest.get("target_local_basis", "")
    host_part = manifest.get("target", "").split("@", 1)[-1]
    host_part = host_part.rsplit(":", 1)[0] if PORT_AT_END.search(host_part) else host_part.split("/", 1)[0]
    if host_part.startswith(("localhost", "127.", "::1", "[::1]")):
        return True, "loopback"
    return None, ""


# ---------------------------------------------------------------------------
# Reading runs
# ---------------------------------------------------------------------------
def read_paths(run):
    """{role: [paths]} and a list of path notes from a run's db_paths.txt."""
    path_file = os.path.join(run.path, "db_paths.txt")
    paths, notes = {}, []
    if not os.path.isfile(path_file):
        return paths, notes
    values = read_key_values(path_file)
    numbered = []
    for key, value in values.items():
        found = PATH_KEY.match(key)
        if found and value:
            numbered.append((found.group(1), int(found.group(2)), value))
    for role, _, value in sorted(numbered):
        paths.setdefault(role, []).append(value)
    for key, value in values.items():
        if key.startswith("path.") and key.endswith(".note") and value:
            notes.append("%s path: %s" % (key.split(".")[1], value))
    return paths, notes


def read_tables(run, engine):
    """({file name: table}, notes) for the data files present in a run directory."""
    tables, notes = {}, []
    for name, key_columns in dbengines.files_for(engine):
        path = os.path.join(run.path, name)
        if not os.path.isfile(path):
            continue
        table = dbcsv.read_wide_csv(path) if key_columns is None else dbcsv.read_long_csv(path, key_columns)
        tables[name] = table
        notes.extend(table.problems(name))
    return tables, notes


def minimums_from(settings):
    """dbengines.Minimums from a thresholds.Settings (or the built-in defaults when None)."""
    from .thresholds import DEFAULTS

    def number(key):
        if settings is not None:
            return settings.float(key, float(DEFAULTS[key]))
        return float(DEFAULTS[key])

    return dbengines.Minimums(reads_ps=number("db.min_reads_ps"), commits_ps=number("db.min_commits_ps"),
                              cache_reads_ps=number("db.cache_hit.min_reads_ps"))


def merge_run_series(pieces):
    """One Series from the same series of several runs of an instance. A later run wins on a clash."""
    merged = {}
    for piece in pieces:
        merged.update(dict(zip(piece.times, piece.values)))
    times = sorted(merged)
    return Series(pieces[0].metric, pieces[0].device, times, [merged[t] for t in times], pieces[0].interval_s, "db")


def split_notes(text):
    return [piece.strip() for piece in (text or "").split(NOTE_SEPARATOR) if piece.strip()]


def build_collector_instance(host, runs, minimums):
    """One DatabaseInstance from the runs of one target (sorted by name, so later runs win)."""
    runs = sorted(runs, key=lambda run: run.name)
    first = runs[0]
    engine = run_engine(first)
    manifest = {}
    for run in runs:
        manifest.update(run.manifest)
    if manifest.get("db_engine") in ENGINE_NAMES:
        engine = manifest["db_engine"]
    instance_text = instance_text_of(first)
    local, basis = locality_of(manifest)
    # A run without collector_schema is a 2.0.0 run.
    schema = max(_int_or(run.manifest.get("collector_schema"), 2) for run in runs)
    version_number = manifest.get("server_version_num", "")
    instance = DatabaseInstance(
        id="%s:%s" % (engine, instance_text or first.host),
        short=short_name(engine, instance_text),
        engine=engine,
        label=label_for(engine, manifest.get("server_version", ""), instance_text),
        source="collector",
        local=local,
        version=manifest.get("server_version", ""),
        version_num=_int_or(version_number, None),
        collector_schema=schema,
        runs=list(runs),
        settings={key[len("setting."):]: value for key, value in manifest.items() if key.startswith("setting.")},
        instance_name=_instance_name(engine, instance_text),
        locality_basis=basis,
        interval_s=_int_or(manifest.get("interval_s"), 0),
    )

    for run in runs:
        run_paths, path_notes = read_paths(run)
        for role, found in run_paths.items():
            known = instance.paths.setdefault(role, [])
            known.extend(path for path in found if path not in known)
        for text in path_notes:
            instance.note(text)
        for text in split_notes(run.manifest.get("notes", "")):
            instance.note(text)

    disk_names = disk_names_of(host)
    device_cache = {}

    def file_device(path):
        """Device of a file in the disk data; only for a local server, whose paths are this host's."""
        if instance.local is not True or host.hardware is None:
            return ""
        if path not in device_cache:
            names, _ = map_path(path, host.hardware, disk_names)
            device_cache[path] = names[0] if names else ""
        return device_cache[path]

    pieces = {}
    resets = set()
    for run in runs:
        tables, table_notes = read_tables(run, engine)
        for text in table_notes:
            instance.note(text)
        for name in tables:
            if name not in instance.tables_present:
                instance.tables_present.append(name)
        stats = tables.get(dbengines.STATS_FILE[engine_family(engine)])
        if stats is None or len(stats) == 0:
            instance.note("%s is missing or has no rows in %s; no database series from this run" % (
                dbengines.STATS_FILE[engine_family(engine)], run.name))
        else:
            instance.sample_count += len(stats)
        run_interval = _int_or(run.manifest.get("interval_s"), 0)
        output = dbengines.adapt(dbengines.EngineInput(
            engine=engine, tables=tables, manifest=run.manifest, settings=instance.settings,
            interval_s=run_interval, host_cpus=host.cpus, local=local, minimums=minimums, file_device=file_device))
        for key, series in output.series.items():
            pieces.setdefault(key, []).append(series)
        for metric, kind in output.basis.items():
            instance.basis.setdefault(metric, kind)
        resets.update(output.resets)
        for text in output.notes:
            instance.note(text)
        host.data_sources.append("db: %s" % run.name)
    for key, parts in pieces.items():
        instance.series[key] = merge_run_series(parts)
    instance.resets = sorted(resets)
    return instance


def _instance_name(engine, instance_text):
    if engine == "oracle":
        return instance_text.split("/", 1)[0]
    if engine == "mssql":
        return instance_text
    return ""


def add_schema_note(instance):
    """The 2.0.0 coverage text (spec 12.5), once the short name is final."""
    if instance.collector_schema >= 3 or instance.source != "collector":
        return
    script = instance.runs[0].manifest.get("script", "the collector")
    version = instance.runs[0].manifest.get("script_version", "2.0.0")
    instance.note("%s %s: collected with %s %s. %s Re-run with 3.0.0." % (
        ENGINE_NAMES.get(instance.engine, instance.engine), instance.short, script, version,
        V2_MISSING.get(instance.engine, "")))


# ---------------------------------------------------------------------------
# Process data: merge with collector instances
# ---------------------------------------------------------------------------
def _same_server(first, second):
    """Same engine family and, for Oracle, the same SID (when both name one)."""
    if engine_family(first.engine) != engine_family(second.engine):
        return False
    if first.engine == "oracle" and first.instance_name and second.instance_name:
        return first.instance_name.lower() == second.instance_name.lower()
    return True


def _share_data_directory(first, second):
    return any(dbdetect.same_directory(left, right)
               for left in first.paths.get("data", []) for right in second.paths.get("data", []))


def _copy_process_data(source, target):
    """Process keys, roles and (when the target has none) the data directory from `source` to `target`."""
    for key in source.process_keys:
        if key not in target.process_keys:
            target.process_keys.append(key)
    for command, role in source.process_roles.items():
        target.process_roles.setdefault(command, role)
    if source.paths.get("data") and not target.paths.get("data"):
        target.paths["data"] = list(source.paths["data"])
    if source.instance_name and not target.instance_name:
        target.instance_name = source.instance_name
    for text in source.notes:
        target.note(text)


def _choose_targets(source, candidates):
    """Candidates a process source belongs to: all of one, or the ones sharing its data directory.

    Returns (targets, ambiguous). With several candidates and no data directory in common the
    source goes to all of them and `ambiguous` is True.
    """
    if len(candidates) <= 1:
        return candidates, False
    matching = [candidate for candidate in candidates if _share_data_directory(source, candidate)]
    if matching:
        return matching, False
    return candidates, True


def merge_process_instances(collectors, pidstat_instances, inventory_instances):
    """Attaches process data to local collector instances; returns the instances left as process-only.

    Inventory entries and pidstat matches of one server are combined first (the inventory knows
    the data directory and SID, pidstat knows the process keys), then attached by spec 8.1 rule 3.
    """
    candidates = list(inventory_instances)
    for seen in pidstat_instances:
        same = [item for item in inventory_instances if _same_server(item, seen)]
        if not same:
            candidates.append(seen)
            continue
        targets, ambiguous = _choose_targets(seen, same)
        for target in targets:
            _copy_process_data(seen, target)

    process_only = []
    for candidate in candidates:
        local_collectors = [item for item in collectors if item.local is True and _same_server(item, candidate)]
        if not local_collectors:
            process_only.append(candidate)
            continue
        targets, ambiguous = _choose_targets(candidate, local_collectors)
        for target in targets:
            _copy_process_data(candidate, target)
            if ambiguous:
                target.note("process data could not be split between the %s instances on this host" %
                            ENGINE_NAMES.get(target.engine, target.engine))
    return process_only


def make_ids_and_shorts_unique(instances):
    """Gives every instance of a host a distinct id and short name."""
    used_ids, used_shorts = set(), set()
    for instance in instances:
        if instance.id in used_ids:
            suffix = instance.paths.get("data", [""])[0] or str(len(used_ids))
            instance.id = "%s:%s" % (instance.id, suffix)
        used_ids.add(instance.id)
        short = instance.short
        if short in used_shorts and instance.source == "collector":
            # The same port on two servers: name the instance by its full db_instance text.
            short = short.split(":", 1)[0] + ":" + instance.id.split(":", 1)[1]
        base, number = short, 2
        while short in used_shorts:
            short = "%s-%d" % (base, number)
            number += 1
        instance.short = short
        used_shorts.add(short)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
def map_instance_devices(host, instance):
    """Fills instance.devices from instance.paths, with notes for what cannot be placed."""
    if instance.local is False or not instance.paths:
        return
    engine_name = ENGINE_NAMES.get(instance.engine, instance.engine)
    if host.hardware is None:
        instance.note("no hardware inventory: %s files could not be placed on devices" % engine_name)
        return
    disk_names = disk_names_of(host)
    if not disk_names:
        return
    for role, paths in instance.paths.items():
        found, unmapped = [], []
        for path in paths:
            names, problem = map_path(path, host.hardware, disk_names)
            if problem:
                instance.note(problem)
            elif not names and not path.startswith("+"):
                unmapped.append(path)
            for name in names:
                if name not in found:
                    found.append(name)
        if found:
            instance.devices[role] = found
        if unmapped and not found:
            shown = ", ".join(unmapped[:MAX_UNMAPPED_PATHS_SHOWN])
            more = len(unmapped) - MAX_UNMAPPED_PATHS_SHOWN
            instance.note("%s path%s %s%s could not be matched to a disk in the data" % (
                role, "" if len(unmapped) == 1 else "s", shown, " and %d more" % more if more > 0 else ""))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
ENGINE_ORDER = {"postgresql": 0, "mysql": 1, "mariadb": 1, "oracle": 2, "mssql": 3}


def load_databases(host, db_runs, settings=None):
    """Fills host.databases from DB collector runs, pidstat rows and the hardware inventory."""
    minimums = minimums_from(settings)
    groups = {}
    for run in db_runs:
        key = (engine_family(run_engine(run)), instance_text_of(run))
        groups.setdefault(key, []).append(run)
    collectors = [build_collector_instance(host, runs, minimums) for runs in groups.values()]
    collectors.sort(key=lambda item: (ENGINE_ORDER.get(item.engine, 9), item.id))

    pidstat_instances = dbdetect.instances_from_processes(host)
    inventory_instances = dbdetect.instances_from_inventory(host.hardware)
    process_only = merge_process_instances(collectors, pidstat_instances, inventory_instances)
    process_only.sort(key=lambda item: (ENGINE_ORDER.get(item.engine, 9), item.id))

    instances = collectors + process_only
    make_ids_and_shorts_unique(instances)
    for instance in instances:
        add_schema_note(instance)
        map_instance_devices(host, instance)
    host.databases = instances
