"""Builds synthetic DB collector run directories (collector schema 3, and a 2.0.0 PostgreSQL run).

The timeline is the one of synth.py: sample i (0-based) is taken at start + (i + 1) * interval,
so an OS run from Synth and a DB run from DbSynth line up second for second.

Each engine class holds named per-sample lists in `self.values` with calm defaults; a test
overrides the ones it cares about and calls write():

    synth_db = DbSynth(tmp)
    pg = synth_db.postgresql(data_directory="/var/lib/pgsql/16/data")
    pg.fill("read_latency_ms", 30, 90, 40.0)
    pg.write()

Counters are built from per-second rates, so the rate the analysis computes equals the rate
the test set. `reset_counters_at` makes the cumulative counters restart at a sample;
`marker_changes_at` changes the start/reset marker without touching the counters (a restart
between two samples that nets growth); `skip_ranges` leaves samples out.

Rates are per second; "latency" values are milliseconds per operation.
"""

import datetime
import math
import os

from synth import BASE_EPOCH

UTC = datetime.timezone.utc
COUNTER_BASE = 100000.0


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, UTC).isoformat()


def csv_text(value):
    """A CSV-quoted string, as the collectors write names."""
    return '"%s"' % str(value).replace('"', '""')


def number_text(value):
    if value is None or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, float):
        return "%.3f" % value if value != int(value) else str(int(value))
    return str(value)


class DbSynth:
    """The shared timeline and the output directory of a set of DB runs."""

    def __init__(self, root, host="db01", samples=120, interval=1, start=BASE_EPOCH, cpus=2,
                 mem_total_kb=8 * 1024 * 1024, timezone="UTC +0000"):
        self.root = root
        self.host = host
        self.samples = samples
        self.interval = interval
        self.start = start
        self.cpus = cpus
        self.mem_total_kb = mem_total_kb
        self.timezone = timezone

    def postgresql(self, **options):
        return PostgresRun(self, **options)

    def postgresql_v2(self, **options):
        return PostgresV2Run(self, **options)

    def mysql(self, **options):
        return MysqlRun(self, **options)

    def oracle(self, **options):
        return OracleRun(self, **options)

    def mssql(self, **options):
        return MssqlRun(self, **options)


class DbRun:
    """What the four engines share: sample times, counters, the manifest and the files."""

    tag = ""
    script = ""
    engine = ""

    def __init__(self, synth, instance, version="", version_num=0, local=True, local_basis="socket",
                 settings=None, paths=None, schema=3, stamp="20261016_151320", notes="", extra_manifest=None):
        self.synth = synth
        self.instance = instance
        self.version = version
        self.version_num = version_num
        self.local = local
        self.local_basis = local_basis
        self.settings = dict(settings or {})
        self.paths = {role: list(found) for role, found in (paths or {}).items()}
        self.schema = schema
        self.stamp = stamp
        self.notes = notes
        self.extra_manifest = dict(extra_manifest or {})
        self.values = {}
        self.skip_ranges = []            # (first_index, last_index) samples to leave out entirely
        self.reset_counters_at = []      # sample indexes at which every counter restarts
        self.marker_changes_at = []      # sample indexes at which the start marker changes, counters keep growing
        self.bad_rows = {}               # sample index -> {file name: [raw line, ...]}, appended after the sample
        self.directory = ""

    # --- helpers for tests ---------------------------------------------------
    @property
    def n(self):
        return self.synth.samples

    def set_default(self, name, value):
        self.values[name] = [value] * self.n

    def fill(self, name, first, last, value):
        """Sets samples first..last-1 of a named list."""
        for index in range(first, last):
            self.values[name][index] = value

    def epoch(self, index):
        return self.synth.start + (index + 1) * self.synth.interval

    def indexes(self):
        return [index for index in range(self.n) if not any(a <= index <= b for a, b in self.skip_ranges)]

    def counter(self, rates, base=COUNTER_BASE):
        """Cumulative counter from per-second rates; the first sample is the base value."""
        interval = self.synth.interval
        total = base
        found = []
        for index, rate in enumerate(rates):
            if index > 0:
                total += rate * interval
            if index in self.reset_counters_at:
                total = math.floor(rate * interval * 0.5)
            found.append(math.floor(total + 0.5))
        return found

    def marker(self, base):
        value = base
        found = []
        for index in range(self.n):
            if index in self.marker_changes_at:
                value += 100000
            found.append(value)
        return found

    # --- files ---------------------------------------------------------------
    def run_directory(self):
        name = "%s_%s_%s" % (self.synth.host, self.tag, self.stamp)
        self.directory = os.path.join(self.synth.root, name)
        os.makedirs(self.directory, exist_ok=True)
        return self.directory

    def write_csv(self, name, header, rows_for):
        """rows_for(index) -> list of rows (lists of cell texts). Bad rows of the test are appended."""
        lines = [",".join(header)]
        for index in self.indexes():
            for cells in rows_for(index):
                lines.append(",".join([iso(self.epoch(index))] + [str(cell) for cell in cells]))
            for raw in self.bad_rows.get(index, {}).get(name, []):
                lines.append(raw)
        with open(os.path.join(self.directory, name), "w") as handle:
            handle.write("\n".join(lines) + "\n")

    def write_paths(self):
        if not (self.schema >= 3 and self.local and self.paths):
            return
        with open(os.path.join(self.directory, "db_paths.txt"), "w") as handle:
            for role, found in self.paths.items():
                for number, path in enumerate(found, 1):
                    handle.write("path.%s.%d=%s\n" % (role, number, path))

    def write_manifest(self, files, v2_target=""):
        synth = self.synth
        lines = [
            "script=" + self.script,
            "script_version=%s" % ("3.0.0" if self.schema >= 3 else "2.0.0"),
            "hostname=" + synth.host,
            "distro=Test Linux 1.0", "kernel=6.1.0-test", "cpus=%d" % synth.cpus,
            "mem_total_kb=%d" % synth.mem_total_kb, "timezone=" + synth.timezone,
            "start=" + iso(synth.start), "interval_s=%d" % synth.interval,
            "samples=%d" % synth.samples, "duration_s=%d" % (synth.samples * synth.interval), "psi=yes",
        ]
        if self.schema >= 3:
            lines += [
                "collector_schema=%d" % self.schema, "db_engine=" + self.engine, "db_instance=" + self.instance,
                "server_version_num=%s" % self.version_num, "server_hostname=" + synth.host,
                "target_local=" + ("yes" if self.local else "no"),
                "target_local_basis=" + (self.local_basis if self.local else "none"),
                "server_start=2026-10-15 08:00:00+00", "query_timeout_s=%d" % max(synth.interval - 1, 1),
                "files=" + " ".join(files),
            ]
        else:
            lines.append("target=" + v2_target)
        lines.append("server_version=" + self.version)
        for key, value in self.settings.items():
            if self.schema >= 3:
                lines.append("setting.%s=%s" % (key, value))
        for key, value in self.extra_manifest.items():
            lines.append("%s=%s" % (key, value))
        lines.append("notes=" + self.notes)
        with open(os.path.join(self.directory, "manifest.txt"), "w") as handle:
            handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
