"""Loading databases into a HostData: detection, merging, path mapping, canonical series, windows.

The collector runs come from synth_db.py; the OS runs from synth.py. Spec: docs/specs/db-collectors-v3.md
sections 8, 9, 12.6 and 13.3.
"""

import io
import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import bundle, dbdetect, dbengines, dbload, inspect_cmd, loader  # noqa: E402
from perfdiag.model import HostData, ProcessRow  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from helpers import write_text  # noqa: E402
from synth import BASE_EPOCH, Synth, hardware_for  # noqa: E402
from synth_db import DbSynth, MssqlFile  # noqa: E402

NAN = float("nan")

PIDSTAT_HEADER = (
    "Linux 6.1 (db01) \t2026-10-16 \t_x86_64_\t(2 CPU)\n\n"
    "# Time        UID       PID    %usr %system  %guest   %wait    %CPU   CPU  minflt/s  majflt/s     VSZ     RSS   "
    "%MEM   kB_rd/s   kB_wr/s kB_ccwr/s iodelay   cswch/s nvcswch/s  Command\n")


def pidstat_text(commands, first_sample=1, last_sample=20, interval=1):
    """A pidstat -h -l log with every command in every sample. commands: [(pid, cpu, rd, wr, text)]."""
    lines = [PIDSTAT_HEADER]
    for sample in range(first_sample, last_sample + 1):
        epoch = BASE_EPOCH + sample * interval
        for pid, cpu, rd, wr, command in commands:
            lines.append("%d 0 %d 0.00 0.00 0.00 0.00 %.2f 0 0.00 0.00 1000 500 0.01 %.2f %.2f 0.00 0 1.00 0.00  %s\n" % (
                epoch, pid, cpu, rd, wr, command))
    return "".join(lines)


def mean(values):
    found = [value for value in values if not math.isnan(value)]
    return sum(found) / len(found)


class DbLoadCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbload-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = DbSynth(self.tmp)

    def os_run(self, hardware=None, disks=(), pidstat=None, **kwargs):
        synth = Synth(self.tmp, **kwargs)
        for name in disks:
            synth.add_disk(name)
        synth.hardware = hardware
        run_directory = synth.write()
        if pidstat is not None:
            write_text(os.path.join(run_directory, "pidstat_20261016.log"), pidstat)
        return synth

    def load(self, host="db01", settings=None, include_partitions=False):
        with bundle.open_bundle(self.tmp) as opened:
            return loader.load_host(opened, host, include_partitions=include_partitions, settings=settings)

    def only_instance(self, host):
        self.assertEqual(len(host.databases), 1, [instance.id for instance in host.databases])
        return host.databases[0]

    def series_mean(self, instance, metric, sub=""):
        series = instance.get(metric, sub)
        self.assertIsNotNone(series, "%s %r missing; have %s" % (metric, sub, sorted(instance.series)))
        return mean(series.values)


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------
class NoDatabaseTests(DbLoadCase):
    def test_a_host_without_any_database_has_no_instances_and_unchanged_sources(self):
        self.os_run(pidstat=pidstat_text([(1, 5.0, 0.0, 0.0, "sshd -D"), (2, 1.0, 0.0, 0.0, "mysqld_safe --defaults-file=x"),
                                          (3, 1.0, 0.0, 0.0, "/usr/bin/mysqld_exporter"), (4, 1.0, 0.0, 0.0, "postgres_exporter")]))
        host = self.load()
        self.assertEqual(host.databases, [])
        self.assertEqual(host.data_sources, ["sadf: db01_os_20261016_151320"])
        self.assertTrue(all(not note.startswith("no os or psar") for note in host.notes))

    def test_inspect_prints_nothing_about_databases_without_instances(self):
        self.os_run()
        with bundle.open_bundle(self.tmp) as opened:
            out = io.StringIO()
            inspect_cmd.inspect_bundle(opened, out)
        self.assertNotIn("databases", out.getvalue())
        self.assertNotIn("Database", out.getvalue())
        self.assertIn("PSI:", out.getvalue())

    def test_a_host_with_nothing_to_analyse_is_still_refused(self):
        synth = Synth(self.tmp)
        synth.hardware = hardware_for({"vda": {"class": "ssd"}})
        synth.write()
        shutil.rmtree(os.path.join(self.tmp, "db01_os_20261016_151320"))
        with bundle.open_bundle(self.tmp) as opened:
            with self.assertRaises(loader.LoadError) as raised:
                loader.load_host(opened, "db01")
        self.assertIn("no os, psar or database run", str(raised.exception))


