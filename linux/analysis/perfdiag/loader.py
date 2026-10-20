"""Builds a HostData from the runs of one host in a bundle."""

import os
import shutil
import subprocess
import tempfile
from collections import Counter

from . import canonical
from .model import HostData, Series
from .readers import kernel_log, pidstat, psar, sadf
from .readers.manifest import parse_hardware, read_key_values
from .timeutil import zone_from_manifest

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


def load_host(bundle, host_name, tz_override=None):
    host = HostData(host=host_name)
    runs = bundle.runs_for(host_name)
    host.runs = runs
    os_runs = [run for run in runs if run.tag == "os"]
    psar_runs = [run for run in runs if run.tag == "psar"]
    hw_runs = sorted((run for run in runs if run.tag == "hw"), key=lambda run: run.name)

    if not os_runs and not psar_runs:
        raise LoadError("host %s has no os or psar run to analyse" % host_name)

    reference_manifest = (os_runs or psar_runs)[0].manifest
    host.zone = zone_from_manifest(reference_manifest.get("timezone", ""))
    if tz_override:
        host.zone = tz_override
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

    _fill_window(host)
    host.gaps = find_gaps(host)

    if bundle.kernel_log_path and host.start is not None:
        year_hint = _year_of(host.start, host.zone)
        _, events = kernel_log.read_kernel_log(bundle.kernel_log_path, host.zone, year_hint, host.end)
        host.kernel_events = [e for e in events if host.start - 600 <= e.time <= host.end + 600]
    return host


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


def _year_of(epoch, zone):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, zone).year


def apply_window(host, start, end):
    """Clip every series, process row and event to [start, end] (epoch). None = open."""
    if start is None and end is None:
        return host
    for key, series in list(host.series.items()):
        host.series[key] = series.clipped(start, end)
    host.process_rows = [row for row in host.process_rows
                         if (start is None or row.time >= start) and (end is None or row.time <= end)]
    host.kernel_events = [event for event in host.kernel_events
                          if (start is None or event.time >= start) and (end is None or event.time <= end)]
    _fill_window(host)
    host.gaps = find_gaps(host)
    return host