PG_SUMMARY_HEADER = (
    "ts,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,"
    "temp_files,temp_bytes,deadlocks,bgwriter_checkpoints_timed,bgwriter_checkpoints_req,bgwriter_buffers_checkpoint,"
    "bgwriter_buffers_clean,active_connections,idle_connections,waiting_connections,blk_read_time_ms,blk_write_time_ms,"
    "checkpoint_write_time_ms,checkpoint_sync_time_ms,backend_buffer_writes,backend_buffer_fsyncs,maxwritten_clean,"
    "buffers_alloc,idle_in_transaction_connections,total_connections,max_connections,oldest_xact_age_s,"
    "oldest_active_query_age_s,xid_age_max,autovacuum_workers,wal_records,wal_fpi,wal_bytes,wal_buffers_full,"
    "wal_lsn_bytes,in_recovery,replication_lag_s,server_start_marker,stats_reset_marker").split(",")
PG_DATABASE_HEADER = ("ts,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,"
                      "tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,blk_read_time_ms,"
                      "blk_write_time_ms").split(",")
PG_WAITS_HEADER = "ts,wait_event_type,wait_event,sessions".split(",")
PG_V2_HEADER = ("ts,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,"
                "tup_deleted,temp_files,temp_bytes,deadlocks,bgwriter_checkpoints_timed,bgwriter_checkpoints_req,"
                "bgwriter_buffers_checkpoint,bgwriter_buffers_clean,active_connections,idle_connections,"
                "waiting_connections").split(",")