# ---------------------------------------------------------------------------
# Process detection
# ---------------------------------------------------------------------------
class ProcessDetectionTests(DbLoadCase):
    POSTGRES = [
        (100, 1.0, 10.0, 90.0, "postgres: checkpointer process"),
        (101, 0.5, 0.0, 20.0, "postgres: 16/main: background writer"),
        (102, 3.0, 0.0, 0.0, "postgres: app mydb 10.0.0.5(50122) SELECT"),
        (103, 0.1, 0.0, 0.0, "postgres: autovacuum worker mydb"),
        (104, 0.0, 0.0, 5.0, "postgres: walwriter"),
        (105, 0.0, 0.0, 0.0, "postgres: walsender repl 10.0.0.9(4000) streaming 0/3000148"),
        (99, 0.0, 0.0, 0.0, "/usr/pgsql-16/bin/postmaster -D /var/lib/pgsql/16/data"),
        (200, 1.0, 0.0, 0.0, "sshd -D"),
    ]

    def test_postgres_titles_alone_give_one_process_instance(self):
        self.os_run(pidstat=pidstat_text(self.POSTGRES))
        instance = self.only_instance(self.load())
        self.assertEqual(instance.source, "process")
        self.assertEqual(instance.engine, "postgresql")
        self.assertEqual(instance.short, "pg:proc")
        self.assertEqual(instance.collector_schema, 0)
        self.assertIs(instance.local, True)
        self.assertEqual(instance.process_keys, ["postgres", "postmaster"])
        self.assertEqual(instance.series, {})
        self.assertEqual(instance.paths["data"], ["/var/lib/pgsql/16/data"])
        roles = instance.process_roles
        self.assertEqual(roles["postgres: checkpointer process"], "checkpointer")
        self.assertEqual(roles["postgres: 16/main: background writer"], "background writer")
        self.assertEqual(roles["postgres: app mydb 10.0.0.5(50122) SELECT"], "client backend")
        self.assertEqual(roles["postgres: autovacuum worker mydb"], "autovacuum")
        self.assertEqual(roles["postgres: walwriter"], "WAL writer")
        self.assertEqual(roles["postgres: walsender repl 10.0.0.9(4000) streaming 0/3000148"], "replication")
        self.assertEqual(roles["/usr/pgsql-16/bin/postmaster -D /var/lib/pgsql/16/data"], "postmaster")
        self.assertNotIn("sshd -D", roles)

    def test_signature_table(self):
        found = {command: (dbdetect.match_command(command).engine if dbdetect.match_command(command) else None)
                 for command in (
                     "/usr/sbin/mysqld --basedir=/usr --datadir=/var/lib/mysql", "/usr/sbin/mariadbd", "mysqld_safe",
                     "/usr/bin/mysqld_exporter", "/opt/mssql/bin/sqlservr", "ora_pmon_ORCL", "oracleORCL (LOCAL=NO)",
                     "asm_pmon_+ASM", "/usr/pgsql-16/bin/postmaster -D /var/lib/pgsql/16/data", "postgres_exporter",
                     "python3 postgres.py", "|__postgres", "oracleasm-discover")}
        self.assertEqual(found, {
            "/usr/sbin/mysqld --basedir=/usr --datadir=/var/lib/mysql": "mysql", "/usr/sbin/mariadbd": "mariadb",
            "mysqld_safe": None, "/usr/bin/mysqld_exporter": None, "/opt/mssql/bin/sqlservr": "mssql",
            "ora_pmon_ORCL": "oracle", "oracleORCL (LOCAL=NO)": "oracle", "asm_pmon_+ASM": "asm",
            "/usr/pgsql-16/bin/postmaster -D /var/lib/pgsql/16/data": "postgresql", "postgres_exporter": None,
            "python3 postgres.py": None, "|__postgres": None, "oracleasm-discover": None})
        self.assertEqual(dbdetect.data_directory_in("mysqld --datadir=/var/lib/mysql --port=3306"), "/var/lib/mysql")
        self.assertEqual(dbdetect.data_directory_in("postgres -D /data/a -c config_file=/etc/x"), "/data/a")

    def test_a_data_directory_cut_off_by_pidstat_is_not_used(self):
        long_command = "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main -c config_file=/etc/x"
        self.os_run(pidstat=pidstat_text([(1, 1.0, 0.0, 0.0, long_command[:60])]))
        instance = self.only_instance(self.load())
        self.assertEqual(instance.paths, {})

    def test_oracle_roles_and_asm_is_not_a_database(self):
        commands = [(1, 1.0, 0.0, 0.0, "ora_pmon_ORCL"), (2, 1.0, 5.0, 50.0, "ora_dbw0_ORCL"), (3, 1.0, 0.0, 80.0, "ora_lgwr_ORCL"),
                    (4, 0.5, 0.0, 0.0, "ora_ckpt_ORCL"), (5, 0.5, 0.0, 0.0, "ora_arc1_ORCL"), (6, 2.0, 0.0, 0.0, "oracleORCL (LOCAL=NO)"),
                    (7, 0.5, 0.0, 0.0, "ora_lg00_ORCL"), (8, 0.0, 0.0, 0.0, "asm_pmon_+ASM")]
        self.os_run(pidstat=pidstat_text(commands))
        instance = self.only_instance(self.load())
        self.assertEqual(instance.engine, "oracle")
        self.assertEqual(instance.short, "ora:ORCL")
        self.assertEqual(instance.instance_name, "ORCL")
        self.assertEqual(instance.process_roles["ora_dbw0_ORCL"], "DB writer")
        self.assertEqual(instance.process_roles["ora_lgwr_ORCL"], "log writer")
        self.assertEqual(instance.process_roles["ora_lg00_ORCL"], "log writer")
        self.assertEqual(instance.process_roles["ora_ckpt_ORCL"], "checkpoint")
        self.assertEqual(instance.process_roles["ora_arc1_ORCL"], "archiver")
        self.assertEqual(instance.process_roles["ora_pmon_ORCL"], "background")
        self.assertEqual(instance.process_roles["oracleORCL (LOCAL=NO)"], "foreground")
        self.assertNotIn("asm_pmon_+ASM", instance.process_roles)

    def test_mysql_mariadb_and_sql_server_are_single_server_processes(self):
        commands = [(1, 5.0, 0.0, 0.0, "/usr/sbin/mysqld --datadir=/var/lib/mysql"), (2, 5.0, 0.0, 0.0, "/opt/mssql/bin/sqlservr")]
        self.os_run(pidstat=pidstat_text(commands))
        host = self.load()
        self.assertEqual([(i.engine, i.short) for i in host.databases], [("mysql", "my:proc"), ("mssql", "mssql:proc")])
        self.assertEqual(host.databases[0].process_roles, {"/usr/sbin/mysqld --datadir=/var/lib/mysql": "server"})
        self.assertEqual(host.databases[0].paths["data"], ["/var/lib/mysql"])

    def test_inventory_keys_give_process_instances_without_any_pidstat_data(self):
        hardware = hardware_for({"vda": {"class": "ssd"}}, extra={
            "dbproc.1.engine": "postgresql", "dbproc.1.pid": "1234", "dbproc.1.cwd": "/var/lib/pgsql/16/data",
            "dbproc.2.engine": "oracle", "dbproc.2.instance": "ORCL", "dbproc.2.cwd": "",
            "dbproc.3.engine": "mysql", "dbproc.3.cwd": "", "dbproc.3.cwd_note": "unreadable (run as root)",
            "dbproc.4.engine": "bogus"})
        self.os_run(hardware=hardware)
        host = self.load()
        self.assertEqual([(i.engine, i.short, i.instance_name) for i in host.databases],
                         [("postgresql", "pg:proc", ""), ("mysql", "my:proc", ""), ("oracle", "ora:ORCL", "ORCL")])
        self.assertEqual(host.databases[0].paths, {"data": ["/var/lib/pgsql/16/data"]})
        self.assertEqual(host.databases[1].paths, {})
        self.assertIn("MySQL process working directory: unreadable (run as root), so its data location is unknown",
                      host.databases[1].notes)
        self.assertEqual(host.databases[2].paths, {})

    def test_inventory_and_pidstat_describe_one_server(self):
        hardware = hardware_for({"vda": {"class": "ssd"}}, extra={
            "dbproc.1.engine": "oracle", "dbproc.1.instance": "ORCL"})
        self.os_run(hardware=hardware, pidstat=pidstat_text([(1, 1.0, 0.0, 50.0, "ora_dbw0_ORCL")]))
        instance = self.only_instance(self.load())
        self.assertEqual((instance.short, instance.process_keys), ("ora:ORCL", ["ora_dbw0_ORCL"]))
        self.assertEqual(instance.process_roles, {"ora_dbw0_ORCL": "DB writer"})

    def test_share_of_samples_in_which_the_process_was_seen(self):
        rows = [ProcessRow(time=1, pid=1, cpu_pct=1.0, rss_kb=0, majflt_ps=0, rd_kBps=0, wr_kBps=0, iodelay=0, command="postgres: x"),
                ProcessRow(time=2, pid=2, cpu_pct=1.0, rss_kb=0, majflt_ps=0, rd_kBps=0, wr_kBps=0, iodelay=0, command="sshd")]
        host = HostData(host="h", process_rows=rows)
        self.assertEqual(dbdetect.process_seen_share(host, ["postgres"]), 0.5)
        self.assertIsNone(dbdetect.process_seen_share(host, []))


