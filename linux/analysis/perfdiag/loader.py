"""Builds a HostData from the runs of one host in a bundle."""

import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter

from . import canonical
from .model import HostData, Series
from .readers import kernel_log, pidstat, psar, sadf
from .readers.manifest import parse_hardware, read_key_values
from .timeutil import dst_change_date, resolve_zone

# Same list as SADF_EXPORTS in gather-perf-diag.sh. Each entry is the file name and
# the argument lists to try in order. sysstat 10.x takes -r without ALL.
SADF_EXPORTS = (
    ("cpu", [["-u", "ALL", "-P", "ALL"]]),
    ("load", [["-q"]]),
    ("psi_cpu", [["-q", "CPU"]]),
    ("psi_io", [["-q", "IO"]]),
    ("psi_mem", [["-q", "MEM"]]),
    ("cswch", [["-w"]]),
    ("memory", [["-r", "ALL"], ["-r"]]),
    ("paging", [["-B"]]),
    ("swap_space", [["-S"]]),
    ("swap_rate", [["-W"]]),
    ("disk", [["-d", "-p"]]),
    ("filesystem", [["-F"]]),
    ("net_dev", [["-n", "DEV"]]),
    ("net_edev", [["-n", "EDEV"]]),
    ("net_tcp", [["-n", "TCP"]]),
    ("net_etcp", [["-n", "ETCP"]]),
    ("net_sock", [["-n", "SOCK"]]),
    ("net_soft", [["-n", "SOFT"]]),
    ("interrupts", [["-I", "ALL"]]),
    ("fchost", [["-n", "FC"]]),
)


class LoadError(Exception):
    pass


# Whole disk of a partition name, for bundles without part.* keys.
PARTITION_PATTERNS = (
    re.compile(r"^((?:sd|vd|xvd|hd)[a-z]+)\d+$"),
    re.compile(r"^(nvme\d+n\d+)p\d+$"),
    re.compile(r"^(mmcblk\d+)p\d+$"),
    re.compile(r"^(md\d+)p\d+$"),
)
MAX_LISTED_PARTITIONS = 10


def partition_parent(name, hardware):
    """Whole disk a partition belongs to, or None when `name` is not a partition."""
    if hardware is not None:
        parent = hardware.parent_of(name)
        if parent:
            return parent
    for pattern in PARTITION_PATTERNS:
        match = pattern.match(name)
        if match:
            return match.group(1)
    return None


def fold_partitions(host):
    """Moves disk series of partitions to host.partition_series and notes it.

    A partition whose parent disk has no series of its own stays: it is then
    the only data for that disk.
    """
    disk_devices = {key[2] for key in host.series if key[1].startswith("disk.")}
    moved = set()
    for key in list(host.series):
        source, metric, device = key
        if not metric.startswith("disk."):
            continue
        parent = partition_parent(device, host.hardware)
        if parent is None or parent not in disk_devices:
            continue
        host.partition_series[key] = host.series.pop(key)
        moved.add(device)
    if moved:
        names = sorted(moved)
        listed = ", ".join(names[:MAX_LISTED_PARTITIONS])
        if len(names) > MAX_LISTED_PARTITIONS:
            listed += " and %d more" % (len(names) - MAX_LISTED_PARTITIONS)
        host.note("partitions excluded from analysis (their parent disk is analysed): " + listed)


def _int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def export_with_local_sadf(sa_file, out_dir):
    """Runs the local sadf over an sa file, as gather-perf-diag.sh would have done."""
    sadf_path = shutil.which("sadf")
    if not sadf_path:
        raise LoadError(
            "%s has no sadf/ exports and this machine has no sadf. Re-run gather-perf-diag.sh on the "
            "server (sysstat is needed there), or install sysstat here." % os.path.basename(sa_file))
    env = dict(os.environ, LC_ALL="C")
    produced = 0
    for name, alternatives in SADF_EXPORTS:
        for arguments in alternatives:
            result = subprocess.run([sadf_path, "-d", "-U", sa_file, "--"] + arguments,
                                    capture_output=True, text=True, env=env)
            if result.returncode == 0 and any(not line.startswith("#") for line in result.stdout.splitlines() if line):
                with open(os.path.join(out_dir, name + ".csv"), "w") as handle:
                    handle.write(result.stdout)
                produced += 1
                break
    if produced == 0:
        version = subprocess.run([sadf_path, "-V"], capture_output=True, text=True).stdout.splitlines()
        header = subprocess.run([sadf_path, "-H", sa_file], capture_output=True, text=True)
        raise LoadError(
            "local %s could not read %s (it is probably older than the sadc that wrote it).\n%s"
            % (version[0] if version else "sadf", os.path.basename(sa_file),
               (header.stdout or header.stderr).strip()[:400]))