class PostgresRun(DbRun):
    tag = "postgresql"
    script = "collect-postgresql-metrics.sh"
    engine = "postgresql"

    def __init__(self, synth, instance="localhost:5432", version="16.4", version_num=160004, data_directory="",
                 wal_directory="", with_waits=True, with_io_timing=True, **options):
        settings = {"block_size": "8192", "max_connections": "100", "track_io_timing": "on" if with_io_timing else "off"}
        settings.update(options.pop("settings", {}))
        paths = options.pop("paths", None)
        if paths is None and data_directory:
            paths = {"data": [data_directory], "wal": [wal_directory or data_directory + "/pg_wal"]}
        super().__init__(synth, instance, version, version_num, settings=settings, paths=paths, **options)
        self.with_waits = with_waits
        defaults = {
            "commits_ps": 50.0, "rollbacks_ps": 1.0, "blks_read_ps": 20.0, "blks_hit_ps": 2000.0,
            "read_latency_ms": 0.5, "temp_bytes_ps": 0.0, "deadlocks_ps": 0.0, "ckpt_timed_ps": 0.0,
            "ckpt_req_ps": 0.0, "ckpt_buffers_ps": 10.0, "clean_buffers_ps": 5.0, "backend_writes_ps": 1.0,
            "backend_fsyncs_ps": 0.0, "wal_bytes_ps": 50000.0, "active": 2, "idle": 10, "total": 12,
            "max_connections": 100, "xact_age_s": 1.0, "replication_lag_s": "",
        }
        for name, value in defaults.items():
            self.set_default(name, value)
        # Sampled waits: (wait_event_type, wait_event) -> sessions per sample.
        self.waits = {("CPU", "CPU"): [1] * self.n}
        # Per-database transaction rates; names may contain commas.
        self.databases = {"app": [40.0] * self.n, "postgres": [1.0] * self.n}

    def write(self):
        self.run_directory()
        v = self.values
        n = self.n
        interval = self.synth.interval
        read_time_rate = [rate * latency for rate, latency in zip(v["blks_read_ps"], v["read_latency_ms"])]
        columns = {
            "xact_commit": self.counter(v["commits_ps"]), "xact_rollback": self.counter(v["rollbacks_ps"]),
            "blks_read": self.counter(v["blks_read_ps"]), "blks_hit": self.counter(v["blks_hit_ps"]),
            "tup_returned": self.counter([100.0] * n), "tup_fetched": self.counter([50.0] * n),
            "tup_inserted": self.counter([5.0] * n), "tup_updated": self.counter([5.0] * n),
            "tup_deleted": self.counter([1.0] * n), "temp_files": self.counter([0.0] * n),
            "temp_bytes": self.counter(v["temp_bytes_ps"]), "deadlocks": self.counter(v["deadlocks_ps"]),
            "bgwriter_checkpoints_timed": self.counter(v["ckpt_timed_ps"], 100),
            "bgwriter_checkpoints_req": self.counter(v["ckpt_req_ps"], 100),
            "bgwriter_buffers_checkpoint": self.counter(v["ckpt_buffers_ps"]),
            "bgwriter_buffers_clean": self.counter(v["clean_buffers_ps"]),
            "active_connections": v["active"], "idle_connections": v["idle"], "waiting_connections": [0] * n,
            "blk_read_time_ms": self.counter(read_time_rate), "blk_write_time_ms": self.counter([0.0] * n),
            "checkpoint_write_time_ms": self.counter([0.0] * n), "checkpoint_sync_time_ms": self.counter([0.0] * n),
            "backend_buffer_writes": self.counter(v["backend_writes_ps"]),
            "backend_buffer_fsyncs": self.counter(v["backend_fsyncs_ps"]),
            "maxwritten_clean": self.counter([0.0] * n), "buffers_alloc": self.counter([10.0] * n),
            "idle_in_transaction_connections": [0] * n, "total_connections": v["total"],
            "max_connections": v["max_connections"], "oldest_xact_age_s": v["xact_age_s"],
            "oldest_active_query_age_s": [0.5] * n, "xid_age_max": [1000] * n, "autovacuum_workers": [0] * n,
            "wal_records": self.counter([100.0] * n), "wal_fpi": self.counter([1.0] * n),
            "wal_bytes": self.counter(v["wal_bytes_ps"]), "wal_buffers_full": self.counter([0.0] * n),
            "wal_lsn_bytes": self.counter(v["wal_bytes_ps"]), "in_recovery": [0] * n,
            "replication_lag_s": v["replication_lag_s"],
            "server_start_marker": self.marker(int(self.synth.start) - 86400),
            "stats_reset_marker": self.marker(int(self.synth.start) - 172800),
        }
        header = PG_SUMMARY_HEADER
        self.write_csv("pg_stats.csv", header, lambda i: [[number_text(columns[name][i]) for name in header[1:]]])

        database_counters = {name: self.counter(rates) for name, rates in self.databases.items()}

        def database_rows(index):
            rows = []
            for name in self.databases:
                commits = database_counters[name][index]
                rows.append([csv_text(name), 1, commits, 0, 10, 100, 10, 5, 1, 1, 1, 0, 0, 0, 0, 0])
            return rows
        self.write_csv("pg_database.csv", PG_DATABASE_HEADER, database_rows)
        files = ["pg_stats.csv", "pg_database.csv"]
        if self.with_waits:
            self.write_csv("pg_waits.csv", PG_WAITS_HEADER, lambda i: [
                [csv_text(kind), csv_text(event), sessions[i]]
                for (kind, event), sessions in self.waits.items() if sessions[i]])
            files.append("pg_waits.csv")
        self.write_paths()
        self.write_manifest(files)
        return self.directory