# ---------------------------------------------------------------------------
# Collector instances and merging with process data
# ---------------------------------------------------------------------------
class CollectorInstanceTests(DbLoadCase):
    def test_collector_instance_fields(self):
        self.os_run()
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data", notes="track_io_timing is off | second note")
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.id, "postgresql:localhost:5432")
        self.assertEqual(instance.short, "pg:5432")
        self.assertEqual(instance.label, "PostgreSQL 16.4 (localhost:5432)")
        self.assertEqual(instance.source, "collector")
        self.assertIs(instance.local, True)
        self.assertEqual(instance.locality_basis, "socket")
        self.assertEqual(instance.version, "16.4")
        self.assertEqual(instance.version_num, 160004)
        self.assertEqual(instance.collector_schema, 3)
        self.assertEqual(instance.settings["block_size"], "8192")
        self.assertEqual(instance.paths["data"], ["/var/lib/pgsql/16/data"])
        self.assertEqual(instance.paths["wal"], ["/var/lib/pgsql/16/data/pg_wal"])
        self.assertIn("track_io_timing is off", instance.notes)
        self.assertIn("second note", instance.notes)
        self.assertEqual(instance.tables_present, ["pg_stats.csv", "pg_database.csv", "pg_waits.csv"])
        self.assertEqual(instance.sample_count, 120)
        self.assertEqual(instance.interval_s, 1)
        self.assertEqual(len(instance.runs), 1)

    def test_database_series_stay_out_of_the_host_series(self):
        self.os_run()
        self.db.postgresql().write()
        host = self.load()
        self.assertTrue(all(not key[1].startswith("db.") for key in host.series))
        self.assertIsNone(host.get("db.transactions_ps"))
        self.assertEqual(host.devices("db.transactions_ps"), [])
        self.assertIn("db: db01_postgresql_20261016_151320", host.data_sources)

    def test_remote_target_with_os_data_is_not_local_and_gets_no_process_or_device_data(self):
        self.os_run(hardware=hardware_for({"vda": {"class": "ssd"}}, extra={
            "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda"}),
            pidstat=pidstat_text([(1, 3.0, 0.0, 0.0, "postgres: checkpointer")]))
        self.db.postgresql(instance="dbhost.example.com:5432", local=False).write()
        host = self.load()
        collector, process = host.databases
        self.assertIs(collector.local, False)
        self.assertEqual(collector.source, "collector")
        self.assertEqual(collector.paths, {})
        self.assertEqual(collector.devices, {})
        self.assertEqual(collector.process_keys, [])
        self.assertEqual(collector.short, "pg:5432")
        self.assertIsNone(collector.get("db.cpu_demand_per_cpu"))        # no per-CPU metric for a remote PostgreSQL
        self.assertEqual(process.source, "process")                     # the local postgres is another server
        self.assertEqual(process.process_keys, ["postgres"])

    def test_local_collector_instance_takes_the_process_data(self):
        self.os_run(pidstat=pidstat_text(ProcessDetectionTests.POSTGRES))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.source, "collector")
        self.assertEqual(instance.process_keys, ["postgres", "postmaster"])
        self.assertEqual(instance.process_roles["postgres: checkpointer process"], "checkpointer")

    def test_two_local_clusters_are_split_by_data_directory(self):
        commands = [(1, 1.0, 0.0, 0.0, "postgres -D /data/a"), (2, 1.0, 0.0, 0.0, "postgres: checkpointer")]
        hardware = hardware_for({"vda": {"class": "ssd"}}, extra={
            "dbproc.1.engine": "postgresql", "dbproc.1.cwd": "/data/a", "dbproc.2.engine": "postgresql",
            "dbproc.2.cwd": "/data/b"})
        self.os_run(hardware=hardware, pidstat=pidstat_text(commands))
        self.db.postgresql(instance="localhost:5432", data_directory="/data/a", stamp="20261016_151320").write()
        self.db.postgresql(instance="localhost:5433", data_directory="/data/b", stamp="20261016_151321").write()
        first, second = self.load().databases
        self.assertEqual((first.short, second.short), ("pg:5432", "pg:5433"))
        self.assertEqual(first.process_keys, ["postgres"])
        self.assertEqual(second.process_keys, [])
        self.assertFalse([note for note in first.notes if "could not be split" in note])

    def test_two_local_clusters_without_a_data_directory_get_the_process_data_and_a_note(self):
        self.os_run(pidstat=pidstat_text([(2, 1.0, 0.0, 0.0, "postgres: checkpointer")]))
        self.db.postgresql(instance="localhost:5432", data_directory="/data/a", stamp="20261016_151320").write()
        self.db.postgresql(instance="localhost:5433", data_directory="/data/b", stamp="20261016_151321").write()
        for instance in self.load().databases:
            self.assertEqual(instance.process_keys, ["postgres"])
            self.assertIn("process data could not be split between the PostgreSQL instances on this host", instance.notes)

    def test_oracle_process_with_the_same_sid_joins_the_collector_instance(self):
        self.os_run(pidstat=pidstat_text([(1, 1.0, 0.0, 50.0, "ora_dbw0_ORCL"), (2, 1.0, 0.0, 0.0, "ora_pmon_OTHER")]))
        self.db.oracle(instance="ORCL/PDB1").write()
        collector, process = self.load().databases
        self.assertEqual((collector.short, collector.instance_name), ("ora:ORCL/PDB1", "ORCL"))
        self.assertEqual(collector.process_keys, ["ora_dbw0_ORCL"])
        self.assertEqual((process.source, process.short), ("process", "ora:OTHER"))

    def test_short_names_per_engine(self):
        self.assertEqual(dbload.short_name("postgresql", "localhost:5432"), "pg:5432")
        self.assertEqual(dbload.short_name("postgresql", "/var/run/postgresql:5433"), "pg:5433")
        self.assertEqual(dbload.short_name("mysql", "127.0.0.1:3306"), "my:3306")
        self.assertEqual(dbload.short_name("mariadb", "localhost:3307"), "my:3307")
        self.assertEqual(dbload.short_name("oracle", "ORCL/PDB1"), "ora:ORCL/PDB1")
        self.assertEqual(dbload.short_name("mssql", "MSSQLSERVER"), "mssql:MSSQLSERVER")
        self.assertEqual(dbload.label_for("mariadb", "10.11.6-MariaDB-0ubuntu0.23.10.1", "localhost:3306"),
                         "MariaDB 10.11.6 (localhost:3306)")

    def test_the_same_port_on_two_servers_gets_distinct_short_names(self):
        self.os_run()
        self.db.postgresql(instance="dbhost1:5432", local=False, stamp="20261016_151320").write()
        self.db.postgresql(instance="dbhost2:5432", local=False, stamp="20261016_151321").write()
        host = self.load()
        self.assertEqual([instance.short for instance in host.databases], ["pg:5432", "pg:dbhost2:5432"])
        self.assertEqual(len({instance.id for instance in host.databases}), 2)

    def test_mariadb_engine_comes_from_the_manifest(self):
        self.os_run()
        self.db.mysql(mariadb=True, version="10.11.6-MariaDB").write()
        instance = self.only_instance(self.load())
        self.assertEqual((instance.engine, instance.short, instance.label), ("mariadb", "my:3306", "MariaDB 10.11.6 (localhost:3306)"))

    def test_run_directory_renamed_is_found_by_script_name(self):
        path = os.path.join(self.tmp, "renamed")
        os.makedirs(path)
        for script, tag in (("collect-postgresql-metrics.sh", "postgresql"), ("collect-mysql-metrics.sh", "mysql"),
                            ("collect-oracle-metrics.sh", "oracle"), ("collect-mssql-metrics.sh", "mssql"),
                            ("collect-os-metrics.sh", "os")):
            write_text(os.path.join(path, "manifest.txt"), "script=%s\nhostname=db01\n" % script)
            self.assertEqual(bundle._run_from_directory(path).tag, tag)


