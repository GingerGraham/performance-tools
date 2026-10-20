"""Per-engine adapters: raw collector tables -> canonical database series (spec section 9).

Every adapter returns an EngineOutput whose `series` are keyed (metric, sub). `sub` is "" for
the instance as a whole. Other sub values are listed per metric below. Rates are per second
and end at the sample time (model.Series convention); the first sample of a run has no rate.
A counter that goes backwards, a changed start/reset marker, or a gap over 2 x the nominal
interval gives NaN for that sample, and a reset is recorded for the first two causes. At a
reset time every rate-based series is NaN, not only the counter that went backwards.

Metrics (basis: T = time-averaged over the interval, S = sampled at the sample instant)

  db.statements_ps            T  MySQL, Oracle, SQL Server
  db.transactions_ps          T  PostgreSQL, MySQL, Oracle
  db.sessions_active          S  all four
  db.connections_pct          S  all four
  db.wait.<category>          PostgreSQL S, others T; sessions waiting (average or sampled).
                              Categories: cpu io_read io_write commit lock latch memory workers
                              parallel network replica temp other. sub "" is the category total;
                              other subs are the detail names (PostgreSQL "IO:DataFileRead",
                              Oracle event or "class:<name>" remainder, SQL Server wait type).
                              Only categories the engine's data can fill are produced.
  db.read_latency_ms          T  PostgreSQL, MySQL, Oracle, SQL Server (sub = device as well)
  db.commit_latency_ms        T  MySQL, Oracle, SQL Server (sub = device for log files)
  db.cache_hit_pct            T  PostgreSQL, MySQL, Oracle; S for SQL Server
  db.read_kBps, db.write_kBps T  all four (SQL Server: sub = device as well)
  db.temp_kBps                T  PostgreSQL, SQL Server (tempdb file writes)
  db.temp_tables_ps           T  MySQL (on-disk temporary tables)
  db.flush_stalls_ps          T  all four
  db.backend_writes_pct       T  PostgreSQL
  db.dirty_pct                S  MySQL
  db.checkpoint_age_pct       S  MariaDB
  db.lock_wait_avg_ms         T  MySQL, SQL Server
  db.deadlocks_ps             T  PostgreSQL, SQL Server
  db.cpu_demand_per_cpu       PostgreSQL S, Oracle T, SQL Server S
  db.concurrency_per_cpu      S  MySQL
  db.compile_pct              T  Oracle (hard parses), SQL Server (compilations)
  db.memory_grants_pending    S  SQL Server
  db.worker_queue             S  SQL Server
  db.xact_age_s               S  PostgreSQL
  db.history_list             S  MySQL
  db.replication_lag_s        S  PostgreSQL, MySQL
  db.checkpoints              T  counts per interval, not rates; sub "timed" and "requested"
                                 (PostgreSQL), "incomplete" (Oracle log file switch)

Additional series not in the spec table, for the report and the later phases:

  db.database_transactions_ps T  PostgreSQL, sub = database name (pg_database.csv)
  db.sessions_by_class        S  Oracle, sub = state class ("ON CPU", "User I/O", ...)

Minimum denominators (db.min_reads_ps, db.min_commits_ps, db.cache_hit.min_reads_ps) make a
quiet interval NaN instead of a 0 % or 100 % spike. Settings the adapters read from the
manifest (`setting.*`): block_size, max_connections, track_io_timing (PostgreSQL),
max_connections, performance_schema (MySQL), cpu_count (Oracle), scheduler_count,
max_workers_count, cpu_count (SQL Server).
"""

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .model import Series

NAN = float("nan")

TIME_AVERAGED = "time-averaged"
SAMPLED = "sampled"

# Files each engine's collector writes: (file name, key columns or None for a wide table).
ENGINE_FILES = {
    "postgresql": (("pg_stats.csv", None), ("pg_database.csv", ("datname",)),
                   ("pg_waits.csv", ("wait_event_type", "wait_event"))),
    "mysql": (("mysql_stats.csv", None), ("mysql_file_io.csv", ("event_name",))),
    "oracle": (("oracle_stats.csv", None), ("oracle_waits.csv", ("kind", "name")),
               ("oracle_active.csv", ("state_class",))),
    "mssql": (("mssql_stats.csv", None), ("mssql_waits.csv", ("wait_type",)),
              ("mssql_files.csv", ("database", "file_type", "physical_name"))),
}
STATS_FILE = {"postgresql": "pg_stats.csv", "mysql": "mysql_stats.csv", "oracle": "oracle_stats.csv",
              "mssql": "mssql_stats.csv"}

BYTES_PER_KB = 1024.0
# Floors for ratios not covered by the db.min_* settings (events per second).
MIN_BUFFER_WRITES_PS = 1.0
MIN_STATEMENTS_PS = 1.0
DEFAULT_PG_BLOCK_SIZE = 8192

WAIT_CATEGORIES = ("cpu", "io_read", "io_write", "commit", "lock", "latch", "memory", "workers",
                   "parallel", "network", "replica", "temp", "other")


@dataclass
class Minimums:
    """Smallest denominators (per second) a ratio is computed at; the db.min_* settings."""

    reads_ps: float = 10.0
    commits_ps: float = 1.0
    cache_reads_ps: float = 100.0


@dataclass
class EngineInput:
    engine: str                                   # postgresql | mysql | mariadb | oracle | mssql
    tables: Dict[str, object]                     # file name -> WideTable or LongTable
    manifest: Dict[str, str] = field(default_factory=dict)
    settings: Dict[str, str] = field(default_factory=dict)
    interval_s: int = 0                           # nominal sample interval of the run
    host_cpus: Optional[int] = None
    local: Optional[bool] = None
    minimums: Minimums = field(default_factory=Minimums)
    # Device name (in the disk data) a database file path sits on, or "" (SQL Server file statistics).
    file_device: Callable[[str], str] = lambda path: ""


@dataclass
class EngineOutput:
    series: Dict[Tuple[str, str], Series] = field(default_factory=dict)
    basis: Dict[str, str] = field(default_factory=dict)
    resets: List[int] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _finite(value):
    return value == value and value not in (math.inf, -math.inf)