class PostgresV2Run(DbRun):
    """A collect-postgresql-metrics.sh 2.0.0 run: 20 columns, no waits, no paths, a target key."""

    tag = "postgresql"
    script = "collect-postgresql-metrics.sh"
    engine = "postgresql"

    def __init__(self, synth, target="postgres@localhost:5432", **options):
        super().__init__(synth, "", version=options.pop("version", "14.10"), schema=2, **options)
        self.target = target
        self.set_default("commits_ps", 50.0)
        self.set_default("blks_read_ps", 20.0)
        self.set_default("blks_hit_ps", 2000.0)

    def write(self):
        self.run_directory()
        v = self.values
        n = self.n
        columns = {
            "xact_commit": self.counter(v["commits_ps"]), "xact_rollback": self.counter([1.0] * n),
            "blks_read": self.counter(v["blks_read_ps"]), "blks_hit": self.counter(v["blks_hit_ps"]),
            "tup_returned": self.counter([100.0] * n), "tup_fetched": self.counter([50.0] * n),
            "tup_inserted": self.counter([5.0] * n), "tup_updated": self.counter([5.0] * n),
            "tup_deleted": self.counter([1.0] * n), "temp_files": self.counter([0.0] * n),
            "temp_bytes": self.counter([0.0] * n), "deadlocks": self.counter([0.0] * n),
            "bgwriter_checkpoints_timed": self.counter([0.0] * n, 100), "bgwriter_checkpoints_req": self.counter([0.0] * n, 100),
            "bgwriter_buffers_checkpoint": self.counter([10.0] * n), "bgwriter_buffers_clean": self.counter([5.0] * n),
            "active_connections": [2] * n, "idle_connections": [10] * n, "waiting_connections": [0] * n,
        }
        header = PG_V2_HEADER
        self.write_csv("pg_stats.csv", header, lambda i: [[number_text(columns[name][i]) for name in header[1:]]])
        self.write_manifest(["pg_stats.csv"], v2_target=self.target)
        return self.directory


# ---------------------------------------------------------------------------
# MySQL and MariaDB
# ---------------------------------------------------------------------------
MYSQL_VARIABLES = (
    "Threads_connected Threads_running Questions Slow_queries Com_select Com_insert Com_update Com_delete "
    "Innodb_buffer_pool_reads Innodb_buffer_pool_read_requests Innodb_row_lock_waits Innodb_row_lock_time "
    "Innodb_row_lock_time_avg Created_tmp_disk_tables Created_tmp_tables Open_files Open_tables Aborted_connects "
    "Aborted_clients Uptime Threads_created Connections Max_used_connections Com_commit Com_rollback "
    "Select_full_join Select_scan Sort_merge_passes Table_locks_waited Innodb_buffer_pool_wait_free "
    "Innodb_buffer_pool_pages_total Innodb_buffer_pool_pages_dirty Innodb_buffer_pool_pages_free "
    "Innodb_buffer_pool_pages_flushed Innodb_data_reads Innodb_data_writes Innodb_data_read Innodb_data_written "
    "Innodb_data_fsyncs Innodb_os_log_written Innodb_os_log_fsyncs Innodb_log_waits Innodb_row_lock_current_waits "
    "Innodb_rows_read Innodb_rows_inserted Innodb_rows_updated Innodb_rows_deleted Innodb_checkpoint_age "
    "Innodb_checkpoint_max_age Innodb_history_list_length Bytes_received Bytes_sent").split()
MYSQL_STATS_HEADER = (["ts"] + MYSQL_VARIABLES +
                      ["history_list_length", "replica_lag_s", "replica_io_running", "replica_sql_running"])
MYSQL_FILE_HEADER = ("ts,event_name,count_read,sum_timer_read_ps,count_write,sum_timer_write_ps,count_misc,"
                     "sum_timer_misc_ps").split(",")
PICOSECONDS_PER_MS = 1e9