# ---------------------------------------------------------------------------
# Version 2 runs
# ---------------------------------------------------------------------------
class VersionTwoTests(DbLoadCase):
    def test_a_2_0_0_postgresql_run_loads_with_the_coverage_note(self):
        self.os_run()
        self.db.postgresql_v2().write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.collector_schema, 2)
        self.assertEqual(instance.short, "pg:5432")
        self.assertEqual(instance.id, "postgresql:localhost:5432")
        self.assertIs(instance.local, True)
        self.assertIn("PostgreSQL pg:5432: collected with collect-postgresql-metrics.sh 2.0.0. Database counters cover "
                      "the postgres database only; waits, IO timing and file placement are not available. "
                      "Re-run with 3.0.0.", instance.notes)
        self.assertAlmostEqual(self.series_mean(instance, "db.transactions_ps"), 51.0, places=1)
        self.assertGreater(self.series_mean(instance, "db.cache_hit_pct"), 98.0)
        self.assertEqual(instance.paths, {})
        self.assertIsNone(instance.get("db.wait.cpu"))
        self.assertIsNone(instance.get("db.read_latency_ms"))
        self.assertEqual(instance.tables_present, ["pg_stats.csv"])

    def test_a_current_run_has_no_coverage_note(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertFalse([note for note in self.only_instance(self.load()).notes if "Re-run with 3.0.0" in note])


# ---------------------------------------------------------------------------
# Host windows
# ---------------------------------------------------------------------------
class WindowTests(DbLoadCase):
    def test_database_only_bundle_loads_and_takes_its_window_from_the_database(self):
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        host = self.load()
        instance = self.only_instance(host)
        self.assertEqual(host.series, {})
        self.assertEqual((host.start, host.end), (BASE_EPOCH, BASE_EPOCH + 120))
        self.assertEqual(host.interval_s, 1)
        self.assertEqual(host.cpus, 2)
        self.assertFalse(host.psi_expected)
        self.assertTrue([note for note in host.notes if "only database collector data" in note])
        self.assertIsNotNone(instance.get("db.transactions_ps"))
        self.assertEqual(instance.devices, {})
        self.assertEqual(host.gaps, [])

    def test_database_only_bundle_inspect_output(self):
        self.db.postgresql().write()
        with bundle.open_bundle(self.tmp) as opened:
            out = io.StringIO()
            status = inspect_cmd.inspect_bundle(opened, out)
        self.assertEqual(status, 0)
        self.assertIn("db01_postgresql_20261016_151320", out.getvalue())
        self.assertNotIn("PSI", out.getvalue())

    def test_database_series_are_clipped_to_the_os_window(self):
        self.os_run(samples=60)
        run = self.db.postgresql()
        run.reset_counters_at = [100]
        run.write()
        host = self.load()
        instance = self.only_instance(host)
        self.assertEqual(host.end, BASE_EPOCH + 60)
        for series in instance.series.values():
            self.assertLessEqual(series.times[-1], host.end)
            self.assertGreaterEqual(series.times[0], host.start)
        self.assertEqual(instance.resets, [])             # the reset is after the window

    def test_a_run_covering_less_than_half_of_the_window_is_noted(self):
        self.os_run(samples=120, interval=10)
        DbSynth(self.tmp, samples=30, interval=10).postgresql().write()
        instance = self.only_instance(self.load())
        notes = [note for note in instance.notes if "run covers" in note]
        self.assertEqual(len(notes), 1)
        self.assertRegex(notes[0], r"^postgresql run covers \d+ min of the 20 min window$")

    def test_a_run_covering_the_window_has_no_coverage_note(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertFalse([note for note in self.only_instance(self.load()).notes if "run covers" in note])

    def test_apply_window_clips_instance_series_and_resets(self):
        self.os_run()
        run = self.db.postgresql()
        run.reset_counters_at = [20, 90]
        run.write()
        host = self.load()
        instance = self.only_instance(host)
        self.assertEqual(instance.resets, [BASE_EPOCH + 21, BASE_EPOCH + 91])
        loader.apply_window(host, BASE_EPOCH + 50, BASE_EPOCH + 100)
        self.assertEqual(instance.resets, [BASE_EPOCH + 91])
        for series in instance.series.values():
            self.assertTrue(all(BASE_EPOCH + 50 <= time <= BASE_EPOCH + 100 for time in series.times))

    def test_settings_change_the_minimum_denominators(self):
        self.os_run()
        run = self.db.postgresql()
        run.fill("blks_read_ps", 50, 60, 2.0)
        run.write()
        quiet = self.only_instance(self.load()).get("db.read_latency_ms")
        self.assertTrue(all(math.isnan(v) for t, v in quiet.points() if BASE_EPOCH + 52 <= t <= BASE_EPOCH + 60))
        settings = Settings()
        settings.set("db.min_reads_ps", "1", "test")
        busy = self.only_instance(self.load(settings=settings)).get("db.read_latency_ms")
        self.assertFalse(any(math.isnan(v) for t, v in busy.points() if BASE_EPOCH + 52 <= t <= BASE_EPOCH + 60))


# ---------------------------------------------------------------------------
# Path to device mapping
# ---------------------------------------------------------------------------
class PathMappingTests(DbLoadCase):
    def hardware(self, **extra):
        values = {
            "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda1", "fs.1.fstype": "xfs", "fs.1.kname": "vda1",
            "fs.2.mountpoint": "/var/lib/pgsql", "fs.2.source": "/dev/mapper/vg-data", "fs.2.fstype": "xfs",
            "fs.2.kname": "dm-0",
            "part.sdb1.parent": "sdb", "part.vda1.parent": "vda",
        }
        values.update(extra)
        return hardware_for({
            "vda": {"class": "ssd"}, "sdb": {"class": "nvme"},
            "dm-0": {"class": "stacked", "slaves": ["sdb1"], "display": "vg-data"}}, extra=values)

    def test_inventory_data_directory_maps_through_dm_and_a_partition(self):
        hardware = self.hardware(**{"dbproc.1.engine": "postgresql", "dbproc.1.cwd": "/var/lib/pgsql/16/data"})
        self.os_run(hardware=hardware, disks=("sdb", "vg-data"))
        instance = self.only_instance(self.load())
        self.assertEqual(instance.source, "process")
        self.assertEqual(instance.paths["data"], ["/var/lib/pgsql/16/data"])
        self.assertEqual(instance.devices["data"], ["vg-data", "sdb"])

    def test_collector_paths_map_per_role(self):
        self.os_run(hardware=self.hardware(), disks=("sdb", "vg-data"))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data", wal_directory="/var/lib/wal").write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.devices["data"], ["vg-data", "sdb"])
        self.assertEqual(instance.devices["wal"], ["vda"])
        self.assertEqual(instance.notes, [])

    def test_inventory_data_directory_is_used_when_the_collector_could_not_read_it(self):
        hardware = self.hardware(**{"dbproc.1.engine": "postgresql", "dbproc.1.cwd": "/var/lib/pgsql/16/data"})
        self.os_run(hardware=hardware, disks=("sdb", "vg-data"))
        run = self.db.postgresql()
        run.paths = {}
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.source, "collector")
        self.assertEqual(instance.paths["data"], ["/var/lib/pgsql/16/data"])
        self.assertEqual(instance.devices["data"], ["vg-data", "sdb"])

    def test_map_path_rules(self):
        from perfdiag.readers.manifest import parse_hardware
        hardware = parse_hardware(self.hardware())
        names = {"vda", "sdb", "vg-data"}
        self.assertEqual(dbload.map_path("/var/lib/pgsql", hardware, names)[0], ["vg-data", "sdb"])
        self.assertEqual(dbload.map_path("/var/lib/pgsql/", hardware, names)[0], ["vg-data", "sdb"])
        self.assertEqual(dbload.map_path("/var/lib/pgsql/../pgsql/16", hardware, names)[0], ["vg-data", "sdb"])
        # A longer name that merely starts the same way is on the root file system, not on /var/lib/pgsql.
        self.assertEqual(dbload.map_path("/var/lib/pgsqlbackup/x", hardware, names)[0], ["vda"])
        self.assertEqual(dbload.map_path("/dev/mapper/vg-data", hardware, names)[0], ["vg-data", "sdb"])
        self.assertEqual(dbload.map_path("/dev/sdb1", hardware, names)[0], ["sdb"])
        self.assertEqual(dbload.map_path("/dev/vda", hardware, names)[0], ["vda"])
        self.assertEqual(dbload.map_path("/dev/vda", hardware, {"sdb"})[0], [])     # not in the disk data
        self.assertEqual(dbload.map_path("+DATA", hardware, names), ([], ""))
        oracleasm = dbload.map_path("/dev/oracleasm/disks/DATA1", hardware, names)
        self.assertEqual(oracleasm[0], [])
        self.assertIn("oracleasm", oracleasm[1])
        self.assertIn("AFD:DATA1", dbload.map_path("AFD:DATA1", hardware, names)[1])
        self.assertEqual(dbload.map_path("/x", None, names), ([], ""))

    def test_oracle_asm_disks_map_as_their_own_role(self):
        self.os_run(hardware=self.hardware(), disks=("sdb", "vg-data"))
        run = self.db.oracle()
        run.paths = {"data": ["+DATA"], "asm_disk": ["/dev/sdb1", "/dev/oracleasm/disks/DATA2"]}
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.devices, {"asm_disk": ["sdb"]})
        self.assertTrue([note for note in instance.notes if "oracleasm" in note])

    def test_no_inventory_gives_one_note_and_no_mapping(self):
        self.os_run()
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.devices, {})
        self.assertEqual(instance.notes, ["no hardware inventory: PostgreSQL files could not be placed on devices"])

    def test_paths_without_a_matching_filesystem_are_noted(self):
        hardware = hardware_for({"vda": {"class": "ssd"}})
        self.os_run(hardware=hardware)
        self.db.postgresql(data_directory="/srv/pg/data").write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.devices, {})
        self.assertIn("data path /srv/pg/data could not be matched to a disk in the data", instance.notes)