def merge_series(parts):
    """One Series from several runs' Series with the same key. Later runs win on a clash."""
    merged = {}
    for part in parts:
        merged.update(dict(zip(part.times, part.values)))
    times = sorted(merged)
    first = parts[0]
    interval = Counter(part.interval_s for part in parts).most_common(1)[0][0]
    return Series(first.metric, first.device, times, [merged[t] for t in times], interval, first.source)


def find_gaps(host, reference_metric="cpu.idle_pct"):
    """(start, end) pairs where consecutive samples are further apart than 2 x interval."""
    reference = host.get(reference_metric, "all") or host.get("load.avg1")
    if reference is None:
        for series in host.series.values():
            reference = series
            break
    gaps = []
    if reference is None or reference.interval_s <= 0:
        return gaps
    limit = 2 * reference.interval_s
    for previous, current in zip(reference.times, reference.times[1:]):
        if current - previous > limit:
            gaps.append((previous, current - reference.interval_s))
    return gaps


def load_host(bundle, host_name, tz_override=None, include_partitions=False):
    host = HostData(host=host_name)
    for note in bundle.notes:
        host.note(note)
    runs = bundle.runs_for(host_name)
    host.runs = runs
    os_runs = [run for run in runs if run.tag == "os"]
    psar_runs = [run for run in runs if run.tag == "psar"]
    hw_runs = sorted((run for run in runs if run.tag == "hw"), key=lambda run: run.name)

    if not os_runs and not psar_runs:
        raise LoadError("host %s has no os or psar run to analyse" % host_name)

    reference_manifest = (os_runs or psar_runs)[0].manifest
    host.zone, zone_note = resolve_zone(reference_manifest.get("timezone", ""), reference_manifest.get("tz_name", ""))
    if tz_override:
        host.zone = tz_override
    elif zone_note:
        host.note(zone_note)
    host.cpus = _int(reference_manifest.get("cpus"))
    host.mem_total_kb = _int(reference_manifest.get("mem_total_kb"))
    host.distro = reference_manifest.get("distro", "")
    host.kernel = reference_manifest.get("kernel", "")
    host.psi_expected = reference_manifest.get("psi", "no") == "yes"
    host.sysstat_version = (os_runs[0].manifest.get("sysstat_version", "") if os_runs else "")

    parts = {}
    intervals = []
    temp_dirs = []
    try:
        for run in os_runs:
            interval = _int(run.manifest.get("interval_s")) or 0
            intervals.append(interval)
            tables = _load_sadf_tables(run, host, temp_dirs)
            host.activities = sorted(set(host.activities) | set(tables))
            host.data_sources.append("sadf: %s" % run.name)
            for series in canonical.series_from_sadf(tables, interval, host):
                parts.setdefault((series.source, series.metric, series.device), []).append(series)
            for notes_text in (run.manifest.get("notes", ""),):
                for item in filter(None, (piece.strip() for piece in notes_text.split("|"))):
                    host.note(item)
            for name in sorted(os.listdir(run.path)):
                if name.startswith("pidstat_") and name.endswith(".log"):
                    host.process_rows.extend(pidstat.read_pidstat_file(os.path.join(run.path, name), host.zone))
        for run in psar_runs:
            interval = _int(run.manifest.get("interval_s")) or 0
            intervals.append(interval)
            tables = psar.read_psar_directory(run.path)
            host.data_sources.append("psar: %s" % run.name)
            for series in canonical.series_from_psar(tables, interval):
                parts.setdefault((series.source, series.metric, series.device), []).append(series)
            for item in filter(None, (piece.strip() for piece in run.manifest.get("notes", "").split("|"))):
                host.note(item)
    finally:
        for directory in temp_dirs:
            shutil.rmtree(directory, ignore_errors=True)

    for key, pieces in parts.items():
        host.series[key] = merge_series(pieces)

    distinct = sorted(set(value for value in intervals if value))
    host.interval_s = distinct[0] if distinct else _median_interval(host)
    if len(distinct) > 1:
        host.note("runs use different sample intervals (%s s); each series keeps its own" %
                  ", ".join(str(v) for v in distinct))

    if hw_runs:
        hardware_path = os.path.join(hw_runs[-1].path, "hardware.txt")
        if os.path.isfile(hardware_path):
            host.hardware = parse_hardware(read_key_values(hardware_path))
            host.data_sources.append("inventory: %s" % hw_runs[-1].name)
            if host.hardware.cpu_logical:
                host.cpus = host.hardware.cpu_logical
            if host.hardware.mem_total_kb:
                host.mem_total_kb = host.hardware.mem_total_kb
            for item in filter(None, (p.strip() for p in hw_runs[-1].manifest.get("notes", "").split("|"))):
                host.note("inventory: " + item)
    if host.hardware is None:
        host.note("no hardware inventory: disk thresholds use defaults and the storage layout is unknown")

    if not include_partitions:
        fold_partitions(host)

    _fill_window(host)
    host.gaps = find_gaps(host)

    if bundle.kernel_log_path and host.start is not None:
        year_hint = _year_of(host.start, host.zone)
        _, events = kernel_log.read_kernel_log(bundle.kernel_log_path, host.zone, year_hint, host.end)
        host.kernel_events = [e for e in events if host.start - 600 <= e.time <= host.end + 600]
        prepare_kernel_events(host)
    return host