class MysqlRun(DbRun):
    tag = "mysql"
    script = "collect-mysql-metrics.sh"

    def __init__(self, synth, instance="localhost:3306", version="8.0.36", version_num=80036, mariadb=False,
                 data_directory="", redo_directory="", with_file_io=True, **options):
        self.engine = "mariadb" if mariadb else "mysql"
        settings = {"max_connections": "151", "performance_schema": "ON" if with_file_io else "OFF"}
        settings.update(options.pop("settings", {}))
        paths = options.pop("paths", None)
        if paths is None and data_directory:
            paths = {"data": [data_directory], "redo": [redo_directory or data_directory]}
        super().__init__(synth, instance, version, version_num, settings=settings, paths=paths, **options)
        self.with_file_io = with_file_io
        defaults = {
            "questions_ps": 200.0, "commits_ps": 40.0, "rollbacks_ps": 1.0, "threads_running": 3,
            "threads_connected": 20, "bp_reads_ps": 5.0, "bp_requests_ps": 5000.0, "data_read_Bps": 80000.0,
            "data_written_Bps": 120000.0, "tmp_disk_ps": 0.0, "wait_free_ps": 0.0, "row_lock_ms_ps": 0.0,
            "row_lock_waits_ps": 0.0, "pages_dirty": 100, "pages_total": 8192, "history_list": 50,
            "replica_lag_s": "", "checkpoint_age": "", "checkpoint_max_age": "",
            # file events: operations per second and latency in ms
            "data_reads_ps": 50.0, "data_read_latency_ms": 0.4, "data_writes_ps": 80.0, "data_write_latency_ms": 0.2,
            "redo_writes_ps": 40.0, "redo_write_latency_ms": 0.3,
        }
        for name, value in defaults.items():
            self.set_default(name, value)

    def write(self):
        self.run_directory()
        v = self.values
        n = self.n
        zero = [0.0] * n
        columns = {
            "Threads_connected": v["threads_connected"], "Threads_running": v["threads_running"],
            "Questions": self.counter(v["questions_ps"]), "Slow_queries": self.counter(zero),
            "Com_select": self.counter([100.0] * n), "Com_insert": self.counter([10.0] * n),
            "Com_update": self.counter([10.0] * n), "Com_delete": self.counter([1.0] * n),
            "Innodb_buffer_pool_reads": self.counter(v["bp_reads_ps"]),
            "Innodb_buffer_pool_read_requests": self.counter(v["bp_requests_ps"]),
            "Innodb_row_lock_waits": self.counter(v["row_lock_waits_ps"]),
            "Innodb_row_lock_time": self.counter(v["row_lock_ms_ps"]),
            "Innodb_row_lock_time_avg": [0] * n,
            "Created_tmp_disk_tables": self.counter(v["tmp_disk_ps"]), "Created_tmp_tables": self.counter([2.0] * n),
            "Open_files": [30] * n, "Open_tables": [200] * n, "Aborted_connects": self.counter(zero),
            "Aborted_clients": self.counter(zero),
            "Uptime": self._uptime(), "Threads_created": self.counter(zero), "Connections": self.counter([1.0] * n),
            "Max_used_connections": [30] * n, "Com_commit": self.counter(v["commits_ps"]),
            "Com_rollback": self.counter(v["rollbacks_ps"]), "Select_full_join": self.counter(zero),
            "Select_scan": self.counter(zero), "Sort_merge_passes": self.counter(zero),
            "Table_locks_waited": self.counter(zero),
            "Innodb_buffer_pool_wait_free": self.counter(v["wait_free_ps"]),
            "Innodb_buffer_pool_pages_total": v["pages_total"], "Innodb_buffer_pool_pages_dirty": v["pages_dirty"],
            "Innodb_buffer_pool_pages_free": [100] * n, "Innodb_buffer_pool_pages_flushed": self.counter([5.0] * n),
            "Innodb_data_reads": self.counter(v["data_reads_ps"]), "Innodb_data_writes": self.counter(v["data_writes_ps"]),
            "Innodb_data_read": self.counter(v["data_read_Bps"]), "Innodb_data_written": self.counter(v["data_written_Bps"]),
            "Innodb_data_fsyncs": self.counter(zero), "Innodb_os_log_written": self.counter([4000.0] * n),
            "Innodb_os_log_fsyncs": self.counter(v["redo_writes_ps"]), "Innodb_log_waits": self.counter(zero),
            "Innodb_row_lock_current_waits": [0] * n, "Innodb_rows_read": self.counter([100.0] * n),
            "Innodb_rows_inserted": self.counter([5.0] * n), "Innodb_rows_updated": self.counter([5.0] * n),
            "Innodb_rows_deleted": self.counter([1.0] * n), "Innodb_checkpoint_age": v["checkpoint_age"],
            "Innodb_checkpoint_max_age": v["checkpoint_max_age"], "Innodb_history_list_length": [""] * n,
            "Bytes_received": self.counter([1000.0] * n), "Bytes_sent": self.counter([5000.0] * n),
            "history_list_length": v["history_list"], "replica_lag_s": v["replica_lag_s"],
            "replica_io_running": [""] * n, "replica_sql_running": [""] * n,
        }
        header = MYSQL_STATS_HEADER
        self.write_csv("mysql_stats.csv", header, lambda i: [[number_text(columns[name][i]) for name in header[1:]]])
        files = ["mysql_stats.csv"]
        if self.with_file_io:
            def timer(count_rates, latencies):
                return self.counter([c * l * PICOSECONDS_PER_MS for c, l in zip(count_rates, latencies)], 0)
            data_reads = self.counter(v["data_reads_ps"], 0)
            data_read_time = timer(v["data_reads_ps"], v["data_read_latency_ms"])
            data_writes = self.counter(v["data_writes_ps"], 0)
            data_write_time = timer(v["data_writes_ps"], v["data_write_latency_ms"])
            redo_writes = self.counter(v["redo_writes_ps"], 0)
            redo_time = timer(v["redo_writes_ps"], v["redo_write_latency_ms"])

            def file_rows(index):
                return [
                    [csv_text("wait/io/file/innodb/innodb_data_file"), data_reads[index], data_read_time[index],
                     data_writes[index], data_write_time[index], 0, 0],
                    [csv_text("wait/io/file/innodb/innodb_log_file"), 0, 0, redo_writes[index], redo_time[index], 0, 0],
                ]
            self.write_csv("mysql_file_io.csv", MYSQL_FILE_HEADER, file_rows)
            files.append("mysql_file_io.csv")
        self.write_paths()
        self.write_manifest(files)
        return self.directory

    def _uptime(self):
        """Seconds since start, restarting from 1 at a counter reset."""
        found = []
        seconds = 86400
        for index in range(self.n):
            seconds += self.synth.interval
            if index in self.reset_counters_at:
                seconds = 1
            found.append(seconds)
        return found


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
ORACLE_STATS_HEADER = (
    "ts,physical_reads,physical_writes,session_logical_reads,redo_size,user_calls,db_block_gets,consistent_gets,"
    "parse_count_total,parse_count_hard,active_sessions,physical_read_total_io_requests,"
    "physical_write_total_io_requests,physical_read_total_bytes,physical_write_total_bytes,user_commits,"
    "user_rollbacks,execute_count,sorts_disk,sorts_memory,redo_log_space_requests,logons_current,db_time_us,"
    "db_cpu_us,user_sessions,user_active_sessions,processes_current,processes_limit,sessions_current,sessions_limit,"
    "server_start_marker").split(",")