def nominal_interval(times, interval_s):
    """The collector's interval, or the median spacing of the samples when the manifest has none."""
    if interval_s and interval_s > 0:
        return interval_s
    spacings = sorted(b - a for a, b in zip(times, times[1:]) if b > a)
    if not spacings:
        return 0
    return spacings[len(spacings) // 2]


def _counter_steps(times, values, nominal_interval_s):
    """(deltas, seconds, reset_times) for samples 1..n-1.

    delta is NaN where no valid step exists: a missing value, a gap over 2 x the nominal
    interval, or a counter that went backwards (which is also a reset at that sample's time).
    """
    limit = 2 * nominal_interval_s if nominal_interval_s and nominal_interval_s > 0 else math.inf
    deltas, seconds, resets = [], [], []
    for index in range(1, len(times)):
        elapsed = times[index] - times[index - 1]
        before, after = values[index - 1], values[index]
        delta = NAN
        if _finite(before) and _finite(after):
            if after < before:
                resets.append(times[index])
            elif 0 < elapsed <= limit:
                delta = after - before
        deltas.append(delta)
        seconds.append(elapsed)
    return deltas, seconds, resets


def counter_rates(times, values, nominal_interval_s):
    """(times, rates, reset_times) from a cumulative counter.

    The rate for sample i covers (times[i-1], times[i]], so the result starts at the second
    sample. A negative difference is a restart or a statistics reset: that sample is NaN and
    its time is a reset. A gap over 2 x the nominal interval gives NaN, so a rate never spans
    a gap.
    """
    deltas, seconds, resets = _counter_steps(times, values, nominal_interval_s)
    rates = [delta / elapsed if _finite(delta) and elapsed > 0 else NAN for delta, elapsed in zip(deltas, seconds)]
    return list(times[1:]), rates, resets


def counter_deltas(times, values, nominal_interval_s):
    """Like counter_rates, but the increase over each interval (counts, not per second)."""
    deltas, _, resets = _counter_steps(times, values, nominal_interval_s)
    return list(times[1:]), deltas, resets


def delta_ratio(numerators, denominators, minimum_denominator):
    """Element-wise numerator / denominator for two per-second rate lists.

    NaN when either side is NaN or the denominator is below `minimum_denominator` (per second)
    or not positive, so a quiet interval does not produce a 0 % or 100 % spike. The ratio of
    two rates equals the ratio of the two interval deltas.
    """
    result = []
    for numerator, denominator in zip(numerators, denominators):
        if not (_finite(numerator) and _finite(denominator)) or denominator <= 0 or denominator < minimum_denominator:
            result.append(NAN)
        else:
            result.append(numerator / denominator)
    return result


def wait_sessions(times, time_values, unit_per_second, nominal_interval_s):
    """(times, sessions, reset_times): average sessions waiting from a cumulative wait time.

    Delta of the time divided by the elapsed seconds gives seconds of waiting per second,
    which is the average number of sessions waiting over the interval. `unit_per_second` is
    how many of the counter's units make one second (1000 for ms, 1e6 for us).
    """
    result_times, rates, resets = counter_rates(times, time_values, nominal_interval_s)
    return result_times, [rate / unit_per_second if _finite(rate) else NAN for rate in rates], resets


def marker_changes(times, markers):
    """Times at which a marker column (start time, statistics reset time) differs from the sample before."""
    changed = []
    for index in range(1, len(times)):
        before, after = markers[index - 1], markers[index]
        if _finite(before) and _finite(after) and before != after:
            changed.append(times[index])
    return changed


def decreases(times, values):
    """Times at which a gauge that only grows (MySQL Uptime) is lower than in the sample before."""
    found = []
    for index in range(1, len(times)):
        before, after = values[index - 1], values[index]
        if _finite(before) and _finite(after) and after < before:
            found.append(times[index])
    return found


def add_lists(*lists):
    """Element-wise sum; NaN where any input is NaN."""
    result = []
    for values in zip(*lists):
        result.append(sum(values) if all(_finite(value) for value in values) else NAN)
    return result


def sum_available(lists):
    """Element-wise sum of the finite values; NaN where no list has one. Empty lists are ignored."""
    lists = [values for values in lists if values]
    result = []
    for values in zip(*lists):
        finite = [value for value in values if _finite(value)]
        result.append(sum(finite) if finite else NAN)
    return result


def scaled(values, factor):
    return [value * factor if _finite(value) else NAN for value in values]


def divided(numerators, denominators):
    """Element-wise division for gauges; NaN for a missing or zero denominator."""
    result = []
    for numerator, denominator in zip(numerators, denominators):
        if _finite(numerator) and _finite(denominator) and denominator != 0:
            result.append(numerator / denominator)
        else:
            result.append(NAN)
    return result


def _parse_int(text, default=None):
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else default


class SeriesSet:
    """Collects an adapter's series, then blanks rates at resets and drops series without a number."""

    def __init__(self, interval_s):
        self.interval_s = interval_s
        self.entries = {}            # (metric, sub) -> (times, values, is_rate)
        self.basis = {}
        self.resets = set()

    def add_rate(self, metric, sub, times, aligned_values, basis=TIME_AVERAGED):
        """`aligned_values` has one entry per time in `times`; the first has no rate and is dropped."""
        self.entries[(metric, sub)] = (list(times[1:]), list(aligned_values[1:]), True)
        self.basis.setdefault(metric, basis)

    def add_gauge(self, metric, sub, times, values, basis=SAMPLED):
        self.entries[(metric, sub)] = (list(times), list(values), False)
        self.basis.setdefault(metric, basis)

    def values_on(self, metric, sub, times):
        """A stored series' values lined up with the sample `times` (rates regain their NaN first entry)."""
        entry = self.entries.get((metric, sub))
        if entry is None:
            return None
        return ([NAN] + entry[1]) if entry[2] else entry[1]

    def finish(self, notes):
        series = {}
        used_metrics = set()
        for (metric, sub), (times, values, is_rate) in self.entries.items():
            if is_rate and self.resets:
                values = [NAN if time in self.resets else value for time, value in zip(times, values)]
            if not any(_finite(value) for value in values):
                continue
            series[(metric, sub)] = Series(metric, sub, times, values, self.interval_s, "db")
            used_metrics.add(metric)
        basis = {metric: kind for metric, kind in self.basis.items() if metric in used_metrics}
        return EngineOutput(series=series, basis=basis, resets=sorted(self.resets), notes=notes)


class WideRates:
    """Rates and gauges of one wide table, all on the table's own sample times.

    A rate list has one entry per sample time; the first is NaN. Using a column as a counter
    registers its resets.
    """

    def __init__(self, table, nominal_interval_s, resets):
        self.table = table
        self.times = table.times
        self.nominal = nominal_interval_s
        self.resets = resets
        self._rates = {}

    def has(self, *columns):
        return all(self.table.has_values(column) for column in columns)

    def gauge(self, column):
        values = self.table.get(column)
        return list(values) if values is not None else [NAN] * len(self.times)

    def rate(self, column):
        if column not in self._rates:
            values = self.gauge(column)
            _, rates, resets = counter_rates(self.times, values, self.nominal)
            self.resets.update(resets)
            self._rates[column] = [NAN] + rates
        return self._rates[column]

    def delta(self, column):
        values = self.gauge(column)
        _, deltas, resets = counter_deltas(self.times, values, self.nominal)
        self.resets.update(resets)
        return [NAN] + deltas


def long_rates(table, base_times, nominal_interval_s, resets, column):
    """{key: rates on base_times} for one numeric column of a LongTable.

    A key with no row at a sample time has NaN there, so its first appearance does not count
    as growth from zero.
    """
    result = {}
    for key, wide in table.rows_by_key.items():
        values = wide.get(column)
        if values is None:
            continue
        by_time = dict(zip(wide.times, values))
        aligned = [by_time.get(time, NAN) for time in base_times]
        _, rates, key_resets = counter_rates(base_times, aligned, nominal_interval_s)
        resets.update(key_resets)
        result[key] = [NAN] + rates
    return result


def sample_axis(stats, long_tables):
    """Sample times of the summary table; without one, every time any long table has a row."""
    if stats is not None and len(stats) > 0:
        return list(stats.times)
    found = set()
    for table in long_tables:
        if table is not None:
            found.update(table.all_times())
    return sorted(found)


def long_gauges(table, base_times, column):
    """{key: values on base_times}, with 0 where the key has no row at a sample (sampled counts)."""
    result = {}
    for key, wide in table.rows_by_key.items():
        values = wide.get(column)
        if values is None:
            continue
        by_time = dict(zip(wide.times, values))
        found = [by_time.get(time, 0.0) for time in base_times]
        result[key] = [value if _finite(value) else 0.0 for value in found]
    return result


def long_deltas(table, key, base_times, nominal_interval_s, resets, column):
    """Increase of one key's counter over each interval, aligned with base_times (first entry NaN)."""
    wide = table.rows_by_key.get(key)
    values = wide.get(column) if wide is not None else None
    if values is None:
        return [NAN] * len(base_times)
    by_time = dict(zip(wide.times, values))
    aligned = [by_time.get(time, NAN) for time in base_times]
    _, deltas, key_resets = counter_deltas(base_times, aligned, nominal_interval_s)
    resets.update(key_resets)
    return [NAN] + deltas


def _get_table(inputs, name):
    return inputs.tables.get(name)


def capacity_cpus(inputs, engine_setting_names):
    """CPUs the per-CPU metrics divide by (spec 9.2).

    Local instance: the host's count. A setting the engine itself imposes (Oracle cpu_count,
    SQL Server scheduler_count) comes first when `engine_setting_names` lists it. Remote
    instance: the engine's own count only. PostgreSQL and MySQL pass no names, so a remote
    one gets no per-CPU metric.
    """
    for name in engine_setting_names:
        count = _parse_int(inputs.settings.get(name))
        if count:
            return count
    if inputs.local is True and inputs.host_cpus:
        return inputs.host_cpus
    return None


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
def postgres_wait_category(wait_type, wait_event):
    """Category of a pg_stat_activity wait (spec 9.3)."""
    if wait_type == "CPU" and wait_event == "CPU":
        return "cpu"
    if wait_event.upper().startswith("WAL"):
        return "commit"
    if wait_type == "IO":
        return "io_read" if wait_event.endswith("Read") else "io_write"
    if wait_type in ("Lock", "BufferPin"):
        return "lock"
    if wait_type == "LWLock":
        return "latch"
    if wait_type == "IPC":
        return "replica" if wait_event == "SyncRep" else "other"
    if wait_type == "Client":
        return "network"
    return "other"


POSTGRES_WAIT_CATEGORIES = ("cpu", "io_read", "io_write", "commit", "lock", "latch", "network", "replica", "other")


def adapt_postgresql(inputs):
    notes = []
    stats = _get_table(inputs, "pg_stats.csv")
    if stats is None or len(stats) == 0:
        return EngineOutput(notes=notes)
    nominal = nominal_interval(stats.times, inputs.interval_s)
    out = SeriesSet(inputs.interval_s)
    rates = WideRates(stats, nominal, out.resets)
    times = stats.times
    minimums = inputs.minimums
    block_kb = (_parse_int(inputs.settings.get("block_size"), DEFAULT_PG_BLOCK_SIZE)) / BYTES_PER_KB

    if rates.has("xact_commit", "xact_rollback"):
        out.add_rate("db.transactions_ps", "", times, add_lists(rates.rate("xact_commit"), rates.rate("xact_rollback")))
    if rates.has("blks_hit", "blks_read"):
        hits, reads = rates.rate("blks_hit"), rates.rate("blks_read")
        ratio = delta_ratio(hits, add_lists(hits, reads), minimums.cache_reads_ps)
        out.add_rate("db.cache_hit_pct", "", times, scaled(ratio, 100.0))
    if rates.has("blks_read"):
        out.add_rate("db.read_kBps", "", times, scaled(rates.rate("blks_read"), block_kb))
    # With track_io_timing off the counter stays 0, and a latency of 0 ms would be false.
    io_timing_off = inputs.settings.get("track_io_timing", "").lower() == "off"
    if rates.has("blks_read", "blk_read_time_ms") and not io_timing_off:
        out.add_rate("db.read_latency_ms", "", times,
                     delta_ratio(rates.rate("blk_read_time_ms"), rates.rate("blks_read"), minimums.reads_ps))

    write_columns = [column for column in ("bgwriter_buffers_checkpoint", "bgwriter_buffers_clean", "backend_buffer_writes")
                     if rates.has(column)]
    if write_columns:
        total_writes = sum_available([rates.rate(column) for column in write_columns])
        out.add_rate("db.write_kBps", "", times, scaled(total_writes, block_kb))
        if rates.has("backend_buffer_writes") and len(write_columns) == 3:
            share = delta_ratio(rates.rate("backend_buffer_writes"), total_writes, MIN_BUFFER_WRITES_PS)
            out.add_rate("db.backend_writes_pct", "", times, scaled(share, 100.0))
    if rates.has("temp_bytes"):
        out.add_rate("db.temp_kBps", "", times, scaled(rates.rate("temp_bytes"), 1.0 / BYTES_PER_KB))
    if rates.has("backend_buffer_fsyncs"):
        out.add_rate("db.flush_stalls_ps", "", times, rates.rate("backend_buffer_fsyncs"))
    if rates.has("deadlocks"):
        out.add_rate("db.deadlocks_ps", "", times, rates.rate("deadlocks"))
    if rates.has("bgwriter_checkpoints_timed"):
        out.add_rate("db.checkpoints", "timed", times, rates.delta("bgwriter_checkpoints_timed"))
    if rates.has("bgwriter_checkpoints_req"):
        out.add_rate("db.checkpoints", "requested", times, rates.delta("bgwriter_checkpoints_req"))

    if rates.has("active_connections"):
        out.add_gauge("db.sessions_active", "", times, rates.gauge("active_connections"))
    if rates.has("total_connections", "max_connections"):
        share = divided(rates.gauge("total_connections"), rates.gauge("max_connections"))
        out.add_gauge("db.connections_pct", "", times, scaled(share, 100.0))
    if rates.has("oldest_xact_age_s"):
        out.add_gauge("db.xact_age_s", "", times, rates.gauge("oldest_xact_age_s"))
    if rates.has("replication_lag_s"):
        out.add_gauge("db.replication_lag_s", "", times, rates.gauge("replication_lag_s"))

    # Restarts and statistics resets show in the markers even when every counter grew.
    for column in ("server_start_marker", "stats_reset_marker"):
        if rates.has(column):
            out.resets.update(marker_changes(times, rates.gauge(column)))

    waits = _get_table(inputs, "pg_waits.csv")
    if waits is not None:
        _postgres_waits(out, waits, times)
        cpu_sessions = out.values_on("db.wait.cpu", "", times)
        cpus = capacity_cpus(inputs, ())
        if cpu_sessions is not None and cpus:
            out.add_gauge("db.cpu_demand_per_cpu", "", times, scaled(cpu_sessions, 1.0 / cpus))

    databases = _get_table(inputs, "pg_database.csv")
    if databases is not None:
        for key, wide in databases.rows_by_key.items():
            if wide.get("xact_commit") is None or wide.get("xact_rollback") is None:
                continue
            commits = counter_rates(wide.times, wide.get("xact_commit"), nominal)
            rollbacks = counter_rates(wide.times, wide.get("xact_rollback"), nominal)
            out.resets.update(commits[2])
            out.entries[("db.database_transactions_ps", key[0])] = (
                commits[0], add_lists(commits[1], rollbacks[1]), True)
            out.basis.setdefault("db.database_transactions_ps", TIME_AVERAGED)
    return out.finish(notes)


def _postgres_waits(out, waits, times):
    """Sampled sessions by category from pg_waits.csv. A sample without rows had no waiting sessions."""
    counts = long_gauges(waits, times, "sessions")
    totals = {category: [0.0] * len(times) for category in POSTGRES_WAIT_CATEGORIES}
    for (wait_type, wait_event), values in counts.items():
        category = postgres_wait_category(wait_type, wait_event)
        totals[category] = [a + b for a, b in zip(totals[category], values)]
        out.add_gauge("db.wait." + category, "%s:%s" % (wait_type, wait_event), times, values)
    for category in POSTGRES_WAIT_CATEGORIES:
        out.add_gauge("db.wait." + category, "", times, totals[category])


# ---------------------------------------------------------------------------
# MySQL and MariaDB
# ---------------------------------------------------------------------------
def mysql_file_group(event_name):
    """'data', 'redo' or 'temp' for an InnoDB file event name; '' for the others."""
    lowered = event_name.lower()
    if "temp" in lowered:
        return "temp"
    if "log" in lowered or "redo" in lowered:
        return "redo"
    if "data" in lowered:
        return "data"
    return ""


def adapt_mysql(inputs):
    notes = []
    stats = _get_table(inputs, "mysql_stats.csv")
    if stats is None or len(stats) == 0:
        return EngineOutput(notes=notes)
    nominal = nominal_interval(stats.times, inputs.interval_s)
    out = SeriesSet(inputs.interval_s)
    rates = WideRates(stats, nominal, out.resets)
    times = stats.times
    minimums = inputs.minimums

    if rates.has("Questions"):
        out.add_rate("db.statements_ps", "", times, rates.rate("Questions"))
    if rates.has("Com_commit", "Com_rollback"):
        out.add_rate("db.transactions_ps", "", times, add_lists(rates.rate("Com_commit"), rates.rate("Com_rollback")))
    if rates.has("Threads_running"):
        running = [max(value - 1.0, 0.0) if _finite(value) else NAN for value in rates.gauge("Threads_running")]
        out.add_gauge("db.sessions_active", "", times, running)       # the collector's own session is one
        cpus = capacity_cpus(inputs, ())
        if cpus:
            out.add_gauge("db.concurrency_per_cpu", "", times, scaled(running, 1.0 / cpus))
    max_connections = _parse_int(inputs.settings.get("max_connections"))
    if rates.has("Threads_connected") and max_connections:
        out.add_gauge("db.connections_pct", "", times, scaled(rates.gauge("Threads_connected"), 100.0 / max_connections))
    if rates.has("Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests"):
        requests = rates.rate("Innodb_buffer_pool_read_requests")
        misses = rates.rate("Innodb_buffer_pool_reads")
        miss_ratio = delta_ratio(misses, requests, minimums.cache_reads_ps)
        out.add_rate("db.cache_hit_pct", "", times, [100.0 * (1.0 - value) if _finite(value) else NAN for value in miss_ratio])
    if rates.has("Innodb_data_read"):
        out.add_rate("db.read_kBps", "", times, scaled(rates.rate("Innodb_data_read"), 1.0 / BYTES_PER_KB))
    if rates.has("Innodb_data_written"):
        out.add_rate("db.write_kBps", "", times, scaled(rates.rate("Innodb_data_written"), 1.0 / BYTES_PER_KB))
    if rates.has("Created_tmp_disk_tables"):
        out.add_rate("db.temp_tables_ps", "", times, rates.rate("Created_tmp_disk_tables"))
    if rates.has("Innodb_buffer_pool_wait_free"):
        out.add_rate("db.flush_stalls_ps", "", times, rates.rate("Innodb_buffer_pool_wait_free"))
    if rates.has("Innodb_buffer_pool_pages_dirty", "Innodb_buffer_pool_pages_total"):
        share = divided(rates.gauge("Innodb_buffer_pool_pages_dirty"), rates.gauge("Innodb_buffer_pool_pages_total"))
        out.add_gauge("db.dirty_pct", "", times, scaled(share, 100.0))
    if rates.has("Innodb_checkpoint_age", "Innodb_checkpoint_max_age"):
        share = divided(rates.gauge("Innodb_checkpoint_age"), rates.gauge("Innodb_checkpoint_max_age"))
        out.add_gauge("db.checkpoint_age_pct", "", times, scaled(share, 100.0))
    if rates.has("Innodb_row_lock_time"):
        lock_ms = rates.rate("Innodb_row_lock_time")
        out.add_rate("db.wait.lock", "", times, scaled(lock_ms, 1.0 / 1000.0))
        if rates.has("Innodb_row_lock_waits"):
            # Delta time over delta waits; the server's own Innodb_row_lock_time_avg is an average since start.
            out.add_rate("db.lock_wait_avg_ms", "", times, delta_ratio(lock_ms, rates.rate("Innodb_row_lock_waits"), 0.0))
    for column in ("history_list_length", "Innodb_history_list_length"):
        if rates.has(column):
            out.add_gauge("db.history_list", "", times, rates.gauge(column))
            break
    if rates.has("replica_lag_s"):
        out.add_gauge("db.replication_lag_s", "", times, rates.gauge("replica_lag_s"))
    if rates.has("Uptime"):
        out.resets.update(decreases(times, rates.gauge("Uptime")))

    file_io = _get_table(inputs, "mysql_file_io.csv")
    if file_io is not None:
        _mysql_file_io(out, file_io, times, nominal, minimums)
    return out.finish(notes)


def _mysql_file_io(out, file_io, times, nominal, minimums):
    """Waits and latencies from performance_schema file timers (picoseconds, cumulative)."""
    columns = ("count_read", "sum_timer_read_ps", "count_write", "sum_timer_write_ps", "count_misc", "sum_timer_misc_ps")
    per_column = {column: long_rates(file_io, times, nominal, out.resets, column) for column in columns}
    groups = {"data": [], "redo": [], "temp": []}
    for key in file_io.rows_by_key:
        group = mysql_file_group(key[0])
        if group:
            groups[group].append(key)

    def group_sum(group, column):
        return sum_available([per_column[column][key] for key in groups[group] if key in per_column[column]])

    picoseconds_per_second = 1e12
    if groups["data"]:
        read_time = group_sum("data", "sum_timer_read_ps")
        write_time = sum_available([group_sum("data", "sum_timer_write_ps"), group_sum("data", "sum_timer_misc_ps")])
        read_count = group_sum("data", "count_read")
        out.add_rate("db.wait.io_read", "", times, scaled(read_time, 1.0 / picoseconds_per_second))
        out.add_rate("db.wait.io_write", "", times, scaled(write_time, 1.0 / picoseconds_per_second))
        latency = delta_ratio(read_time, read_count, minimums.reads_ps)
        out.add_rate("db.read_latency_ms", "", times, scaled(latency, 1e-9))     # ps per read -> ms
    if groups["redo"]:
        write_time = sum_available([group_sum("redo", "sum_timer_write_ps"), group_sum("redo", "sum_timer_misc_ps")])
        write_count = sum_available([group_sum("redo", "count_write"), group_sum("redo", "count_misc")])
        out.add_rate("db.wait.commit", "", times, scaled(write_time, 1.0 / picoseconds_per_second))
        out.add_rate("db.commit_latency_ms", "", times, scaled(delta_ratio(write_time, write_count, minimums.commits_ps), 1e-9))
    if groups["temp"]:
        total = sum_available([group_sum("temp", column) for column in ("sum_timer_read_ps", "sum_timer_write_ps", "sum_timer_misc_ps")])
        out.add_rate("db.wait.temp", "", times, scaled(total, 1.0 / picoseconds_per_second))


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
def oracle_event_category(name):
    """Category of a listed wait event (spec 9.3), or '' when only its class decides."""
    if name.startswith("direct path") and "temp" in name:
        return "temp"
    if name.startswith("db file ") or name == "direct path read" or name == "read by other session":
        return "io_read"
    if name == "log file sync" or name == "log buffer space" or name.startswith("log file switch"):
        return "commit"
    if name == "free buffer waits":
        return "io_write"
    if name in ("enq: TX - row lock contention", "enq: TM - contention"):
        return "lock"
    if (name.startswith(("latch: ", "library cache: ", "cursor: ")) or name == "buffer busy waits"
            or name == "enq: TX - index contention"):
        return "latch"
    if name == "resmgr:cpu quantum":
        return "cpu"
    if name.startswith("gc "):
        return "replica"
    return ""


def oracle_event_class(name):
    """Wait class of an event of the collector's fixed list, or ''."""
    if name.startswith(("db file ", "direct path")) or name == "read by other session":
        return "User I/O"
    if name == "log file sync":
        return "Commit"
    if name in ("log buffer space", "free buffer waits") or name.startswith("log file switch"):
        return "Configuration"
    if name in ("enq: TX - row lock contention", "enq: TM - contention"):
        return "Application"
    if (name.startswith(("latch: ", "library cache: ", "cursor: ")) or name == "buffer busy waits"
            or name == "enq: TX - index contention"):
        return "Concurrency"
    if name == "resmgr:cpu quantum":
        return "Scheduler"
    if name.startswith("gc "):
        return "Cluster"
    return ""


ORACLE_CLASS_CATEGORY = {
    "User I/O": "io_read", "System I/O": "io_write", "Commit": "commit", "Application": "lock",
    "Concurrency": "latch", "Scheduler": "cpu", "Network": "network", "Cluster": "replica",
}
ORACLE_WAIT_CATEGORIES = ("cpu", "io_read", "io_write", "commit", "lock", "latch", "network", "replica", "temp", "other")
ORACLE_SEQUENTIAL_READ = "db file sequential read"
ORACLE_COMMIT_EVENT = "log file sync"
ORACLE_FREE_BUFFER_EVENT = "free buffer waits"
ORACLE_CHECKPOINT_EVENT = "log file switch (checkpoint incomplete)"
ORACLE_RESOURCE_MANAGER_EVENT = "resmgr:cpu quantum"


def adapt_oracle(inputs):
    notes = []
    stats = _get_table(inputs, "oracle_stats.csv")
    if stats is None or len(stats) == 0:
        return EngineOutput(notes=notes)
    nominal = nominal_interval(stats.times, inputs.interval_s)
    out = SeriesSet(inputs.interval_s)
    rates = WideRates(stats, nominal, out.resets)
    times = stats.times
    minimums = inputs.minimums

    if rates.has("execute_count"):
        out.add_rate("db.statements_ps", "", times, rates.rate("execute_count"))
    if rates.has("user_commits", "user_rollbacks"):
        out.add_rate("db.transactions_ps", "", times, add_lists(rates.rate("user_commits"), rates.rate("user_rollbacks")))
    if rates.has("user_active_sessions"):
        out.add_gauge("db.sessions_active", "", times, rates.gauge("user_active_sessions"))
    if rates.has("physical_reads", "session_logical_reads"):
        logical = rates.rate("session_logical_reads")
        miss_ratio = delta_ratio(rates.rate("physical_reads"), logical, minimums.cache_reads_ps)
        out.add_rate("db.cache_hit_pct", "", times, [100.0 * (1.0 - value) if _finite(value) else NAN for value in miss_ratio])
    if rates.has("physical_read_total_bytes"):
        out.add_rate("db.read_kBps", "", times, scaled(rates.rate("physical_read_total_bytes"), 1.0 / BYTES_PER_KB))
    if rates.has("physical_write_total_bytes"):
        out.add_rate("db.write_kBps", "", times, scaled(rates.rate("physical_write_total_bytes"), 1.0 / BYTES_PER_KB))
    if rates.has("parse_count_hard", "execute_count"):
        share = delta_ratio(rates.rate("parse_count_hard"), rates.rate("execute_count"), MIN_STATEMENTS_PS)
        out.add_rate("db.compile_pct", "", times, scaled(share, 100.0))

    # The more constrained of processes and sessions decides how close the instance is to its limit.
    usage = []
    for current, limit in (("processes_current", "processes_limit"), ("sessions_current", "sessions_limit")):
        if rates.has(current, limit):
            usage.append(divided(rates.gauge(current), rates.gauge(limit)))
    if usage:
        out.add_gauge("db.connections_pct", "", times, scaled(maximum_available(usage), 100.0))
    if rates.has("server_start_marker"):
        out.resets.update(marker_changes(times, rates.gauge("server_start_marker")))

    waits = _get_table(inputs, "oracle_waits.csv")
    cpu_rates = scaled(rates.rate("db_cpu_us"), 1e-6) if rates.has("db_cpu_us") else None
    if waits is not None and len(waits) > 0:
        _oracle_waits(out, waits, times, nominal, minimums, cpu_rates)
    elif cpu_rates is not None:
        out.add_rate("db.wait.cpu", "", times, cpu_rates)
        out.add_rate("db.wait.cpu", "DB CPU", times, cpu_rates)
    cpu_sessions = out.values_on("db.wait.cpu", "", times)
    cpus = capacity_cpus(inputs, ("cpu_count",))
    if cpu_sessions is not None and cpus:
        out.add_rate("db.cpu_demand_per_cpu", "", times, scaled(cpu_sessions, 1.0 / cpus))

    active = _get_table(inputs, "oracle_active.csv")
    if active is not None:
        for key, values in long_gauges(active, times, "sessions").items():
            out.add_gauge("db.sessions_by_class", key[0], times, values)
    return out.finish(notes)


def maximum_available(lists):
    """Element-wise maximum of the finite values; NaN where there is none."""
    result = []
    for values in zip(*lists):
        finite = [value for value in values if _finite(value)]
        result.append(max(finite) if finite else NAN)
    return result


def _oracle_waits(out, waits, times, nominal, minimums, cpu_rates):
    """Foreground wait time by category from oracle_waits.csv (cumulative microseconds)."""
    time_fg = long_rates(waits, times, nominal, out.resets, "time_waited_us_fg")
    time_all = long_rates(waits, times, nominal, out.resets, "time_waited_us")
    count_fg = long_rates(waits, times, nominal, out.resets, "total_waits_fg")
    count_all = long_rates(waits, times, nominal, out.resets, "total_waits")

    def seconds_per_second(key):
        # Foreground time where the release reports it, else the all-session figure.
        rates_by_key = time_fg if key in time_fg and any(_finite(v) for v in time_fg[key]) else time_all
        return scaled(rates_by_key.get(key, [NAN] * len(times)), 1e-6)

    def wait_count(key):
        by_key = count_fg if key in count_fg and any(_finite(v) for v in count_fg[key]) else count_all
        return by_key.get(key, [NAN] * len(times))

    category_parts = {category: [] for category in ORACLE_WAIT_CATEGORIES}     # (detail name, values)
    counted_per_class = {}
    if cpu_rates is not None:
        category_parts["cpu"].append(("DB CPU", cpu_rates))
    for key in sorted(waits.rows_by_key):
        kind, name = key
        if kind != "event":
            continue
        category = oracle_event_category(name)
        if not category:
            continue
        values = seconds_per_second(key)
        category_parts[category].append((name, values))
        wait_class = oracle_event_class(name)
        if wait_class:
            counted_per_class.setdefault(wait_class, []).append(values)
    for key in sorted(waits.rows_by_key):
        kind, name = key
        if kind != "class":
            continue
        remainder = seconds_per_second(key)
        already = sum_available(counted_per_class.get(name, [[NAN] * len(times)]))
        remainder = [max(total - (counted if _finite(counted) else 0.0), 0.0) if _finite(total) else NAN
                     for total, counted in zip(remainder, already)]
        category = ORACLE_CLASS_CATEGORY.get(name, "other")
        category_parts[category].append(("class:" + name, remainder))

    for category in ORACLE_WAIT_CATEGORIES:
        parts = category_parts[category]
        if not parts:
            continue
        out.add_rate("db.wait." + category, "", times, sum_available([values for _, values in parts]))
        for detail, values in parts:
            out.add_rate("db.wait." + category, detail, times, values)

    event_time = {name: seconds_per_second(("event", name)) for (kind, name) in waits.rows_by_key if kind == "event"}
    event_count = {name: wait_count(("event", name)) for (kind, name) in waits.rows_by_key if kind == "event"}
    if ORACLE_SEQUENTIAL_READ in event_time:
        latency = delta_ratio(event_time[ORACLE_SEQUENTIAL_READ], event_count[ORACLE_SEQUENTIAL_READ], minimums.reads_ps)
        out.add_rate("db.read_latency_ms", "", times, scaled(latency, 1000.0))
    if ORACLE_COMMIT_EVENT in event_time:
        latency = delta_ratio(event_time[ORACLE_COMMIT_EVENT], event_count[ORACLE_COMMIT_EVENT], minimums.commits_ps)
        out.add_rate("db.commit_latency_ms", "", times, scaled(latency, 1000.0))
    if ("event", ORACLE_FREE_BUFFER_EVENT) in count_all:
        out.add_rate("db.flush_stalls_ps", "", times, count_all[("event", ORACLE_FREE_BUFFER_EVENT)])
    if ("event", ORACLE_CHECKPOINT_EVENT) in count_all:
        out.add_rate("db.checkpoints", "incomplete", times,
                     long_deltas(waits, ("event", ORACLE_CHECKPOINT_EVENT), times, nominal, out.resets, "total_waits"))


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------
MSSQL_GAUGE = 65792
MSSQL_CUMULATIVE = 272696576
MSSQL_RATIO = 537003264


def mssql_wait_category(wait_type):
    """Category of a sys.dm_os_wait_stats wait type (spec 9.3), or '' for a type outside the list."""
    if wait_type.startswith("PAGEIOLATCH_"):
        return "io_read"
    if wait_type in ("IO_COMPLETION", "ASYNC_IO_COMPLETION", "WRITE_COMPLETION", "BACKUPIO"):
        return "io_write"
    if wait_type in ("WRITELOG", "LOGBUFFER"):
        return "commit"
    if wait_type == "HADR_SYNC_COMMIT":
        return "replica"
    if wait_type.startswith("LCK_M_"):
        return "lock"
    if wait_type.startswith("PAGELATCH_") or wait_type == "CMEMTHREAD":
        return "latch"
    if wait_type == "SOS_SCHEDULER_YIELD":
        return "cpu"
    if wait_type in ("CXPACKET", "CXCONSUMER"):
        return "parallel"
    if wait_type.startswith("RESOURCE_SEMAPHORE"):
        return "memory"
    if wait_type == "THREADPOOL":
        return "workers"
    if wait_type == "ASYNC_NETWORK_IO":
        return "network"
    return ""


MSSQL_WAIT_CATEGORIES = ("cpu", "io_read", "io_write", "commit", "lock", "latch", "memory", "workers",
                         "parallel", "network", "replica")


class MssqlColumns:
    """Reads a counter column as a cumulative counter or as a gauge, as the server's cntr_type says."""

    def __init__(self, rates, manifest, notes):
        self.rates = rates
        self.manifest = manifest
        self.notes = notes
        self.noted = set()

    def is_cumulative(self, column):
        recorded = _parse_int(self.manifest.get("counter_type." + column))
        if recorded is None:
            return column.endswith("_total")
        if recorded == MSSQL_CUMULATIVE:
            return True
        if recorded not in (MSSQL_GAUGE, MSSQL_RATIO) and column not in self.noted:
            self.noted.add(column)
            self.notes.append("SQL Server counter %s has cntr_type %d, which perfdiag does not know; "
                              "it is read as a gauge" % (column, recorded))
        return False


def adapt_mssql(inputs):
    notes = []
    stats = _get_table(inputs, "mssql_stats.csv")
    files = _get_table(inputs, "mssql_files.csv")
    waits = _get_table(inputs, "mssql_waits.csv")
    if stats is None or len(stats) == 0:
        return EngineOutput(notes=notes)
    nominal = nominal_interval(stats.times, inputs.interval_s)
    out = SeriesSet(inputs.interval_s)
    rates = WideRates(stats, nominal, out.resets)
    times = stats.times
    minimums = inputs.minimums
    kinds = MssqlColumns(rates, inputs.manifest, notes)

    def cumulative(column):
        return rates.has(column) and kinds.is_cumulative(column)

    def gauge(column):
        return rates.has(column) and not kinds.is_cumulative(column)

    if cumulative("batch_requests_total"):
        out.add_rate("db.statements_ps", "", times, rates.rate("batch_requests_total"))
    if gauge("active_requests"):
        out.add_gauge("db.sessions_active", "", times, rates.gauge("active_requests"))
    max_workers = _parse_int(inputs.settings.get("max_workers_count"))
    if gauge("active_workers") and max_workers:
        out.add_gauge("db.connections_pct", "", times, scaled(rates.gauge("active_workers"), 100.0 / max_workers))
    if rates.has("buffer_cache_hit_ratio"):
        out.add_gauge("db.cache_hit_pct", "", times, rates.gauge("buffer_cache_hit_ratio"))
    if gauge("memory_grants_pending"):
        out.add_gauge("db.memory_grants_pending", "", times, rates.gauge("memory_grants_pending"))
    if gauge("work_queue_count"):
        out.add_gauge("db.worker_queue", "", times, rates.gauge("work_queue_count"))
    if cumulative("lock_wait_time_ms_total") and cumulative("lock_waits_total"):
        out.add_rate("db.lock_wait_avg_ms", "", times,
                     delta_ratio(rates.rate("lock_wait_time_ms_total"), rates.rate("lock_waits_total"), 0.0))
    if cumulative("deadlocks_total"):
        out.add_rate("db.deadlocks_ps", "", times, rates.rate("deadlocks_total"))
    if cumulative("free_list_stalls_total"):
        out.add_rate("db.flush_stalls_ps", "", times, rates.rate("free_list_stalls_total"))
    if cumulative("sql_compilations_total") and cumulative("batch_requests_total"):
        share = delta_ratio(rates.rate("sql_compilations_total"), rates.rate("batch_requests_total"), MIN_STATEMENTS_PS)
        out.add_rate("db.compile_pct", "", times, scaled(share, 100.0))
    scheduler_count = _parse_int(inputs.settings.get("scheduler_count")) or capacity_cpus(inputs, ())
    if gauge("runnable_tasks") and gauge("busy_schedulers") and scheduler_count:
        demand = add_lists(rates.gauge("runnable_tasks"), rates.gauge("busy_schedulers"))
        out.add_gauge("db.cpu_demand_per_cpu", "", times, scaled(demand, 1.0 / scheduler_count))
    if rates.has("server_start_marker"):
        out.resets.update(marker_changes(times, rates.gauge("server_start_marker")))

    if waits is not None:
        _mssql_waits(out, waits, times, nominal, minimums)
    if files is not None:
        _mssql_files(out, files, times, nominal, minimums, inputs.file_device)
    return out.finish(notes)


def _mssql_waits(out, waits, times, nominal, minimums):
    wait_time = long_rates(waits, times, nominal, out.resets, "wait_time_ms")
    signal_time = long_rates(waits, times, nominal, out.resets, "signal_wait_time_ms")
    wait_count = long_rates(waits, times, nominal, out.resets, "waiting_tasks_count")
    parts = {category: [] for category in MSSQL_WAIT_CATEGORIES}
    for key in sorted(waits.rows_by_key):
        wait_type = key[0]
        category = mssql_wait_category(wait_type)
        if not category or key not in wait_time:
            continue
        # CPU wait is the time a task was runnable and waiting for a scheduler: the signal part.
        source = signal_time if category == "cpu" and key in signal_time else wait_time
        parts[category].append((wait_type, scaled(source[key], 1.0 / 1000.0)))
    for category in MSSQL_WAIT_CATEGORIES:
        if not parts[category]:
            # The collector lists these wait types every sample; none of this category means no waiting.
            if waits.rows_by_key:
                out.add_rate("db.wait." + category, "", times, [NAN] + [0.0] * (len(times) - 1))
            continue
        out.add_rate("db.wait." + category, "", times, sum_available([values for _, values in parts[category]]))
        for detail, values in parts[category]:
            out.add_rate("db.wait." + category, detail, times, values)
    writelog = ("WRITELOG",)
    if writelog in wait_time and writelog in wait_count:
        latency = delta_ratio(wait_time[writelog], wait_count[writelog], minimums.commits_ps)
        out.add_rate("db.commit_latency_ms", "", times, latency)


def _mssql_files(out, files, times, nominal, minimums, file_device):
    """Read and write latency and volume from sys.dm_io_virtual_file_stats, in total and per device."""
    columns = ("num_of_reads", "io_stall_read_ms", "num_of_bytes_read", "num_of_writes", "io_stall_write_ms",
               "num_of_bytes_written")
    per_column = {column: long_rates(files, times, nominal, out.resets, column) for column in columns}

    def keys_where(predicate):
        return [key for key in files.rows_by_key if predicate(key)]

    def total(keys, column):
        return sum_available([per_column[column][key] for key in keys if key in per_column[column]])

    def devices_of(keys):
        grouped = {}
        for key in keys:
            device = file_device(key[2])
            if device:
                grouped.setdefault(device, []).append(key)
        return grouped

    def emit_latency(metric, keys, stall_column, count_column, minimum):
        if not keys:
            return
        out.add_rate(metric, "", times, delta_ratio(total(keys, stall_column), total(keys, count_column), minimum))
        for device, device_keys in devices_of(keys).items():
            out.add_rate(metric, device, times,
                         delta_ratio(total(device_keys, stall_column), total(device_keys, count_column), minimum))

    def emit_volume(metric, keys, column):
        if not keys:
            return
        out.add_rate(metric, "", times, scaled(total(keys, column), 1.0 / BYTES_PER_KB))
        for device, device_keys in devices_of(keys).items():
            out.add_rate(metric, device, times, scaled(total(device_keys, column), 1.0 / BYTES_PER_KB))

    data_files = keys_where(lambda key: key[1] == "ROWS")
    log_files = keys_where(lambda key: key[1] == "LOG")
    emit_latency("db.read_latency_ms", data_files, "io_stall_read_ms", "num_of_reads", minimums.reads_ps)
    # A log-file figure only fills the instance value when no WRITELOG wait gave one.
    if log_files:
        wrote_total = ("db.commit_latency_ms", "") in out.entries
        for device, device_keys in devices_of(log_files).items():
            out.add_rate("db.commit_latency_ms", device, times,
                         delta_ratio(total(device_keys, "io_stall_write_ms"), total(device_keys, "num_of_writes"),
                                     minimums.commits_ps))
        if not wrote_total:
            out.add_rate("db.commit_latency_ms", "", times,
                         delta_ratio(total(log_files, "io_stall_write_ms"), total(log_files, "num_of_writes"),
                                     minimums.commits_ps))
    everything = keys_where(lambda key: True)
    emit_volume("db.read_kBps", everything, "num_of_bytes_read")
    emit_volume("db.write_kBps", everything, "num_of_bytes_written")
    tempdb = keys_where(lambda key: key[0].lower() == "tempdb" and key[1] == "ROWS")
    if tempdb:
        out.add_rate("db.temp_kBps", "", times, scaled(total(tempdb, "num_of_bytes_written"), 1.0 / BYTES_PER_KB))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
ADAPTERS = {
    "postgresql": adapt_postgresql,
    "mysql": adapt_mysql,
    "mariadb": adapt_mysql,
    "oracle": adapt_oracle,
    "mssql": adapt_mssql,
}


def files_for(engine):
    """(file name, key columns or None) pairs the engine's collector writes."""
    return ENGINE_FILES["mysql" if engine == "mariadb" else engine]


def adapt(inputs):
    """EngineOutput for one run's tables."""
    return ADAPTERS[inputs.engine](inputs)