# ---------------------------------------------------------------------------
# Canonical series per engine
# ---------------------------------------------------------------------------
class PostgresSeriesTests(DbLoadCase):
    def test_rates_ratios_and_basis(self):
        self.os_run()
        run = self.db.postgresql()
        run.fill("blks_read_ps", 30, 90, 100.0)
        run.fill("read_latency_ms", 30, 90, 40.0)
        run.fill("temp_bytes_ps", 0, 120, 20480.0)
        run.fill("ckpt_req_ps", 40, 41, 1.0)
        run.write()
        instance = self.only_instance(self.load())
        read_latency = instance.get("db.read_latency_ms")
        self.assertAlmostEqual(dict(read_latency.points())[BASE_EPOCH + 60], 40.0)
        self.assertAlmostEqual(dict(instance.get("db.read_kBps").points())[BASE_EPOCH + 60], 100 * 8)
        self.assertAlmostEqual(self.series_mean(instance, "db.temp_kBps"), 20.0)
        self.assertAlmostEqual(dict(instance.get("db.transactions_ps").points())[BASE_EPOCH + 60], 51.0)
        # hits 2000/s, reads 100/s -> 95.2 %
        self.assertAlmostEqual(dict(instance.get("db.cache_hit_pct").points())[BASE_EPOCH + 60], 100 * 2000 / 2100.0, places=3)
        # buffers per second (10 + 5 + 1) x 8 kB
        self.assertAlmostEqual(self.series_mean(instance, "db.write_kBps"), 16 * 8, places=1)
        self.assertAlmostEqual(self.series_mean(instance, "db.backend_writes_pct"), 100.0 / 16, places=2)
        self.assertEqual(dict(instance.get("db.checkpoints", "requested").points())[BASE_EPOCH + 41], 1.0)
        self.assertEqual(dict(instance.get("db.checkpoints", "requested").points())[BASE_EPOCH + 60], 0.0)
        self.assertEqual(instance.get("db.checkpoints", "timed").values.count(0.0), 119)
        self.assertEqual(self.series_mean(instance, "db.sessions_active"), 2.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.connections_pct"), 12.0)
        self.assertEqual(instance.basis["db.read_latency_ms"], "time-averaged")
        self.assertEqual(instance.basis["db.sessions_active"], "sampled")
        self.assertEqual(instance.basis["db.wait.io_read"], "sampled")
        self.assertEqual(instance.get("db.transactions_ps").source, "db")
        self.assertEqual(instance.get("db.transactions_ps").interval_s, 1)

    def test_read_latency_needs_io_timing(self):
        self.os_run()
        self.db.postgresql(with_io_timing=False).write()
        instance = self.only_instance(self.load())
        self.assertIsNone(instance.get("db.read_latency_ms"))
        self.assertIsNotNone(instance.get("db.read_kBps"))

    def test_wait_categories_and_detail_names(self):
        self.os_run()
        run = self.db.postgresql()
        n = run.n
        run.waits = {
            ("CPU", "CPU"): [1] * n, ("IO", "DataFileRead"): [2] * n, ("IO", "DataFileWrite"): [1] * n,
            ("IO", "WALWrite"): [3] * n, ("LWLock", "WALWriteLock"): [1] * n, ("Lock", "transactionid"): [4] * n,
            ("BufferPin", "BufferPin"): [1] * n, ("IPC", "SyncRep"): [2] * n, ("IPC", "ProcArrayGroupUpdate"): [1] * n,
            ("Client", "ClientRead"): [5] * n, ("Timeout", "PgSleep"): [1] * n, ("LWLock", "buffer_content"): [2] * n}
        run.write()
        instance = self.only_instance(self.load())
        expected = {"cpu": 1, "io_read": 2, "io_write": 1, "commit": 3 + 1, "lock": 4 + 1, "latch": 2,
                    "replica": 2, "network": 5, "other": 1 + 1}
        for category, sessions in expected.items():
            self.assertEqual(self.series_mean(instance, "db.wait." + category), sessions, category)
        self.assertEqual(self.series_mean(instance, "db.wait.io_read", "IO:DataFileRead"), 2)
        self.assertEqual(instance.subs("db.wait.latch"), ["LWLock:buffer_content"])
        self.assertAlmostEqual(self.series_mean(instance, "db.cpu_demand_per_cpu"), 0.5)

    def test_wait_category_function(self):
        category = dbengines.postgres_wait_category
        self.assertEqual(category("CPU", "CPU"), "cpu")
        self.assertEqual(category("IO", "WALSync"), "commit")
        self.assertEqual(category("LWLock", "WALInsert"), "commit")
        self.assertEqual(category("IO", "DataFileRead"), "io_read")
        self.assertEqual(category("IO", "BufFileRead"), "io_read")
        self.assertEqual(category("IO", "DataFileExtend"), "io_write")
        self.assertEqual(category("Lock", "relation"), "lock")
        self.assertEqual(category("BufferPin", "BufferPin"), "lock")
        self.assertEqual(category("LWLock", "lock_manager"), "latch")
        self.assertEqual(category("IPC", "SyncRep"), "replica")
        self.assertEqual(category("IPC", "BgWorkerStartup"), "other")
        self.assertEqual(category("Client", "ClientWrite"), "network")
        for wait_type in ("Timeout", "Extension", "Activity"):
            self.assertEqual(category(wait_type, "Anything"), "other")

    def test_a_sample_without_waits_counts_as_zero_sessions(self):
        self.os_run()
        run = self.db.postgresql()
        run.waits = {("IO", "DataFileRead"): [2 if 10 <= i < 20 else 0 for i in range(run.n)]}
        run.write()
        series = self.only_instance(self.load()).get("db.wait.io_read")
        self.assertEqual(len(series), 120)
        self.assertEqual(series.values[5], 0.0)
        self.assertEqual(series.values[15], 2.0)

    def test_pg_database_names_with_commas_are_read_correctly(self):
        self.os_run()
        run = self.db.postgresql()
        run.databases = {"app,db": [30.0] * run.n, 'say "hi"': [5.0] * run.n}
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.subs("db.database_transactions_ps"), ['app,db', 'say "hi"'])
        self.assertAlmostEqual(self.series_mean(instance, "db.database_transactions_ps", "app,db"), 30.0)

    def test_rows_with_the_wrong_field_count_are_skipped_and_counted(self):
        self.os_run()
        run = self.db.postgresql()
        run.bad_rows = {10: {"pg_stats.csv": ["2026-10-16T15:13:31+00:00,1,2,3", "Time: 0.5 ms"]},
                        11: {"pg_waits.csv": ["2026-10-16T15:13:32+00:00,CPU"]}}
        run.write()
        instance = self.only_instance(self.load())
        self.assertIn("pg_stats.csv: 2 rows skipped because the field count differs from the header", instance.notes)
        self.assertIn("pg_waits.csv: 1 row skipped because the field count differs from the header", instance.notes)
        self.assertEqual(instance.sample_count, 120)
        self.assertFalse(any(math.isnan(v) for v in instance.get("db.transactions_ps").values))

    def test_a_run_without_samples_still_creates_an_instance_with_a_note(self):
        self.os_run()
        run = self.db.postgresql()
        run.skip_ranges = [(0, run.n)]
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.series, {})
        self.assertTrue([note for note in instance.notes if "pg_stats.csv is missing or has no rows" in note])