ORACLE_WAITS_HEADER = "ts,kind,name,total_waits,time_waited_us,total_waits_fg,time_waited_us_fg".split(",")
ORACLE_ACTIVE_HEADER = "ts,state_class,sessions".split(",")


class OracleRun(DbRun):
    tag = "oracle"
    script = "collect-oracle-metrics.sh"
    engine = "oracle"

    def __init__(self, synth, instance="ORCL/PDB1", version="19.3.0.0.0", version_num=190300, datafile_directory="",
                 **options):
        settings = {"cpu_count": "2", "db_block_size": "8192", "con_name": "PDB1"}
        settings.update(options.pop("settings", {}))
        paths = options.pop("paths", None)
        if paths is None and datafile_directory:
            paths = {"data": [datafile_directory], "redo": [datafile_directory]}
        super().__init__(synth, instance, version, version_num, settings=settings, paths=paths, **options)
        defaults = {
            "exec_ps": 300.0, "commits_ps": 40.0, "rollbacks_ps": 1.0, "physical_reads_ps": 20.0,
            "logical_reads_ps": 5000.0, "read_bytes_ps": 200000.0, "write_bytes_ps": 300000.0,
            "cpu_sessions": 1.0, "active": 2, "processes_current": 60, "processes_limit": 300,
            "sessions_current": 80, "sessions_limit": 472, "hard_parse_ps": 0.5,
        }
        for name, value in defaults.items():
            self.set_default(name, value)
        # Foreground wait events: name -> operations per second and latency in ms (per sample lists).
        self.events = {
            "db file sequential read": {"waits_ps": [20.0] * self.n, "latency_ms": [1.0] * self.n},
            "log file sync": {"waits_ps": [40.0] * self.n, "latency_ms": [1.0] * self.n},
        }
        # Time in classes that the listed events do not explain, in seconds per second.
        self.class_extra = {"Idle": None, "User I/O": [0.0] * self.n, "Commit": [0.0] * self.n,
                            "Concurrency": [0.0] * self.n, "Application": [0.0] * self.n}
        self.active_classes = {"ON CPU": [1] * self.n}

    EVENT_CLASS = {
        "db file sequential read": "User I/O", "db file scattered read": "User I/O", "direct path read": "User I/O",
        "direct path read temp": "User I/O", "direct path write temp": "User I/O", "log file sync": "Commit",
        "log buffer space": "Configuration", "free buffer waits": "Configuration",
        "enq: TX - row lock contention": "Application", "latch: cache buffers chains": "Concurrency",
        "resmgr:cpu quantum": "Scheduler", "log file switch (checkpoint incomplete)": "Configuration",
    }

    def write(self):
        self.run_directory()
        v = self.values
        n = self.n
        zero = [0.0] * n
        cpu_us_rate = [sessions * 1e6 for sessions in v["cpu_sessions"]]
        columns = {
            "physical_reads": self.counter(v["physical_reads_ps"]), "physical_writes": self.counter([10.0] * n),
            "session_logical_reads": self.counter(v["logical_reads_ps"]), "redo_size": self.counter([50000.0] * n),
            "user_calls": self.counter([400.0] * n), "db_block_gets": self.counter([1000.0] * n),
            "consistent_gets": self.counter([4000.0] * n), "parse_count_total": self.counter([100.0] * n),
            "parse_count_hard": self.counter(v["hard_parse_ps"]), "active_sessions": [30] * n,
            "physical_read_total_io_requests": self.counter(v["physical_reads_ps"]),
            "physical_write_total_io_requests": self.counter([10.0] * n),
            "physical_read_total_bytes": self.counter(v["read_bytes_ps"]),
            "physical_write_total_bytes": self.counter(v["write_bytes_ps"]),
            "user_commits": self.counter(v["commits_ps"]), "user_rollbacks": self.counter(v["rollbacks_ps"]),
            "execute_count": self.counter(v["exec_ps"]), "sorts_disk": self.counter(zero),
            "sorts_memory": self.counter([50.0] * n), "redo_log_space_requests": self.counter(zero),
            "logons_current": [60] * n, "db_time_us": self.counter(cpu_us_rate), "db_cpu_us": self.counter(cpu_us_rate),
            "user_sessions": [50] * n, "user_active_sessions": v["active"],
            "processes_current": v["processes_current"], "processes_limit": v["processes_limit"],
            "sessions_current": v["sessions_current"], "sessions_limit": v["sessions_limit"],
            "server_start_marker": self.marker(int(self.synth.start) - 86400),
        }
        header = ORACLE_STATS_HEADER
        self.write_csv("oracle_stats.csv", header, lambda i: [[number_text(columns[name][i]) for name in header[1:]]])

        event_counts, event_times = {}, {}
        for name, spec in self.events.items():
            event_counts[name] = self.counter(spec["waits_ps"], 0)
            event_times[name] = self.counter([w * l * 1000.0 for w, l in zip(spec["waits_ps"], spec["latency_ms"])], 0)
        class_times = {}
        for wait_class, extra in self.class_extra.items():
            if extra is None:
                continue
            members = [name for name in self.events if self.EVENT_CLASS.get(name) == wait_class]
            rate = [extra[i] * 1e6 + sum(self.events[name]["waits_ps"][i] * self.events[name]["latency_ms"][i] * 1000.0
                                         for name in members) for i in range(n)]
            class_times[wait_class] = self.counter(rate, 0)

        def wait_rows(index):
            rows = []
            for wait_class, times in class_times.items():
                rows.append([csv_text("class"), csv_text(wait_class), 0, times[index], 0, times[index]])
            for name in self.events:
                rows.append([csv_text("event"), csv_text(name), event_counts[name][index], event_times[name][index],
                             event_counts[name][index], event_times[name][index]])
            return rows
        self.write_csv("oracle_waits.csv", ORACLE_WAITS_HEADER, wait_rows)
        self.write_csv("oracle_active.csv", ORACLE_ACTIVE_HEADER, lambda i: [
            [csv_text(state), sessions[i]] for state, sessions in self.active_classes.items() if sessions[i]])
        self.write_paths()
        self.write_manifest(["oracle_stats.csv", "oracle_waits.csv", "oracle_active.csv"])
        return self.directory


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------
MSSQL_STATS_HEADER = (
    "ts,buffer_cache_hit_ratio,page_life_expectancy,checkpoint_pages_total,lazy_writes_total,memory_grants_pending,"
    "target_server_memory_kb,total_server_memory_kb,batch_requests_total,sql_compilations_total,"
    "sql_recompilations_total,user_connections,processes_blocked,lock_waits_total,lock_wait_time_ms_total,"
    "deadlocks_total,log_flushes_total,log_flush_wait_time_ms_total,log_bytes_flushed_total,page_reads_total,"
    "page_writes_total,free_list_stalls_total,full_scans_total,page_splits_total,workfiles_created_total,"
    "worktables_created_total,memory_grants_outstanding,active_requests,runnable_tasks,work_queue_count,"
    "active_workers,busy_schedulers,server_start_marker").split(",")
