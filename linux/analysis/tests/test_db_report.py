"""Data for the Databases section of the reports (spec 12.2, 12.5) and the header and JSON helpers.

Scenarios come from synth_db.py (database runs) and synth.py (the operating-system run).
Chart-dependent assertions are skipped without matplotlib; the rest of report_db needs none.
"""

import datetime
import json
import os
import shutil
import statistics
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    __import__("matplotlib")
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

from perfdiag import analysis, bundle, charts, loader, report_db  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from helpers import write_text  # noqa: E402
from synth import BASE_EPOCH, Synth, hardware_for  # noqa: E402
from synth_db import DbSynth, pidstat_text  # noqa: E402

V2_LINE = ("PostgreSQL pg:5432: collected with collect-postgresql-metrics.sh 2.0.0. Database counters cover the "
           "postgres database only; waits, IO timing and file placement are not available. Re-run with 3.0.0.")


def inventory():
    """A system disk, an NVMe disk behind a device-mapper volume for /var/lib/pgsql, and the dm name in the disk data."""
    return hardware_for({
        "vda": {"class": "ssd"}, "sdb": {"class": "nvme"},
        "dm-0": {"class": "stacked", "slaves": ["sdb1"], "display": "vg-data"}}, extra={
        "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda1", "fs.1.fstype": "xfs", "fs.1.kname": "vda1",
        "fs.2.mountpoint": "/var/lib/pgsql", "fs.2.source": "/dev/mapper/vg-data", "fs.2.fstype": "xfs",
        "fs.2.kname": "dm-0", "part.sdb1.parent": "sdb", "part.vda1.parent": "vda"})


class DbReportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbreport-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = DbSynth(self.tmp)

    def os_run(self, hardware=None, disks=(), pidstat=None, **kwargs):
        synth = Synth(self.tmp, **kwargs)
        for name in disks:
            synth.add_disk(name)
        synth.hardware = hardware
        run_directory = synth.write()
        if pidstat is not None:
            write_text(os.path.join(run_directory, "pidstat_20261016.log"), pidstat_text(pidstat, last_sample=120))
        return synth

    def analyse(self):
        """What report_db reads of an analysis result: the loaded host. Keeps these tests on the data layer."""
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01")
        return types.SimpleNamespace(host=host)

    def analyse_fully(self):
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01")
        return analysis.run_analysis(host, Settings())

    def instance(self, result, short):
        return next(item for item in result.host.databases if item.short == short)

    def section(self, short, catalogue=None):
        result = self.analyse()
        return report_db.instance_section(result, self.instance(result, short), catalogue)

    def fact(self, section, label):
        return dict(section["facts"]).get(label)

    def reset_lines(self, short, **kwargs):
        return [line for line in self.coverage(short, **kwargs) if "statistics reset" in line]

    def coverage(self, short, **kwargs):
        result = self.analyse()
        return report_db.coverage_lines(self.instance(result, short), result.host.zone, **kwargs)


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------
class FactTests(DbReportCase):
    def test_postgresql_facts(self):
        self.os_run()
        self.db.postgresql().write()
        section = self.section("pg:5432")
        facts = dict(section["facts"])
        self.assertEqual(facts["Engine and version"], "PostgreSQL 16.4")
        self.assertEqual(facts["Source"], "collector run")
        self.assertEqual(facts["Location"], "on this host (socket)")
        self.assertEqual(facts["Collector"], "collect-postgresql-metrics.sh 3.0.0, schema 3")
        self.assertEqual(facts["Samples"], "120 samples, every 1 s")
        self.assertEqual(facts["Window covered"], "2026-10-16 15:13:21 to 2026-10-16 15:15:20 UTC (2 min)")
        self.assertEqual(facts["Server start"], "2026-10-15 08:00:00+00")
        self.assertEqual(facts["Resets in window"], "none")
        self.assertEqual(section["label"], "PostgreSQL 16.4 (localhost:5432)")
        self.assertEqual(section["short"], "pg:5432")

    def test_each_engine_names_its_collector_and_version(self):
        self.os_run()
        self.db.mysql().write()
        self.db.mysql(instance="localhost:3307", mariadb=True, version="10.11.6", stamp="20261016_151321").write()
        self.db.oracle().write()
        self.db.mssql().write()
        result = self.analyse()
        by_short = {item.short: report_db.instance_section(result, item) for item in result.host.databases}
        self.assertEqual(self.fact(by_short["my:3306"], "Engine and version"), "MySQL 8.0.36")
        self.assertEqual(self.fact(by_short["my:3307"], "Engine and version"), "MariaDB 10.11.6")
        self.assertEqual(self.fact(by_short["my:3306"], "Collector"), "collect-mysql-metrics.sh 3.0.0, schema 3")
        self.assertEqual(self.fact(by_short["ora:ORCL/PDB1"], "Collector"), "collect-oracle-metrics.sh 3.0.0, schema 3")
        self.assertEqual(self.fact(by_short["mssql:MSSQLSERVER"], "Collector"), "collect-mssql-metrics.sh 3.0.0, schema 3")
        self.assertEqual(self.fact(by_short["mssql:MSSQLSERVER"], "Engine and version"), "SQL Server 16.0.4125.3")
        for section in by_short.values():
            self.assertEqual(self.fact(section, "Server start"), "2026-10-15 08:00:00+00")

    def test_a_remote_instance_says_so_and_its_basis(self):
        self.os_run()
        self.db.postgresql(local=False).write()
        self.assertEqual(self.fact(self.section("pg:5432"), "Location"), "remote")

    def test_a_loopback_basis_is_shown(self):
        self.os_run()
        self.db.postgresql(local_basis="loopback").write()
        self.assertEqual(self.fact(self.section("pg:5432"), "Location"), "on this host (loopback)")

    def test_resets_in_the_window_are_counted_with_their_times(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [70]
        pg.write()
        # Sample 70 is taken at BASE_EPOCH + 71 s = 15:14:31.
        self.assertEqual(self.fact(self.section("pg:5432"), "Resets in window"), "1 (at 15:14)")

    def test_a_2_0_0_run_has_the_2_0_0_collector_and_no_server_start(self):
        self.os_run()
        self.db.postgresql_v2().write()
        section = self.section("pg:5432")
        self.assertEqual(self.fact(section, "Collector"), "collect-postgresql-metrics.sh 2.0.0, schema 2")
        self.assertIsNone(self.fact(section, "Server start"))

    def test_a_process_only_instance_has_no_collector_samples_or_window(self):
        self.os_run(pidstat=[(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")])
        section = self.section("my:proc")
        facts = dict(section["facts"])
        self.assertEqual(facts["Source"], "process data only (no collector run)")
        self.assertEqual(facts["Processes"], "mysqld; seen in 100 % of pidstat samples")
        for label in ("Collector", "Samples", "Window covered", "Server start", "Resets in window"):
            self.assertNotIn(label, facts)

    def test_the_window_is_the_span_of_the_instances_own_series(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.skip_ranges = [(0, 29)]                      # the run starts 30 s into the window
        pg.write()
        facts = dict(self.section("pg:5432")["facts"])
        self.assertTrue(facts["Window covered"].startswith("2026-10-16 15:13:51 to 2026-10-16 15:15:20 UTC"))


# ---------------------------------------------------------------------------
# File placement
# ---------------------------------------------------------------------------
class PlacementTests(DbReportCase):
    def test_paths_are_placed_through_the_inventory_with_chain_and_media_class(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data", wal_directory="/var/lib/wal").write()
        section = self.section("pg:5432")
        self.assertEqual(section["placement_headers"], ("Role", "Path", "Device", "Media class"))
        self.assertEqual(section["placement_rows"], [
            ("data", "/var/lib/pgsql/16/data", "vg-data (dm-0) on sdb1", "nvme"),
            ("wal", "/var/lib/wal", "vda", "ssd"),
        ])
        self.assertEqual(section["placement_note"], "")

    def test_a_path_with_no_matching_file_system_is_listed_as_not_placed(self):
        self.os_run(hardware=hardware_for({"vda": {"class": "ssd"}}))
        self.db.postgresql(data_directory="/srv/pg/data").write()
        section = self.section("pg:5432")
        self.assertEqual(section["placement_rows"][0], ("data", "/srv/pg/data", "not placed", "-"))

    def test_without_an_inventory_the_paths_are_listed_with_the_reason(self):
        self.os_run()
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        section = self.section("pg:5432")
        self.assertEqual([row[2] for row in section["placement_rows"]], ["not placed", "not placed"])
        self.assertEqual(section["placement_note"], "no hardware inventory: files could not be placed on devices")

    def test_a_remote_instance_is_not_placed(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data", local=False).write()
        section = self.section("pg:5432")
        self.assertEqual(section["placement_rows"], [])
        self.assertIn("remote server", section["placement_note"])

    def test_an_instance_without_paths_says_so(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        self.db.postgresql().write()
        section = self.section("pg:5432")
        self.assertEqual(section["placement_rows"], [])
        self.assertEqual(section["placement_note"], "no database file locations were recorded")

    def test_roles_are_listed_in_the_order_of_the_spec_and_asm_groups_are_not_placed(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        run = self.db.oracle()
        run.paths = {"temp": ["/var/lib/pgsql/temp"], "redo": ["/var/lib/pgsql/redo"], "data": ["+DATA"],
                     "asm_disk": ["/dev/sdb1"]}
        run.write()
        rows = self.section("ora:ORCL/PDB1")["placement_rows"]
        self.assertEqual([row[0] for row in rows], ["data", "redo", "temp", "asm_disk"])
        self.assertEqual(rows[0], ("data", "+DATA", "not placed", "-"))
        self.assertEqual(rows[1][2], "vg-data (dm-0) on sdb1")
        self.assertEqual(rows[3][2:], ("sdb", "nvme"))

    def test_a_process_only_instance_is_placed_from_its_data_directory(self):
        hardware = inventory()
        hardware.update({"dbproc.1.engine": "postgresql", "dbproc.1.cwd": "/var/lib/pgsql/16/data"})
        self.os_run(hardware=hardware, disks=("sdb", "vg-data"))
        rows = self.section("pg:proc")["placement_rows"]
        self.assertEqual(rows, [("data", "/var/lib/pgsql/16/data", "vg-data (dm-0) on sdb1", "nvme")])

    def test_a_stack_on_two_media_lists_both_classes(self):
        hardware = hardware_for({
            "sda": {"class": "hdd"}, "sdc": {"class": "ssd"},
            "md0": {"class": "stacked", "slaves": ["sda", "sdc"]}}, extra={
            "fs.1.mountpoint": "/var/lib/pgsql", "fs.1.source": "/dev/md0", "fs.1.fstype": "xfs", "fs.1.kname": "md0"})
        self.os_run(hardware=hardware, disks=("md0",))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        row = self.section("pg:5432")["placement_rows"][0]
        self.assertEqual(row[2], "md0 on sda, sdc")
        self.assertEqual(row[3], "hdd, ssd")


# ---------------------------------------------------------------------------
# Activity summary and settings
# ---------------------------------------------------------------------------
class ActivityTests(DbReportCase):
    def rows_by_metric(self, section):
        return {row["metric"]: row for row in section["activity"]}

    def test_postgresql_medians_and_p95_match_the_synthetic_data(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.fill("read_latency_ms", 30, 100, 40.0)        # 70 of 120 samples
        pg.waits[("Lock", "relation")] = [0] * 20 + [4] * 100
        pg.fill("active", 0, 120, 2)
        pg.write()
        section = self.section("pg:5432")
        rows = self.rows_by_metric(section)
        self.assertAlmostEqual(rows["db.transactions_ps"]["median"], 51.0, places=2)      # 50 commits + 1 rollback
        self.assertAlmostEqual(rows["db.transactions_ps"]["p95"], 51.0, places=2)
        self.assertEqual(rows["db.sessions_active"]["median"], 2.0)
        self.assertEqual(rows["db.wait.lock"]["median"], 4.0)
        self.assertEqual(rows["db.wait.lock"]["p95"], 4.0)
        self.assertEqual(rows["db.wait.cpu"]["median"], 1.0)
        self.assertAlmostEqual(rows["db.read_latency_ms"]["median"], 40.0, places=2)
        self.assertAlmostEqual(rows["db.read_latency_ms"]["p95"], 40.0, places=2)
        self.assertAlmostEqual(rows["db.cache_hit_pct"]["median"], 100.0 * 2000 / 2020, places=2)
        # PostgreSQL has no statements counter and no commit latency (waits carry commit).
        self.assertNotIn("db.statements_ps", rows)
        self.assertNotIn("db.commit_latency_ms", rows)

    def test_the_summary_is_the_median_and_95th_percentile_of_the_series_itself(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.fill("commits_ps", 0, 100, 10.0)
        pg.fill("commits_ps", 100, 120, 200.0)           # a spike in the last 20 samples
        pg.write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        values = instance.get("db.transactions_ps").finite_values()
        row = self.rows_by_metric(report_db.instance_section(result, instance))["db.transactions_ps"]
        self.assertEqual(row["median"], statistics.median(values))
        self.assertGreater(row["p95"], 100.0)
        self.assertLess(row["median"], 20.0)

    def test_basis_comes_from_the_instance(self):
        self.os_run()
        self.db.postgresql().write()
        rows = self.rows_by_metric(self.section("pg:5432"))
        self.assertEqual(rows["db.wait.cpu"]["basis"], "sampled")
        self.assertEqual(rows["db.sessions_active"]["basis"], "sampled")
        self.assertEqual(rows["db.transactions_ps"]["basis"], "time-averaged")

    def test_each_engine_reports_its_own_rates_and_latencies(self):
        self.os_run()
        self.db.mysql().write()
        self.db.oracle().write()
        self.db.mssql().write()
        result = self.analyse()
        sections = {item.short: report_db.instance_section(result, item) for item in result.host.databases}
        my = self.rows_by_metric(sections["my:3306"])
        self.assertAlmostEqual(my["db.statements_ps"]["median"], 200.0, places=2)
        self.assertAlmostEqual(my["db.transactions_ps"]["median"], 41.0, places=2)
        self.assertAlmostEqual(my["db.read_latency_ms"]["median"], 0.4, places=2)
        self.assertAlmostEqual(my["db.commit_latency_ms"]["median"], 0.3, places=2)
        self.assertEqual(my["db.sessions_active"]["median"], 2.0)              # Threads_running 3 minus the collector's own
        ora = self.rows_by_metric(sections["ora:ORCL/PDB1"])
        self.assertAlmostEqual(ora["db.statements_ps"]["median"], 300.0, places=2)
        self.assertAlmostEqual(ora["db.read_latency_ms"]["median"], 1.0, places=2)
        self.assertAlmostEqual(ora["db.commit_latency_ms"]["median"], 1.0, places=2)
        ms = self.rows_by_metric(sections["mssql:MSSQLSERVER"])
        self.assertAlmostEqual(ms["db.statements_ps"]["median"], 150.0, places=2)
        self.assertAlmostEqual(ms["db.cache_hit_pct"]["median"], 99.5, places=2)
        self.assertEqual(ms["db.cache_hit_pct"]["basis"], "sampled")

    def test_text_rows_follow_the_headers_and_the_report_formats(self):
        self.os_run()
        self.db.postgresql().write()
        section = self.section("pg:5432")
        self.assertEqual(section["activity_headers"], ("Metric", "Basis", "Median", "p95"))
        rows = {row[0]: row for row in section["activity_rows"]}
        self.assertEqual(rows["Transactions per second"], ("Transactions per second", "time-averaged", "51.0 /s", "51.0 /s"))
        self.assertEqual(rows["Active sessions"][1:], ("sampled", "2.00 sessions", "2.00 sessions"))
        self.assertEqual(rows["Cache hit"][2], "99.0 %")
        self.assertEqual([row[0] for row in section["activity_rows"]][:2], ["Transactions per second", "Active sessions"])

    def test_wait_categories_follow_the_spec_order_after_the_rates(self):
        self.os_run()
        self.db.postgresql().write()
        metrics = [row["metric"] for row in self.section("pg:5432")["activity"]]
        waits = [metric for metric in metrics if metric.startswith("db.wait.")]
        self.assertEqual(waits[0], "db.wait.cpu")
        self.assertLess(metrics.index("db.sessions_active"), metrics.index("db.wait.cpu"))
        self.assertGreater(metrics.index("db.cache_hit_pct"), metrics.index(waits[-1]))

    def test_a_process_only_instance_has_an_empty_summary(self):
        self.os_run(pidstat=[(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")])
        section = self.section("my:proc")
        self.assertEqual(section["activity"], [])
        self.assertEqual(section["activity_rows"], [])

    def test_recorded_settings_are_the_instance_settings_sorted(self):
        self.os_run()
        self.db.postgresql(settings={"shared_buffers": "128MB"}).write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        rows = report_db.instance_section(result, instance)["settings"]
        self.assertEqual(rows, sorted(instance.settings.items()))
        self.assertIn(("shared_buffers", "128MB"), rows)
        self.assertIn(("track_io_timing", "on"), rows)


# ---------------------------------------------------------------------------
# Coverage lines (spec 12.5), each condition on and off
# ---------------------------------------------------------------------------
class CoverageTests(DbReportCase):
    def test_a_clean_instance_has_no_coverage_lines(self):
        self.os_run()
        self.db.mysql().write()
        self.assertEqual(self.coverage("my:3306"), [])

    def test_postgresql_track_io_timing_off(self):
        self.os_run()
        self.db.postgresql(with_io_timing=False).write()
        self.assertIn("PostgreSQL pg:5432: track_io_timing is off, so read latency was not measured.",
                      self.coverage("pg:5432"))

    def test_postgresql_track_io_timing_on(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertFalse([line for line in self.coverage("pg:5432") if "track_io_timing" in line])

    def test_sampled_waits_say_how_often(self):
        self.db = DbSynth(self.tmp, interval=15)
        self.os_run(interval=15)
        self.db.postgresql().write()
        self.assertIn("PostgreSQL pg:5432: wait events are sampled every 15 s, not averaged; short waits between "
                      "samples are not visible.", self.coverage("pg:5432"))

    def test_averaged_waits_and_a_missing_waits_file_have_no_sampling_line(self):
        self.os_run()
        self.db.mysql().write()
        self.db.postgresql(with_waits=False).write()
        self.assertFalse([line for line in self.coverage("my:3306") if "sampled" in line])
        self.assertFalse([line for line in self.coverage("pg:5432") if "sampled" in line])

    def test_mysql_performance_schema_off(self):
        self.os_run()
        self.db.mysql(with_file_io=False).write()
        self.assertEqual(self.coverage("my:3306"),
                         ["MySQL my:3306: performance_schema is off, so InnoDB file IO latency was not measured."])

    def test_mariadb_names_itself(self):
        self.os_run()
        self.db.mysql(mariadb=True, with_file_io=False).write()
        self.assertEqual(self.coverage("my:3306"),
                         ["MariaDB my:3306: performance_schema is off, so InnoDB file IO latency was not measured."])

    def test_mysql_performance_schema_on(self):
        self.os_run()
        self.db.mysql().write()
        self.assertFalse([line for line in self.coverage("my:3306") if "performance_schema" in line])

    def test_the_2_0_0_collector_line_is_exact(self):
        self.os_run()
        self.db.postgresql_v2().write()
        lines = self.coverage("pg:5432")
        self.assertIn(V2_LINE, lines)
        # None of the schema 3 lines apply to a run that recorded no settings.
        self.assertFalse([line for line in lines if "track_io_timing" in line])

    def test_a_current_collector_has_no_2_0_0_line(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertFalse([line for line in self.coverage("pg:5432") if "collected with" in line])

    def test_the_2_0_0_line_is_the_one_phase_4_noted_and_the_section_notes_do_not_repeat_it(self):
        self.os_run()
        self.db.postgresql_v2().write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        self.assertIn(V2_LINE, instance.notes)
        self.assertEqual(report_db.section_notes(instance), [])
        self.assertEqual(report_db.instance_section(result, instance)["notes"], [])

    def test_an_oracle_pdb_connection(self):
        self.os_run()
        self.db.oracle().write()
        self.assertEqual(self.coverage("ora:ORCL/PDB1"), [
            "Oracle ORCL/PDB1: connected to a PDB; instance-wide figures need a CDB$ROOT connection."])

    def test_an_oracle_cdb_root_connection_and_an_unrecorded_container_have_no_pdb_line(self):
        self.os_run()
        run = self.db.oracle(instance="ORCL/CDB$ROOT", settings={"con_name": "CDB$ROOT"})
        run.write()
        self.assertEqual(self.coverage("ora:ORCL/CDB$ROOT"), [])
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        self.os_run()
        run = self.db.oracle()
        run.settings.pop("con_name")
        run.write()
        self.assertEqual(self.coverage("ora:ORCL/PDB1"), [])

    def test_sql_server_container_paths(self):
        self.os_run()
        self.db.mssql(extra_manifest={"path_namespace": "container"}).write()
        self.assertEqual(self.coverage("mssql:MSSQLSERVER"), [
            "SQL Server MSSQLSERVER: file paths are inside the container and were mapped to host devices through the "
            "mount table only when the paths matched."])

    def test_sql_server_outside_a_container_has_no_container_line(self):
        self.os_run()
        self.db.mssql().write()
        self.assertEqual(self.coverage("mssql:MSSQLSERVER"), [])

    def test_process_only_lines_per_engine(self):
        self.os_run(pidstat=[(1, 1.0, 0.0, 0.0, "mysqld --datadir=/var/lib/mysql"), (2, 1.0, 0.0, 0.0, "sqlservr"),
                             (3, 1.0, 0.0, 0.0, "postgres: checkpointer"), (4, 1.0, 0.0, 0.0, "ora_dbw0_ORCL")])
        self.assertEqual(self.coverage("my:proc"),
                         ["mysqld: process data only. Database-level findings need collect-mysql-metrics.sh."])
        self.assertEqual(self.coverage("mssql:proc"),
                         ["sqlservr: process data only. Database-level findings need collect-mssql-metrics.sh."])
        self.assertEqual(self.coverage("pg:proc"),
                         ["postgres: process data only. Database-level findings need collect-postgresql-metrics.sh."])
        self.assertEqual(self.coverage("ora:ORCL"),
                         ["Oracle ORCL: process data only. Database-level findings need collect-oracle-metrics.sh."])

    def test_mariadb_process_is_mariadbd_with_the_mysql_collector(self):
        self.os_run(pidstat=[(1, 1.0, 0.0, 0.0, "mariadbd --datadir=/var/lib/mysql")])
        self.assertEqual(self.coverage("my:proc"),
                         ["mariadbd: process data only. Database-level findings need collect-mysql-metrics.sh."])

    def test_a_collector_instance_with_process_data_has_no_process_only_line(self):
        self.os_run(pidstat=[(1, 1.0, 0.0, 0.0, "mysqld --datadir=/var/lib/mysql")])
        self.db.mysql().write()
        self.assertEqual(self.coverage("my:3306"), [])

    def test_one_reset(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [70]
        pg.write()
        self.assertEqual(self.reset_lines("pg:5432"), [
            "PostgreSQL pg:5432: statistics reset or server restart at 15:14; rates are blank for that sample."])

    def test_the_reset_time_is_shown_in_the_report_zone(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [70]
        pg.write()
        result = self.analyse()
        zone = datetime.timezone(datetime.timedelta(hours=2))
        lines = report_db.coverage_lines(self.instance(result, "pg:5432"), zone)
        self.assertTrue([line for line in lines if "restart at 17:14;" in line], lines)

    def test_several_resets_are_listed_and_counted(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [20, 40, 60]
        pg.write()
        self.assertEqual(self.reset_lines("pg:5432"), [
            "PostgreSQL pg:5432: statistics reset or server restart at 15:13, 15:14 and 15:14; "
            "rates are blank for those samples."])

    def test_more_than_five_resets_are_summarised(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [10, 20, 30, 40, 50, 60, 70]
        pg.write()
        self.assertIn("and 2 more; rates are blank for those samples.", self.reset_lines("pg:5432")[0])

    def test_reset_times_carry_dates_when_the_window_spans_days(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [70]
        pg.write()
        line = self.reset_lines("pg:5432", window=(BASE_EPOCH, BASE_EPOCH + 3 * 86400))[0]
        self.assertIn("restart at 2026-10-16 15:14;", line)

    def test_no_resets_no_reset_line(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertFalse([line for line in self.coverage("pg:5432") if "reset" in line])

    def test_several_conditions_give_several_lines_in_spec_order(self):
        self.os_run()
        pg = self.db.postgresql(with_io_timing=False)
        pg.reset_counters_at = [70]
        pg.write()
        lines = self.coverage("pg:5432")
        self.assertEqual(len(lines), 3)
        self.assertIn("track_io_timing", lines[0])
        self.assertIn("sampled every 1 s", lines[1])
        self.assertIn("statistics reset", lines[2])

    def test_coverage_is_part_of_the_section(self):
        self.os_run()
        self.db.postgresql(with_io_timing=False).write()
        self.assertEqual(len(self.section("pg:5432")["coverage"]), 2)


# ---------------------------------------------------------------------------
# Header line, header fact, JSON, whole sections
# ---------------------------------------------------------------------------
class HeaderAndJsonTests(DbReportCase):
    def test_header_line_of_a_local_collector_instance(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data", wal_directory="/var/lib/wal").write()
        result = self.analyse()
        line = report_db.header_line(result.host, self.instance(result, "pg:5432"))
        self.assertEqual(line, "Database: PostgreSQL 16.4 (pg:5432), collector, local, data on vg-data/sdb, WAL on vda")

    def test_header_line_of_remote_and_unknown_locality(self):
        self.os_run()
        self.db.postgresql(local=False).write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        self.assertEqual(report_db.header_line(result.host, instance),
                         "Database: PostgreSQL 16.4 (pg:5432), collector, remote")
        instance.local = None
        self.assertEqual(report_db.header_line(result.host, instance),
                         "Database: PostgreSQL 16.4 (pg:5432), collector, location unknown")

    def test_header_line_of_a_process_only_instance(self):
        hardware = inventory()
        hardware.update({"dbproc.1.engine": "mysql", "dbproc.1.cwd": "/var/lib/pgsql/mysql"})
        self.os_run(hardware=hardware, disks=("sdb", "vg-data"), pidstat=[(1, 1.0, 0.0, 0.0, "mysqld")])
        result = self.analyse()
        line = report_db.header_line(result.host, self.instance(result, "my:proc"))
        self.assertEqual(line, "Database: mysqld (process data only), seen in 100 % of pidstat samples, "
                               "data on vg-data/sdb")

    def test_header_fact_lists_the_labels_and_is_none_without_instances(self):
        self.os_run()
        result = self.analyse()
        self.assertIsNone(report_db.databases_header_fact(result.host))
        self.assertEqual(report_db.database_sections(result), [])
        self.db.postgresql().write()
        self.db.mysql().write()
        result = self.analyse()
        self.assertEqual(report_db.databases_header_fact(result.host),
                         ("Databases", "PostgreSQL 16.4 (localhost:5432); MySQL 8.0.36 (localhost:3306)"))

    def test_the_findings_json_entry_has_the_spec_keys_and_serialises(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"))
        pg = self.db.postgresql(data_directory="/var/lib/pgsql/16/data", wal_directory="/var/lib/wal")
        pg.reset_counters_at = [70]
        pg.write()
        result = self.analyse()
        entry = report_db.database_json(self.instance(result, "pg:5432"))
        self.assertEqual(sorted(entry), sorted([
            "id", "short", "engine", "label", "source", "local", "version_num", "collector_schema", "devices",
            "resets", "settings", "notes"]))
        self.assertEqual(entry["id"], "postgresql:localhost:5432")
        self.assertEqual(entry["short"], "pg:5432")
        self.assertIs(entry["local"], True)
        self.assertEqual(entry["version_num"], 160004)
        self.assertEqual(entry["collector_schema"], 3)
        self.assertEqual(entry["devices"], {"data": ["vg-data", "sdb"], "wal": ["vda"]})
        self.assertEqual(entry["resets"], [BASE_EPOCH + 71])
        self.assertEqual(entry["settings"]["track_io_timing"], "on")
        json.dumps(entry)

    def test_a_section_is_plain_data(self):
        self.os_run(hardware=inventory(), disks=("sdb", "vg-data"), pidstat=[(1, 1.0, 0.0, 0.0, "mysqld")])
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        self.db.oracle().write()
        result = self.analyse()
        sections = report_db.database_sections(result)
        self.assertEqual([section["short"] for section in sections], [item.short for item in result.host.databases])
        for section in sections:
            json.dumps(section)
            self.assertEqual(section["charts"], [])               # no catalogue given

    @unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
    def test_the_sections_chart_names_come_from_the_catalogue(self):
        self.os_run()
        self.db.postgresql().write()
        self.db.postgresql(instance="localhost:5433", stamp="20261016_151321", with_waits=False).write()
        result = self.analyse_fully()
        catalogue = charts.chart_catalogue(result)
        first, second = report_db.database_sections(result, catalogue)
        self.assertEqual(first["charts"], ["db_activity_pg_5432", "db_waits_pg_5432", "db_latency_pg_5432",
                                           "db_cache_pg_5432"])
        self.assertNotIn("db_waits_pg_5433", second["charts"])
        self.assertTrue(all(name.endswith("pg_5433") for name in second["charts"]))

    def test_notes_other_than_the_2_0_0_line_stay_in_the_section(self):
        self.os_run()
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()          # no inventory: a placement note
        section = self.section("pg:5432")
        self.assertEqual(section["notes"], ["no hardware inventory: PostgreSQL files could not be placed on devices"])


if __name__ == "__main__":
    unittest.main()