class ResetAndGapTests(DbLoadCase):
    def points(self, instance, metric="db.transactions_ps"):
        return dict(instance.get(metric).points())

    def test_a_counter_reset_gives_nan_at_that_sample_and_lists_the_reset(self):
        self.os_run()
        run = self.db.postgresql()
        run.reset_counters_at = [60]
        run.write()
        instance = self.only_instance(self.load())
        reset_time = BASE_EPOCH + 61
        self.assertEqual(instance.resets, [reset_time])
        for key, series in instance.series.items():
            if instance.basis[key[0]] == "time-averaged":
                self.assertTrue(math.isnan(dict(series.points())[reset_time]), key)
            self.assertTrue(all(math.isnan(v) or v >= 0 for v in series.values), "negative value in %s" % (key,))
        self.assertAlmostEqual(self.points(instance)[reset_time + 1], 51.0)
        self.assertAlmostEqual(self.points(instance)[reset_time - 1], 51.0)
        self.assertEqual(sum(math.isnan(v) for v in instance.get("db.transactions_ps").values), 1)

    def test_a_marker_change_is_a_reset_even_when_every_counter_grew(self):
        self.os_run()
        run = self.db.postgresql()
        run.marker_changes_at = [40]
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.resets, [BASE_EPOCH + 41])
        self.assertTrue(math.isnan(self.points(instance)[BASE_EPOCH + 41]))
        self.assertAlmostEqual(self.points(instance)[BASE_EPOCH + 42], 51.0)

    def test_mysql_uptime_going_back_is_a_reset(self):
        self.os_run()
        run = self.db.mysql()
        run.reset_counters_at = [50]
        run.write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.resets, [BASE_EPOCH + 51])
        self.assertTrue(math.isnan(self.points(instance, "db.statements_ps")[BASE_EPOCH + 51]))

    def test_a_gap_over_twice_the_interval_gives_nan_but_one_missing_sample_does_not(self):
        self.os_run()
        run = self.db.postgresql()
        run.skip_ranges = [(30, 40), (60, 60)]
        run.write()
        instance = self.only_instance(self.load())
        points = self.points(instance)
        self.assertTrue(math.isnan(points[BASE_EPOCH + 42]))                # sample 41 follows a 12 s gap
        self.assertAlmostEqual(points[BASE_EPOCH + 62], 51.0)               # sample 61 follows one missing sample: 2 s
        self.assertAlmostEqual(points[BASE_EPOCH + 43], 51.0)
        self.assertEqual(instance.resets, [])
        self.assertEqual(sorted(set(BASE_EPOCH + i + 1 for i in range(120)) - set(points) - {BASE_EPOCH + 1}),
                         sorted([BASE_EPOCH + i + 1 for i in range(30, 41)] + [BASE_EPOCH + 61]))


class MysqlSeriesTests(DbLoadCase):
    def test_file_io_waits_latency_and_lock_figures(self):
        self.os_run()
        run = self.db.mysql()
        run.fill("data_read_latency_ms", 0, 120, 2.0)
        run.fill("data_reads_ps", 0, 120, 100.0)
        run.fill("redo_write_latency_ms", 0, 120, 5.0)
        run.fill("row_lock_ms_ps", 0, 120, 500.0)
        run.fill("row_lock_waits_ps", 0, 120, 10.0)
        run.fill("tmp_disk_ps", 0, 120, 3.0)
        run.write()
        instance = self.only_instance(self.load())
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms"), 2.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.commit_latency_ms"), 5.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.io_read"), 0.2)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.commit"), 40 * 0.005)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.lock"), 0.5)
        self.assertAlmostEqual(self.series_mean(instance, "db.lock_wait_avg_ms"), 50.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.temp_tables_ps"), 3.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.dirty_pct"), 100 * 100 / 8192.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.concurrency_per_cpu"), 1.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.connections_pct"), 100 * 20 / 151.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.write_kBps"), 120000 / 1024.0)
        self.assertEqual(instance.basis["db.wait.lock"], "time-averaged")

    def test_without_performance_schema_there_are_no_file_figures(self):
        self.os_run()
        self.db.mysql(with_file_io=False).write()
        instance = self.only_instance(self.load())
        self.assertIsNone(instance.get("db.read_latency_ms"))
        self.assertIsNone(instance.get("db.wait.io_read"))
        self.assertIsNotNone(instance.get("db.statements_ps"))

    def test_file_event_groups(self):
        self.assertEqual(dbengines.mysql_file_group("wait/io/file/innodb/innodb_data_file"), "data")
        self.assertEqual(dbengines.mysql_file_group("wait/io/file/innodb/innodb_log_file"), "redo")
        self.assertEqual(dbengines.mysql_file_group("wait/io/file/innodb/innodb_temp_file"), "temp")
        self.assertEqual(dbengines.mysql_file_group("wait/io/file/innodb/innodb_dblwr_file"), "")

    def test_remote_mysql_has_no_per_cpu_metric(self):
        self.os_run()
        self.db.mysql(local=False).write()
        self.assertIsNone(self.only_instance(self.load()).get("db.concurrency_per_cpu"))