MSSQL_WAITS_HEADER = "ts,wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms".split(",")
MSSQL_FILES_HEADER = ("ts,database,file_type,physical_name,num_of_reads,io_stall_read_ms,num_of_bytes_read,"
                      "num_of_writes,io_stall_write_ms,num_of_bytes_written").split(",")
MSSQL_COUNTER_TYPES = {
    "lock_waits_total": 272696576, "lock_wait_time_ms_total": 272696576, "deadlocks_total": 272696576,
    "batch_requests_total": 272696576, "sql_compilations_total": 272696576, "free_list_stalls_total": 272696576,
    "memory_grants_pending": 65792, "user_connections": 65792,
}
MSSQL_WAIT_TYPES = ("WRITELOG", "PAGEIOLATCH_SH", "PAGEIOLATCH_EX", "LCK_M_X", "LCK_M_S", "PAGELATCH_EX",
                    "SOS_SCHEDULER_YIELD", "CXPACKET", "RESOURCE_SEMAPHORE", "THREADPOOL", "ASYNC_NETWORK_IO")


class MssqlFile:
    """One database file of sys.dm_io_virtual_file_stats, with its load."""

    def __init__(self, database, file_type, path, reads_ps=50.0, read_latency_ms=1.0, writes_ps=50.0,
                 write_latency_ms=1.0, bytes_per_read=8192, bytes_per_write=8192):
        self.database = database
        self.file_type = file_type
        self.path = path
        self.values = {"reads_ps": reads_ps, "read_latency_ms": read_latency_ms, "writes_ps": writes_ps,
                       "write_latency_ms": write_latency_ms}
        self.bytes_per_read = bytes_per_read
        self.bytes_per_write = bytes_per_write
        self.lists = {}

    def prepare(self, samples):
        self.lists = {name: [value] * samples for name, value in self.values.items()}

    def fill(self, name, first, last, value):
        for index in range(first, last):
            self.lists[name][index] = value