def event_disks(device, host):
    """Names of the whole disks a kernel-log device name applies to."""
    if not device:
        return []
    hardware = host.hardware
    parent = partition_parent(device, hardware)
    if parent:
        device = parent
    if re.fullmatch(r"nvme\d+", device):
        pattern = re.compile(r"^%sn\d+$" % re.escape(device))
        known = {key[2] for key in host.series if key[1].startswith("disk.")}
        if hardware is not None:
            known.update(hardware.disks)
        return sorted(name for name in known if pattern.match(name))
    names = [device]
    disk = hardware.find_disk(device) if hardware is not None else None
    if disk is not None:
        for name in (disk.kname, disk.display):
            if name and name not in names:
                names.append(name)
    return names


def prepare_kernel_events(host):
    """Fills event.disks, then drops removable and loop devices with one host note."""
    kept, ignored = [], []
    for event in host.kernel_events:
        event.disks = event_disks(event.device, host)
        (ignored if event.ignored else kept).append(event)
    host.kernel_events = kept
    if ignored:
        names = sorted({event.device for event in ignored})
        host.note("%d kernel IO messages on removable or loop devices (%s) were ignored" % (
            sum(event.count for event in ignored), ", ".join(names)))


def _load_sadf_tables(run, host, temp_dirs):
    sadf_dir = os.path.join(run.path, "sadf")
    if os.path.isdir(sadf_dir):
        tables = sadf.read_sadf_directory(sadf_dir)
        if tables:
            return tables
    sa_files = sorted(name for name in os.listdir(run.path) if name.startswith("sa_"))
    if not sa_files:
        raise LoadError("%s has neither sadf/ exports nor an sa_ file" % run.name)
    temp_dir = tempfile.mkdtemp(prefix="perfdiag-sadf-")
    temp_dirs.append(temp_dir)
    export_with_local_sadf(os.path.join(run.path, sa_files[0]), temp_dir)
    host.note("%s: no sadf/ exports in the bundle; converted with the local sadf" % run.name)
    return sadf.read_sadf_directory(temp_dir)


def _median_interval(host):
    counts = Counter(series.interval_s for series in host.series.values() if series.interval_s)
    return counts.most_common(1)[0][0] if counts else 0


def _fill_window(host):
    starts, ends = [], []
    for series in host.series.values():
        if series.times:
            starts.append(series.times[0] - series.interval_s)
            ends.append(series.times[-1])
    host.start = min(starts) if starts else None
    host.end = max(ends) if ends else None
    _refresh_dst_note(host)


def _refresh_dst_note(host):
    """A note when the window crosses a daylight-saving change; dropped again if --from/--to cut it out."""
    if host.dst_note in host.notes:
        host.notes.remove(host.dst_note)
    host.dst_note = ""
    changed = dst_change_date(host.zone, host.start, host.end)
    if changed:
        host.dst_note = ("the window crosses a daylight-saving change on %s; times are shown in local time on "
                         "each side of it" % changed)
        host.note(host.dst_note)


def _year_of(epoch, zone):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, zone).year


def apply_window(host, start, end):
    """Clip every series, process row and event to [start, end] (epoch). None = open."""
    if start is None and end is None:
        return host
    for key, series in list(host.series.items()):
        host.series[key] = series.clipped(start, end)
    for key, series in list(host.partition_series.items()):
        host.partition_series[key] = series.clipped(start, end)
    host.process_rows = [row for row in host.process_rows
                         if (start is None or row.time >= start) and (end is None or row.time <= end)]
    host.kernel_events = [event for event in host.kernel_events
                          if (start is None or event.time >= start) and (end is None or event.time <= end)]
    _fill_window(host)
    host.gaps = find_gaps(host)
    return host