class OracleSeriesTests(DbLoadCase):
    def test_events_classes_cpu_and_latency(self):
        self.os_run()
        run = self.db.oracle()
        n = run.n
        run.events["db file sequential read"] = {"waits_ps": [20.0] * n, "latency_ms": [10.0] * n}
        run.events["log file sync"] = {"waits_ps": [40.0] * n, "latency_ms": [5.0] * n}
        run.events["enq: TX - row lock contention"] = {"waits_ps": [2.0] * n, "latency_ms": [100.0] * n}
        run.events["resmgr:cpu quantum"] = {"waits_ps": [5.0] * n, "latency_ms": [100.0] * n}
        run.events["log file switch (checkpoint incomplete)"] = {"waits_ps": [1.0] * n, "latency_ms": [1.0] * n}
        run.class_extra["User I/O"] = [0.3] * n               # time in User I/O events the list does not name
        run.class_extra["Scheduler"] = [0.0] * n
        run.write()
        instance = self.only_instance(self.load())
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms"), 10.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.commit_latency_ms"), 5.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.io_read"), 20 * 0.010 + 0.3)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.io_read", "class:User I/O"), 0.3)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.commit"), 40 * 0.005 + 1 * 0.001)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.lock"), 0.2)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.cpu"), 1.0 + 0.5)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.cpu", "resmgr:cpu quantum"), 0.5)
        self.assertAlmostEqual(self.series_mean(instance, "db.cpu_demand_per_cpu"), 0.75)
        self.assertAlmostEqual(self.series_mean(instance, "db.checkpoints", "incomplete"), 1.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.cache_hit_pct"), 100 * (1 - 20 / 5000.0))
        self.assertAlmostEqual(self.series_mean(instance, "db.read_kBps"), 200000 / 1024.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.connections_pct"), 100 * 60 / 300.0)
        self.assertEqual(self.series_mean(instance, "db.sessions_by_class", "ON CPU"), 1.0)
        self.assertEqual(instance.basis["db.wait.io_read"], "time-averaged")

    def test_unlimited_limits_leave_connections_blank(self):
        self.os_run()
        run = self.db.oracle()
        run.fill("processes_limit", 0, run.n, "")
        run.fill("sessions_limit", 0, run.n, "")
        run.write()
        self.assertIsNone(self.only_instance(self.load()).get("db.connections_pct"))

    def test_event_and_class_mapping_functions(self):
        category = dbengines.oracle_event_category
        for name in ("db file sequential read", "db file scattered read", "direct path read", "read by other session"):
            self.assertEqual(category(name), "io_read", name)
        for name in ("direct path read temp", "direct path write temp"):
            self.assertEqual(category(name), "temp", name)
        for name in ("log file sync", "log buffer space", "log file switch (checkpoint incomplete)", "log file switch completion"):
            self.assertEqual(category(name), "commit", name)
        self.assertEqual(category("free buffer waits"), "io_write")
        for name in ("enq: TX - row lock contention", "enq: TM - contention"):
            self.assertEqual(category(name), "lock", name)
        for name in ("latch: cache buffers chains", "library cache: mutex X", "cursor: pin S wait on X", "buffer busy waits",
                     "enq: TX - index contention"):
            self.assertEqual(category(name), "latch", name)
        self.assertEqual(category("resmgr:cpu quantum"), "cpu")
        self.assertEqual(category("gc buffer busy acquire"), "replica")
        self.assertEqual(category("log file parallel write"), "")         # decided by its class, System I/O
        self.assertEqual(dbengines.ORACLE_CLASS_CATEGORY["User I/O"], "io_read")
        self.assertEqual(dbengines.ORACLE_CLASS_CATEGORY["System I/O"], "io_write")
        self.assertEqual(dbengines.ORACLE_CLASS_CATEGORY["Application"], "lock")
        self.assertEqual(dbengines.ORACLE_CLASS_CATEGORY["Concurrency"], "latch")


class SqlServerSeriesTests(DbLoadCase):
    def hardware(self):
        return hardware_for({"sdb": {"class": "ssd"}, "sdc": {"class": "ssd"}, "vda": {"class": "ssd"}}, extra={
            "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda",
            "fs.2.mountpoint": "/var/opt/mssql/data", "fs.2.source": "/dev/sdb", "fs.2.fstype": "xfs", "fs.2.kname": "sdb",
            "fs.3.mountpoint": "/mnt/data2", "fs.3.source": "/dev/sdc", "fs.3.fstype": "xfs", "fs.3.kname": "sdc"})

    def files(self):
        return [
            MssqlFile("app", "ROWS", "/var/opt/mssql/data/app.mdf", reads_ps=100.0, read_latency_ms=30.0, writes_ps=20.0),
            MssqlFile("app2", "ROWS", "/mnt/data2/app2.ndf", reads_ps=100.0, read_latency_ms=2.0, writes_ps=10.0),
            MssqlFile("app", "LOG", "/mnt/data2/app_log.ldf", reads_ps=0.0, writes_ps=100.0, write_latency_ms=4.0),
            MssqlFile("tempdb", "ROWS", "/var/opt/mssql/data/tempdb.mdf", reads_ps=0.0, writes_ps=10.0),
        ]

    def test_read_latency_per_device_and_read_weighted_total(self):
        self.os_run(hardware=self.hardware(), disks=("sdb", "sdc"))
        self.db.mssql(files=self.files()).write()
        instance = self.only_instance(self.load())
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms", "sdb"), 30.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms", "sdc"), 2.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms"), 16.0)      # equal reads: the mean
        self.assertEqual(instance.subs("db.read_latency_ms"), ["sdb", "sdc"])
        self.assertAlmostEqual(self.series_mean(instance, "db.commit_latency_ms", "sdc"), 4.0)
        self.assertIsNone(instance.get("db.commit_latency_ms", "sdb"))
        self.assertAlmostEqual(self.series_mean(instance, "db.read_kBps", "sdb"), 100 * 8)
        self.assertAlmostEqual(self.series_mean(instance, "db.write_kBps", "sdc"), (10 + 100) * 8)
        self.assertAlmostEqual(self.series_mean(instance, "db.write_kBps"), (20 + 10 + 100 + 10) * 8)
        self.assertAlmostEqual(self.series_mean(instance, "db.temp_kBps"), 10 * 8)

    def test_without_a_local_mapping_only_the_instance_value_exists(self):
        self.os_run(hardware=self.hardware(), disks=("sdb", "sdc"))
        self.db.mssql(files=self.files(), local=False).write()
        instance = self.only_instance(self.load())
        self.assertEqual(instance.subs("db.read_latency_ms"), [])
        self.assertAlmostEqual(self.series_mean(instance, "db.read_latency_ms"), 16.0)

    def test_waits_by_category_and_latency(self):
        self.os_run()
        run = self.db.mssql()
        n = run.n

        def wait(wait_type, wait_ms_ps, tasks_ps=1.0, signal_ms_ps=0.0):
            run.waits[wait_type] = {"tasks_ps": [tasks_ps] * n, "wait_ms_ps": [wait_ms_ps] * n, "signal_ms_ps": [signal_ms_ps] * n}
        wait("PAGEIOLATCH_SH", 2000.0)
        wait("WRITELOG", 500.0, tasks_ps=100.0)
        wait("LCK_M_X", 1000.0)
        wait("SOS_SCHEDULER_YIELD", 400.0, signal_ms_ps=300.0)
        wait("CXPACKET", 100.0)
        wait("THREADPOOL", 50.0)
        wait("RESOURCE_SEMAPHORE", 200.0)
        wait("ASYNC_NETWORK_IO", 600.0)
        wait("PAGELATCH_EX", 150.0)
        wait("IO_COMPLETION", 80.0)
        run.write()
        instance = self.only_instance(self.load())
        expected = {"io_read": 2.0, "commit": 0.5, "lock": 1.0, "cpu": 0.3, "parallel": 0.1, "workers": 0.05,
                    "memory": 0.2, "network": 0.6, "latch": 0.15, "io_write": 0.08, "replica": 0.0}
        for category, sessions in expected.items():
            self.assertAlmostEqual(self.series_mean(instance, "db.wait." + category), sessions, msg=category)
        self.assertAlmostEqual(self.series_mean(instance, "db.wait.io_read", "PAGEIOLATCH_SH"), 2.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.commit_latency_ms"), 5.0)
        self.assertEqual(instance.basis["db.wait.io_read"], "time-averaged")

    def test_counters_gauges_and_counter_types(self):
        self.os_run()
        run = self.db.mssql()
        run.fill("lock_waits_ps", 0, 120, 5.0)
        run.fill("lock_wait_ms_ps", 0, 120, 250.0)
        run.fill("deadlocks_ps", 0, 120, 2.0)
        run.fill("memory_grants_pending", 0, 120, 3)
        run.fill("work_queue", 0, 120, 4)
        run.fill("runnable_tasks", 0, 120, 2)
        run.fill("busy_schedulers", 0, 120, 2)
        run.write()
        instance = self.only_instance(self.load())
        self.assertAlmostEqual(self.series_mean(instance, "db.lock_wait_avg_ms"), 50.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.deadlocks_ps"), 2.0)
        self.assertEqual(self.series_mean(instance, "db.memory_grants_pending"), 3)
        self.assertEqual(self.series_mean(instance, "db.worker_queue"), 4)
        self.assertAlmostEqual(self.series_mean(instance, "db.cpu_demand_per_cpu"), (2 + 2) / 2.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.statements_ps"), 150.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.compile_pct"), 100 * 2 / 150.0)
        self.assertAlmostEqual(self.series_mean(instance, "db.connections_pct"), 100 * 40 / 512.0)
        self.assertEqual(instance.basis["db.cache_hit_pct"], "sampled")

    def test_an_unknown_counter_type_is_read_as_a_gauge_with_a_note(self):
        self.os_run()
        run = self.db.mssql(extra_manifest={"counter_type.batch_requests_total": 1234})
        run.write()
        instance = self.only_instance(self.load())
        self.assertIsNone(instance.get("db.statements_ps"))
        self.assertTrue([note for note in instance.notes if "batch_requests_total has cntr_type 1234" in note])

    def test_wait_type_mapping(self):
        category = dbengines.mssql_wait_category
        expected = {
            "PAGEIOLATCH_SH": "io_read", "PAGEIOLATCH_EX": "io_read", "IO_COMPLETION": "io_write",
            "ASYNC_IO_COMPLETION": "io_write", "WRITE_COMPLETION": "io_write", "BACKUPIO": "io_write",
            "WRITELOG": "commit", "LOGBUFFER": "commit", "HADR_SYNC_COMMIT": "replica", "LCK_M_S": "lock",
            "LCK_M_IX": "lock", "PAGELATCH_UP": "latch", "CMEMTHREAD": "latch", "SOS_SCHEDULER_YIELD": "cpu",
            "CXPACKET": "parallel", "CXCONSUMER": "parallel", "RESOURCE_SEMAPHORE": "memory",
            "RESOURCE_SEMAPHORE_QUERY_COMPILE": "memory", "THREADPOOL": "workers", "ASYNC_NETWORK_IO": "network",
            "SOMETHING_ELSE": ""}
        for wait_type, name in expected.items():
            self.assertEqual(category(wait_type), name, wait_type)


# ---------------------------------------------------------------------------
# Rate helpers
# ---------------------------------------------------------------------------
class RateHelperTests(unittest.TestCase):
    def test_counter_rates_end_at_the_sample_time(self):
        times, rates, resets = dbengines.counter_rates([10, 20, 30], [100, 200, 500], 10)
        self.assertEqual((times, rates, resets), ([20, 30], [10.0, 30.0], []))

    def test_a_negative_difference_is_nan_and_a_reset(self):
        times, rates, resets = dbengines.counter_rates([10, 20, 30, 40], [100, 200, 50, 80], 10)
        self.assertEqual(times, [20, 30, 40])
        self.assertEqual(rates[0], 10.0)
        self.assertTrue(math.isnan(rates[1]))
        self.assertEqual(rates[2], 3.0)
        self.assertEqual(resets, [30])

    def test_a_gap_over_twice_the_interval_is_nan_but_twice_is_not(self):
        _, rates, resets = dbengines.counter_rates([0, 10, 30, 61], [0, 100, 300, 610], 10)
        self.assertEqual(rates[0], 10.0)
        self.assertEqual(rates[1], 10.0)           # 20 s = exactly twice the interval: one missed sample
        self.assertTrue(math.isnan(rates[2]))      # 31 s
        self.assertEqual(resets, [])

    def test_a_reset_inside_a_gap_is_still_a_reset(self):
        _, rates, resets = dbengines.counter_rates([0, 10, 100], [500, 600, 20], 10)
        self.assertTrue(math.isnan(rates[1]))
        self.assertEqual(resets, [100])

    def test_missing_values_give_nan_without_a_reset(self):
        _, rates, resets = dbengines.counter_rates([0, 10, 20, 30], [1, NAN, 5, 9], 10)
        self.assertTrue(math.isnan(rates[0]) and math.isnan(rates[1]))
        self.assertEqual(rates[2], 0.4)
        self.assertEqual(resets, [])

    def test_the_nominal_interval_falls_back_to_the_median_spacing(self):
        self.assertEqual(dbengines.nominal_interval([0, 5, 10, 16], 0), 5)
        self.assertEqual(dbengines.nominal_interval([0, 5, 10], 10), 10)
        self.assertEqual(dbengines.nominal_interval([0], 0), 0)

    def test_counter_deltas_are_not_divided_by_time(self):
        times, deltas, resets = dbengines.counter_deltas([0, 10, 20], [5, 8, 8], 10)
        self.assertEqual((times, deltas, resets), ([10, 20], [3, 0], []))

    def test_delta_ratio_needs_a_minimum_denominator(self):
        ratios = dbengines.delta_ratio([5.0, 5.0, 5.0, NAN, 5.0], [10.0, 9.9, 0.0, 10.0, NAN], 10.0)
        self.assertEqual(ratios[0], 0.5)
        self.assertTrue(all(math.isnan(value) for value in ratios[1:]))
        self.assertEqual(dbengines.delta_ratio([1.0], [0.5], 0.0), [2.0])
        self.assertTrue(math.isnan(dbengines.delta_ratio([1.0], [0.0], 0.0)[0]))

    def test_wait_sessions_is_seconds_of_waiting_per_second(self):
        times, sessions, resets = dbengines.wait_sessions([0, 10, 20], [0, 5000, 25000], 1000.0, 10)
        self.assertEqual((times, sessions, resets), ([10, 20], [0.5, 2.0], []))

    def test_markers_and_decreases(self):
        self.assertEqual(dbengines.marker_changes([1, 2, 3, 4], [7, 7, 8, NAN]), [3])
        self.assertEqual(dbengines.decreases([1, 2, 3], [100, 5, 10]), [2])

    def test_series_set_blanks_rates_at_resets_and_drops_empty_series(self):
        collected = dbengines.SeriesSet(10)
        times = [10, 20, 30]
        collected.add_rate("db.a", "", times, [NAN, 1.0, 2.0])
        collected.add_gauge("db.b", "", times, [4.0, 5.0, 6.0])
        collected.add_rate("db.c", "", times, [NAN, NAN, NAN])
        collected.resets.add(30)
        output = collected.finish([])
        self.assertTrue(math.isnan(output.series[("db.a", "")].values[1]))
        self.assertEqual(output.series[("db.b", "")].values, [4.0, 5.0, 6.0])
        self.assertNotIn(("db.c", ""), output.series)
        self.assertEqual(output.resets, [30])
        self.assertEqual(sorted(output.basis), ["db.a", "db.b"])


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------
class InspectTests(DbLoadCase):
    def test_inspect_lists_instances_files_devices_and_notes(self):
        hardware = hardware_for({"vda": {"class": "ssd"}}, extra={
            "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda"})
        self.os_run(hardware=hardware, pidstat=pidstat_text([(1, 3.0, 0.0, 0.0, "postgres: checkpointer")]))
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data", with_waits=False, notes="a collector note")
        run.write()
        self.db.mysql(local=False, stamp="20261016_151399").write()
        with bundle.open_bundle(self.tmp) as opened:
            out = io.StringIO()
            inspect_cmd.inspect_bundle(opened, out)
        text = out.getvalue()
        self.assertIn("  databases:\n", text)
        self.assertIn("    PostgreSQL 16.4 (localhost:5432) [pg:5432]\n", text)
        self.assertIn("      source: collector, schema 3, on this host (socket)\n", text)
        self.assertIn("      samples: 120, every 1 s\n", text)
        self.assertIn("      files: pg_stats.csv: yes, pg_database.csv: yes, pg_waits.csv: no\n", text)
        self.assertIn("      devices: data vda; wal vda\n", text)
        self.assertIn("      processes: postgres\n", text)
        self.assertIn("      NOTE: a collector note\n", text)
        self.assertIn("    MySQL 8.0.36 (localhost:3306) [my:3306]\n", text)
        self.assertIn("      source: collector, schema 3, remote (none)\n", text)
        self.assertRegex(text, r"db01_postgresql_\S+\s+postgresql\s+interval 1s, 120 samples")

    def test_inspect_process_only_instance(self):
        self.os_run(pidstat=pidstat_text([(1, 3.0, 0.0, 0.0, "/usr/sbin/mysqld --datadir=/var/lib/mysql")]))
        with bundle.open_bundle(self.tmp) as opened:
            out = io.StringIO()
            inspect_cmd.inspect_bundle(opened, out)
        self.assertIn("    MySQL (process data only) [my:proc]\n", out.getvalue())
        self.assertIn("      source: process data only, on this host (process)\n", out.getvalue())


if __name__ == "__main__":
    unittest.main()