class MssqlRun(DbRun):
    tag = "mssql"
    script = "collect-mssql-metrics.sh"
    engine = "mssql"

    def __init__(self, synth, instance="MSSQLSERVER", version="16.0.4125.3", version_num=16, files=None, **options):
        settings = {"cpu_count": "2", "scheduler_count": "2", "max_workers_count": "512"}
        settings.update(options.pop("settings", {}))
        paths = options.pop("paths", None)
        super().__init__(synth, instance, version, version_num, settings=settings, paths=paths, **options)
        defaults = {
            "batches_ps": 150.0, "compilations_ps": 2.0, "cache_hit_pct": 99.5, "memory_grants_pending": 0,
            "active_requests": 2, "runnable_tasks": 0, "busy_schedulers": 1, "active_workers": 40, "work_queue": 0,
            "lock_waits_ps": 0.0, "lock_wait_ms_ps": 0.0, "deadlocks_ps": 0.0, "free_list_stalls_ps": 0.0,
        }
        for name, value in defaults.items():
            self.set_default(name, value)
        self.files = files if files is not None else [
            MssqlFile("app", "ROWS", "/var/opt/mssql/data/app.mdf"),
            MssqlFile("app", "LOG", "/var/opt/mssql/data/app_log.ldf", reads_ps=0.0, writes_ps=100.0),
            MssqlFile("tempdb", "ROWS", "/var/opt/mssql/data/tempdb.mdf", reads_ps=5.0, writes_ps=5.0),
        ]
        for spec in self.files:
            spec.prepare(self.n)
        # wait type -> {"tasks_ps", "wait_ms_ps" (ms of waiting per second), "signal_ms_ps"} per-sample lists
        self.waits = {wait_type: {"tasks_ps": [1.0] * self.n, "wait_ms_ps": [0.0] * self.n, "signal_ms_ps": [0.0] * self.n}
                      for wait_type in MSSQL_WAIT_TYPES}

    def write(self):
        self.run_directory()
        v = self.values
        n = self.n
        zero = [0.0] * n
        columns = {
            "buffer_cache_hit_ratio": v["cache_hit_pct"], "page_life_expectancy": [5000] * n,
            "checkpoint_pages_total": self.counter([5.0] * n), "lazy_writes_total": self.counter(zero),
            "memory_grants_pending": v["memory_grants_pending"], "target_server_memory_kb": [4000000] * n,
            "total_server_memory_kb": [3900000] * n, "batch_requests_total": self.counter(v["batches_ps"]),
            "sql_compilations_total": self.counter(v["compilations_ps"]),
            "sql_recompilations_total": self.counter(zero), "user_connections": [30] * n, "processes_blocked": [0] * n,
            "lock_waits_total": self.counter(v["lock_waits_ps"]), "lock_wait_time_ms_total": self.counter(v["lock_wait_ms_ps"]),
            "deadlocks_total": self.counter(v["deadlocks_ps"]), "log_flushes_total": self.counter([40.0] * n),
            "log_flush_wait_time_ms_total": self.counter([40.0] * n), "log_bytes_flushed_total": self.counter([40000.0] * n),
            "page_reads_total": self.counter([20.0] * n), "page_writes_total": self.counter([10.0] * n),
            "free_list_stalls_total": self.counter(v["free_list_stalls_ps"]), "full_scans_total": self.counter([1.0] * n),
            "page_splits_total": self.counter([1.0] * n), "workfiles_created_total": self.counter(zero),
            "worktables_created_total": self.counter(zero), "memory_grants_outstanding": [0] * n,
            "active_requests": v["active_requests"], "runnable_tasks": v["runnable_tasks"],
            "work_queue_count": v["work_queue"], "active_workers": v["active_workers"],
            "busy_schedulers": v["busy_schedulers"],
            "server_start_marker": self.marker(int(self.synth.start) - 86400),
        }
        header = MSSQL_STATS_HEADER
        self.write_csv("mssql_stats.csv", header, lambda i: [[number_text(columns[name][i]) for name in header[1:]]])

        wait_columns = {}
        for wait_type, spec in self.waits.items():
            wait_columns[wait_type] = (self.counter(spec["tasks_ps"], 0), self.counter(spec["wait_ms_ps"], 0),
                                       self.counter(spec["signal_ms_ps"], 0))

        def wait_rows(index):
            return [[csv_text(wait_type), counts[index], times[index], signals[index]]
                    for wait_type, (counts, times, signals) in wait_columns.items()]
        self.write_csv("mssql_waits.csv", MSSQL_WAITS_HEADER, wait_rows)

        file_columns = []
        for spec in self.files:
            lists = spec.lists
            file_columns.append((
                spec, self.counter(lists["reads_ps"], 0),
                self.counter([r * l for r, l in zip(lists["reads_ps"], lists["read_latency_ms"])], 0),
                self.counter([r * spec.bytes_per_read for r in lists["reads_ps"]], 0),
                self.counter(lists["writes_ps"], 0),
                self.counter([w * l for w, l in zip(lists["writes_ps"], lists["write_latency_ms"])], 0),
                self.counter([w * spec.bytes_per_write for w in lists["writes_ps"]], 0)))

        def file_rows(index):
            rows = []
            for spec, reads, read_stall, read_bytes, writes, write_stall, write_bytes in file_columns:
                rows.append([csv_text(spec.database), csv_text(spec.file_type), csv_text(spec.path), reads[index],
                             read_stall[index], read_bytes[index], writes[index], write_stall[index], write_bytes[index]])
            return rows
        self.write_csv("mssql_files.csv", MSSQL_FILES_HEADER, file_rows)
        self.write_paths()
        for name, kind in MSSQL_COUNTER_TYPES.items():
            self.extra_manifest.setdefault("counter_type." + name, kind)
        self.write_manifest(["mssql_stats.csv", "mssql_waits.csv", "mssql_files.csv"])
        return self.directory
