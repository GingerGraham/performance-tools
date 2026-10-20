"""Database charts (spec 12.3): which exist, what they say, and that they draw.

Scenarios come from synth_db.py (database runs) and synth.py (the operating-system run). Charts
tests are skipped without matplotlib, like the other chart tests.
"""

import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    __import__("matplotlib")
    HAVE_MPL = True
except ImportError:
    HAVE_MPL = False

from perfdiag import analysis, bundle, charts, loader  # noqa: E402
from perfdiag.model import Episode, Series  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from helpers import write_text  # noqa: E402
from synth import Synth, hardware_for  # noqa: E402
from synth_db import PIDSTAT_HEADER, DbSynth, pidstat_text  # noqa: E402

if HAVE_MPL:
    from perfdiag import dbcharts  # noqa: E402

POSTGRES_PROCESSES = [
    (100, 1.0, 10.0, 90.0, "postgres: checkpointer process"),
    (101, 0.5, 0.0, 20.0, "postgres: 16/main: background writer"),
    (102, 30.0, 400.0, 0.0, "postgres: app mydb 10.0.0.5(50122) SELECT"),
    (99, 0.0, 0.0, 0.0, "/usr/pgsql-16/bin/postmaster -D /var/lib/pgsql/16/data"),
]


def inventory():
    """An NVMe disk holding /var/lib/pgsql and a system disk."""
    return hardware_for({"vda": {"class": "ssd"}, "nvme1n1": {"class": "nvme"}}, extra={
        "fs.1.mountpoint": "/", "fs.1.source": "/dev/vda", "fs.1.fstype": "xfs", "fs.1.kname": "vda",
        "fs.2.mountpoint": "/var/lib/pgsql", "fs.2.source": "/dev/nvme1n1", "fs.2.fstype": "xfs",
        "fs.2.kname": "nvme1n1"})


def chart_names(catalogue):
    return [chart.name for chart in catalogue]


def db_chart_names(catalogue):
    return [name for name in chart_names(catalogue) if name.startswith("db_")]


class ChartText(str):
    """SVG text whose repr is short, so a failed assertIn does not print the whole chart."""

    def __repr__(self):
        return "<chart svg, %d characters>" % len(self)


class StubResult:
    """Just what dbcharts.latency_thresholds reads: no indicators, no profiles."""

    def __init__(self, host=None, settings=None):
        self.host = host
        self.settings = settings or Settings()

    def indicator(self, rule, device=""):
        return None

    def profile_for(self, device):
        return None


@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class DbChartCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbcharts-")
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

    def analyse(self, extra_series=()):
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01")
        for series in extra_series:
            host.add_series(series)
        return analysis.run_analysis(host, Settings())

    def instance(self, result, short):
        return next(item for item in result.host.databases if item.short == short)

    def catalogue_names(self, result):
        return db_chart_names(charts.chart_catalogue(result))

    def svg_of(self, result, name):
        chart = next(item for item in charts.chart_catalogue(result) if item.name == name)
        return ChartText(chart.render("svg"))

    def psar_await(self, result, device, metric, value):
        """A psar-style await series for a device that has sar data."""
        base = result.host.get("disk.await_ms", device)
        return Series(metric, device, list(base.times), [value] * len(base.times), base.interval_s, "psar")


# ---------------------------------------------------------------------------
# Which charts exist
# ---------------------------------------------------------------------------
class CatalogueTests(DbChartCase):
    def test_a_host_without_databases_gets_no_database_charts(self):
        self.os_run()
        result = self.analyse()
        self.assertEqual(result.host.databases, [])
        self.assertEqual(self.catalogue_names(result), [])
        self.assertEqual(dbcharts.database_charts(result), [])

    def test_the_existing_charts_are_the_same_with_and_without_a_database(self):
        self.os_run()
        without = chart_names(charts.chart_catalogue(self.analyse()))
        self.db.postgresql().write()
        result = self.analyse()
        with_database = chart_names(charts.chart_catalogue(result))
        self.assertEqual([name for name in with_database if not name.startswith("db_")], without)
        self.assertTrue(db_chart_names(charts.chart_catalogue(result)))
        # Database charts come after every host chart.
        first_database = next(index for index, name in enumerate(with_database) if name.startswith("db_"))
        self.assertTrue(all(name.startswith("db_") for name in with_database[first_database:]))

    def test_postgresql_with_everything_has_five_charts(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",), pidstat=POSTGRES_PROCESSES)
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        result = self.analyse()
        self.assertEqual(self.catalogue_names(result), [
            "db_activity_pg_5432", "db_waits_pg_5432", "db_latency_pg_5432", "db_cache_pg_5432",
            "db_processes_pg_5432"])

    def test_postgresql_without_waits_or_io_timing_has_no_waits_or_latency_chart(self):
        self.os_run()
        self.db.postgresql(with_waits=False, with_io_timing=False).write()
        names = self.catalogue_names(self.analyse())
        self.assertEqual(names, ["db_activity_pg_5432", "db_cache_pg_5432"])

    def test_postgresql_without_io_timing_keeps_its_waits_but_has_no_latency_chart(self):
        self.os_run()
        self.db.postgresql(with_io_timing=False).write()
        names = self.catalogue_names(self.analyse())
        self.assertIn("db_waits_pg_5432", names)
        self.assertNotIn("db_latency_pg_5432", names)

    def test_a_process_chart_needs_pidstat_rows_of_the_instance(self):
        self.os_run()
        self.db.postgresql().write()
        self.assertNotIn("db_processes_pg_5432", self.catalogue_names(self.analyse()))

    def test_mysql_has_five_charts_with_file_io_and_no_latency_chart_without(self):
        self.os_run(pidstat=[(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")])
        self.db.mysql().write()
        self.assertEqual(self.catalogue_names(self.analyse()), [
            "db_activity_my_3306", "db_waits_my_3306", "db_latency_my_3306", "db_cache_my_3306",
            "db_processes_my_3306"])
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        self.os_run(pidstat=[(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")])
        self.db.mysql(with_file_io=False).write()
        names = self.catalogue_names(self.analyse())
        self.assertNotIn("db_latency_my_3306", names)
        self.assertIn("db_waits_my_3306", names)       # row lock time is a wait category even without file events

    def test_oracle_and_sql_server_charts_use_their_short_names(self):
        self.os_run(pidstat=[(3, 3.0, 4.0, 5.0, "ora_dbw0_ORCL"), (2, 3.0, 4.0, 5.0, "sqlservr")])
        self.db.oracle().write()
        self.db.mssql().write()
        names = self.catalogue_names(self.analyse())
        for kind in dbcharts.KINDS:
            self.assertIn("db_%s_ora_ORCL_PDB1" % kind, names)
            self.assertIn("db_%s_mssql_MSSQLSERVER" % kind, names)

    def test_a_process_only_instance_has_only_the_process_chart(self):
        self.os_run(pidstat=POSTGRES_PROCESSES)
        self.assertEqual(self.catalogue_names(self.analyse()), ["db_processes_pg_proc"])

    def test_process_only_without_pidstat_rows_has_no_chart(self):
        hardware = hardware_for({"vda": {"class": "ssd"}}, extra={
            "dbproc.1.engine": "postgresql", "dbproc.1.cwd": "/var/lib/pgsql/16/data"})
        self.os_run(hardware=hardware)
        result = self.analyse()
        self.assertEqual(len(result.host.databases), 1)
        self.assertEqual(self.catalogue_names(result), [])

    def test_two_instances_each_get_their_own_charts(self):
        self.os_run()
        self.db.postgresql().write()
        self.db.postgresql(instance="localhost:5433", stamp="20261016_151321").write()
        names = self.catalogue_names(self.analyse())
        self.assertEqual(sum(1 for name in names if name.startswith("db_activity_")), 2)
        self.assertIn("db_waits_pg_5433", names)

    def test_every_chart_renders_as_svg_and_png(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",), pidstat=POSTGRES_PROCESSES + [
            (1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql"), (2, 3.0, 4.0, 5.0, "sqlservr"),
            (3, 3.0, 4.0, 5.0, "ora_dbw0_ORCL")])
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        self.db.mysql().write()
        self.db.oracle().write()
        self.db.mssql().write()
        result = self.analyse()
        catalogue = charts.chart_catalogue(result)
        drawn = [chart for chart in catalogue if chart.name.startswith("db_")]
        self.assertEqual(len(drawn), 20)
        for chart in drawn:
            svg = chart.render("svg")
            self.assertIn("<svg", svg, chart.name)
            png = chart.render("png")
            self.assertTrue(png.startswith(b"\x89PNG"), chart.name)

    def test_write_all_writes_the_database_charts_as_files(self):
        self.os_run()
        self.db.postgresql().write()
        out = os.path.join(self.tmp, "charts")
        os.makedirs(out)
        names = {os.path.basename(path) for path in charts.write_all(self.analyse(), out, "svg")}
        self.assertIn("db_activity_pg_5432.svg", names)
        self.assertIn("db_waits_pg_5432.svg", names)


# ---------------------------------------------------------------------------
# Naming helpers for the report layer
# ---------------------------------------------------------------------------
class NamingTests(DbChartCase):
    def build(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",), pidstat=POSTGRES_PROCESSES)
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        self.db.postgresql(instance="localhost:5433", stamp="20261016_151321").write()
        result = self.analyse()
        return result, charts.chart_catalogue(result)

    def test_names_and_kinds(self):
        result, _ = self.build()
        instance = self.instance(result, "pg:5432")
        self.assertEqual(dbcharts.chart_name("waits", instance), "db_waits_pg_5432")
        self.assertEqual(dbcharts.latency_chart_name(instance), "db_latency_pg_5432")
        self.assertEqual(dbcharts.chart_kind("db_waits_pg_5432"), "waits")
        self.assertEqual(dbcharts.chart_kind("disk_latency_vda"), "")
        self.assertEqual(dbcharts.chart_kind("db_latency_pg_5432"), "latency")

    def test_charts_for_instance_picks_only_that_instances_charts_in_report_order(self):
        result, catalogue = self.build()
        first = dbcharts.charts_for_instance(catalogue, self.instance(result, "pg:5432"))
        self.assertEqual(first, ["db_activity_pg_5432", "db_waits_pg_5432", "db_latency_pg_5432",
                                 "db_cache_pg_5432", "db_processes_pg_5432"])
        second = dbcharts.charts_for_instance(catalogue, self.instance(result, "pg:5433"))
        self.assertTrue(second)
        self.assertTrue(all(name.endswith("pg_5433") for name in second))
        self.assertIn("db_latency_pg_5433", second)

    def test_a_d_code_takes_the_charts_that_belong_with_it(self):
        result, catalogue = self.build()
        instance = self.instance(result, "pg:5432")
        self.assertEqual(dbcharts.charts_for_instance(catalogue, instance, "D1"),
                         ["db_waits_pg_5432", "db_latency_pg_5432", "db_cache_pg_5432"])
        self.assertEqual(dbcharts.charts_for_instance(catalogue, instance, "D3"),
                         ["db_activity_pg_5432", "db_waits_pg_5432"])
        # A code the table does not know gets all of them.
        self.assertEqual(len(dbcharts.charts_for_instance(catalogue, instance, "D99")), 5)

    def test_charts_for_instance_leaves_out_charts_not_in_the_catalogue(self):
        self.os_run()
        self.db.postgresql(with_waits=False, with_io_timing=False).write()
        result = self.analyse()
        names = dbcharts.charts_for_instance(charts.chart_catalogue(result), self.instance(result, "pg:5432"))
        self.assertEqual(names, ["db_activity_pg_5432", "db_cache_pg_5432"])

    def test_tags_name_the_d_codes_that_use_a_chart(self):
        result, catalogue = self.build()
        latency = next(chart for chart in catalogue if chart.name == "db_latency_pg_5432")
        self.assertEqual(set(latency.tags), {"D1", "D2", "D6"})


# ---------------------------------------------------------------------------
# Captions (the chart title is the report's caption)
# ---------------------------------------------------------------------------
class CaptionTests(DbChartCase):
    def title_of(self, result, name):
        return next(chart.title for chart in charts.chart_catalogue(result) if chart.name == name)

    def test_sampled_waits_say_so_with_the_interval(self):
        self.os_run(interval=15)
        self.db.postgresql().write()
        # The synthetic database timeline is 1 s; the collector's interval is what the caption quotes.
        result = self.analyse()
        self.instance(result, "pg:5432").interval_s = 15
        title = self.title_of(result, "db_waits_pg_5432")
        self.assertIn("sampled every 15 s, not averaged", title)

    def test_averaged_waits_are_not_called_sampled(self):
        self.os_run()
        self.db.mysql().write()
        title = self.title_of(self.analyse(), "db_waits_my_3306")
        self.assertIn("average over each interval", title)
        self.assertNotIn("sampled", title)

    def test_sql_server_waits_say_parallel_tasks_are_counted_individually(self):
        self.os_run()
        self.db.mssql().write()
        self.db.postgresql().write()
        result = self.analyse()
        self.assertIn("parallel tasks are counted individually", self.title_of(result, "db_waits_mssql_MSSQLSERVER"))
        self.assertNotIn("parallel", self.title_of(result, "db_waits_pg_5432"))

    def test_sessions_and_sql_server_cache_hit_are_marked_as_snapshots(self):
        self.os_run()
        self.db.mssql().write()
        result = self.analyse()
        self.assertIn("sessions are sampled", self.title_of(result, "db_activity_mssql_MSSQLSERVER"))
        self.assertIn("hit ratio is sampled", self.title_of(result, "db_cache_mssql_MSSQLSERVER"))

    def test_the_latency_title_names_the_series_and_the_device_overlay(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",))
        self.db.mysql().write()
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        result = self.analyse()
        self.assertEqual(self.title_of(result, "db_latency_my_3306"), "my:3306 read and commit latency (ms)")
        self.assertEqual(self.title_of(result, "db_latency_pg_5432"), "pg:5432 read latency (ms) against device await")


# ---------------------------------------------------------------------------
# Latency chart: device overlay and thresholds
# ---------------------------------------------------------------------------
class LatencyTests(DbChartCase):
    def local_mapped(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        result = self.analyse()
        return self.analyse([self.psar_await(result, "nvme1n1", "disk.r_await_ms", 3.0),
                             self.psar_await(result, "nvme1n1", "disk.w_await_ms", 4.0)])

    def test_a_local_mapped_instance_overlays_the_data_and_log_devices(self):
        result = self.local_mapped()
        overlays = dbcharts.device_overlays(result, self.instance(result, "pg:5432"))
        self.assertEqual([label for _, label, _ in overlays], ["nvme1n1 read await", "nvme1n1 write await"])
        self.assertEqual([series.metric for series, _, _ in overlays], ["disk.r_await_ms", "disk.w_await_ms"])
        svg = self.svg_of(result, "db_latency_pg_5432")
        self.assertIn("nvme1n1 read await", svg)
        self.assertIn("nvme1n1 write await", svg)
        self.assertIn("database read latency", svg)

    def test_without_the_psar_series_the_sar_await_is_the_overlay(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        result = self.analyse()
        overlays = dbcharts.device_overlays(result, self.instance(result, "pg:5432"))
        self.assertEqual([label for _, label, _ in overlays], ["nvme1n1 await", "nvme1n1 await"])
        self.assertTrue(all(series.metric == "disk.await_ms" for series, _, _ in overlays))

    def test_a_local_instance_without_a_mapping_has_no_overlay(self):
        self.os_run(disks=("nvme1n1",))
        self.db.postgresql().write()                     # no data directory, so nothing to place
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        self.assertEqual(instance.devices, {})
        self.assertEqual(dbcharts.device_overlays(result, instance), [])
        self.assertNotIn("against device await", dbcharts.latency_title(result, instance))
        self.assertNotIn("nvme1n1", self.svg_of(result, "db_latency_pg_5432"))

    def test_a_remote_instance_has_no_overlay_even_if_a_device_name_matches(self):
        self.os_run(hardware=inventory(), disks=("nvme1n1",))
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data", local=False).write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        self.assertIs(instance.local, False)
        self.assertEqual(dbcharts.device_overlays(result, instance), [])
        instance.devices = {"data": ["nvme1n1"]}         # even a stray mapping must not be drawn for a remote server
        self.assertEqual(dbcharts.device_overlays(result, instance), [])
        self.assertNotIn("nvme1n1", self.svg_of(result, "db_latency_pg_5432"))

    def test_thresholds_come_from_the_mapped_device_class_then_the_settings(self):
        result = self.local_mapped()
        instance = self.instance(result, "pg:5432")
        # No indicator and no profile in the stub: the device class is resolved from the inventory (NVMe: 2 / 10 ms).
        stub = StubResult(result.host, result.settings)
        self.assertEqual(dbcharts.latency_thresholds(stub, instance, "db_read_latency", dbcharts.DATA_ROLES,
                                                     "db.read_latency"), (2.0, 10.0))
        instance.devices = {}
        self.assertEqual(dbcharts.latency_thresholds(stub, instance, "db_read_latency", dbcharts.DATA_ROLES,
                                                     "db.read_latency"), (10.0, 30.0))
        stub.settings.values["db.read_latency.warn_ms"] = "7"
        stub.settings.values["db.read_latency.crit_ms"] = "21"
        self.assertEqual(dbcharts.latency_thresholds(stub, instance, "db_read_latency", dbcharts.DATA_ROLES,
                                                     "db.read_latency"), (7.0, 21.0))

    def test_thresholds_fall_back_to_the_spec_defaults_when_the_settings_lack_the_keys(self):
        stub = StubResult(host=None, settings=Settings())
        for key in ("db.commit_latency.warn_ms", "db.commit_latency.crit_ms"):
            stub.settings.values.pop(key, None)
        instance = type("Instance", (), {"short": "x", "local": False, "devices": {}})()
        self.assertEqual(dbcharts.latency_thresholds(stub, instance, "db_commit_latency", dbcharts.LOG_ROLES,
                                                     "db.commit_latency"), (10.0, 30.0))

    def test_the_threshold_lines_are_labelled_with_their_values(self):
        self.os_run()
        self.db.postgresql().write()
        svg = self.svg_of(self.analyse(), "db_latency_pg_5432")
        self.assertIn("warn 10 ms", svg)
        self.assertIn("crit 30 ms", svg)

    def test_read_and_commit_thresholds_are_labelled_per_series_when_they_differ(self):
        self.os_run()
        self.db.mysql().write()
        result = self.analyse()
        result.settings.values["db.commit_latency.warn_ms"] = "5"
        result.settings.values["db.commit_latency.crit_ms"] = "15"
        stub = StubResult(result.host, result.settings)
        self.assertEqual(dbcharts.latency_thresholds(stub, self.instance(result, "my:3306"), "db_commit_latency",
                                                     dbcharts.LOG_ROLES, "db.commit_latency"), (5.0, 15.0))
        figure, axes = charts.new_figure()
        dbcharts.draw_latency_thresholds(axes, [("read", (10.0, 30.0)), ("commit", (5.0, 15.0))])
        labels = [text.get_text() for text in axes.texts]
        self.assertEqual(labels, ["read warn 10 ms", "read crit 30 ms", "commit warn 5 ms", "commit crit 15 ms"])
        figure, axes = charts.new_figure()
        dbcharts.draw_latency_thresholds(axes, [("read", (10.0, 30.0)), ("commit", (10.0, 30.0))])
        self.assertEqual([text.get_text() for text in axes.texts], ["warn 10 ms", "crit 30 ms"])

    def test_an_episode_of_the_instance_is_shaded_and_labelled_with_its_duration(self):
        self.os_run()
        self.db.postgresql().write()
        result = self.analyse()
        start = result.host.start + 20
        result.episodes.append(Episode(rule="db_read_latency", device="pg:5432", kind="sustained", severity="crit",
                                       start=start, end=start + 60, duration_s=60, samples=60, exceeding_samples=60,
                                       mean=40.0, p95=40.0, peak=40.0))
        # An episode of another instance is not drawn here.
        result.episodes.append(Episode(rule="db_read_latency", device="pg:9999", kind="sustained", severity="crit",
                                       start=start, end=start + 600, duration_s=600, samples=60, exceeding_samples=60,
                                       mean=40.0, p95=40.0, peak=40.0))
        svg = self.svg_of(result, "db_latency_pg_5432")
        self.assertIn("60 s", svg)
        self.assertNotIn("10 min", svg)


# ---------------------------------------------------------------------------
# Waits chart
# ---------------------------------------------------------------------------
class WaitsTests(DbChartCase):
    def busy_postgres(self, **options):
        self.os_run(**options)
        pg = self.db.postgresql()
        pg.waits[("Lock", "relation")] = [0] * 30 + [3] * 60 + [0] * 30
        pg.waits[("IO", "DataFileRead")] = [0] * 50 + [2] * 30 + [0] * 40
        pg.write()
        return self.analyse()

    def test_categories_that_stay_at_zero_are_left_out_of_the_legend(self):
        svg = self.svg_of(self.busy_postgres(), "db_waits_pg_5432")
        for label in ("CPU", "IO read", "lock"):
            self.assertIn(label, svg)
        for label in ("network", "replica", "latch", "temp"):
            self.assertNotIn(">%s<" % label, svg)

    def test_the_cpu_count_line_is_drawn_for_a_local_instance_when_the_waits_come_near_it(self):
        result = self.busy_postgres()                    # 2 CPUs; the stack peaks above 5 sessions
        self.assertIn("2 CPUs", self.svg_of(result, "db_waits_pg_5432"))

    def test_the_cpu_count_line_is_not_drawn_for_a_remote_instance(self):
        self.os_run()
        pg = self.db.postgresql(local=False)
        pg.waits[("Lock", "relation")] = [3] * 120
        pg.write()
        result = self.analyse()
        self.assertNotIn("CPUs", self.svg_of(result, "db_waits_pg_5432"))

    def test_the_cpu_count_line_is_left_out_when_the_waits_are_far_below_it(self):
        self.os_run(cpus=64)
        self.db.postgresql().write()                     # one CPU-wait session on a 64-CPU host
        self.assertNotIn("CPUs", self.svg_of(self.analyse(), "db_waits_pg_5432"))

    def test_cpu_is_the_first_category_so_it_is_drawn_at_the_bottom(self):
        result = self.busy_postgres()
        names = [category for category, _ in dbcharts.wait_series(self.instance(result, "pg:5432"))]
        self.assertEqual(names[0], "cpu")
        self.assertEqual(names, [name for name in dbcharts.WAIT_CATEGORIES if name in names])

    def test_a_quiet_instance_says_so_instead_of_drawing_an_empty_area(self):
        self.os_run()
        self.db.mssql().write()
        svg = self.svg_of(self.analyse(), "db_waits_mssql_MSSQLSERVER")
        self.assertIn("no sessions waiting in the window", svg)

    def test_the_stack_is_broken_where_the_data_has_a_hole(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.skip_ranges = [(40, 59)]                      # 20 missing samples: more than twice the interval
        pg.write()
        instance = self.instance(self.analyse(), "pg:5432")
        plan = dbcharts.wait_series(instance)
        rows = dbcharts.stack_rows(plan, 1)
        broken = [time_value for time_value, values in rows if all(math.isnan(v) for v in values)]
        self.assertEqual(len(broken), 1)
        # The break sits between the last sample before the hole and the first after it.
        before = max(time for time, values in rows if time < broken[0] and not math.isnan(values[0]))
        after = min(time for time, values in rows if time > broken[0] and not math.isnan(values[0]))
        self.assertLess(before, broken[0])
        self.assertLess(broken[0], after)
        self.assertGreater(after - before, 2)

    def test_a_reset_sample_stays_blank_for_every_category(self):
        self.os_run()
        pg = self.db.postgresql()
        pg.reset_counters_at = [70]
        pg.write()
        instance = self.instance(self.analyse(), "pg:5432")
        self.assertTrue(instance.resets)
        svg = self.svg_of(self.analyse(), "db_waits_pg_5432")
        self.assertIn(">reset<", svg)

    def test_a_category_missing_at_one_time_counts_as_zero_there_but_not_as_a_hole(self):
        low = Series("db.wait.cpu", "", [10, 20, 30], [1.0, 1.0, 1.0], 10)
        high = Series("db.wait.lock", "", [10, 30], [2.0, 2.0], 10)
        rows = dbcharts.stack_rows([("cpu", low), ("lock", high)], 10)
        self.assertEqual(rows, [(10, [1.0, 2.0]), (20, [1.0, 0.0]), (30, [1.0, 2.0])])

    def test_thinning_keeps_the_tallest_stack_of_each_bucket_and_every_break(self):
        rows = [(index, [1.0, 1.0]) for index in range(100)]
        rows[37] = (37, [1.0, 9.0])
        rows[60] = (60, [math.nan, math.nan])
        thinned = dbcharts.thin_rows(rows, limit=20)
        self.assertLessEqual(len(thinned), 40)
        self.assertIn((37, [1.0, 9.0]), thinned)
        self.assertTrue(any(time_value == 60 for time_value, _ in thinned))
        self.assertEqual(dbcharts.thin_rows(rows, limit=1000), rows)


# ---------------------------------------------------------------------------
# Other charts
# ---------------------------------------------------------------------------
class ActivityAndCacheTests(DbChartCase):
    def test_activity_draws_rates_and_sessions_on_a_second_axis(self):
        self.os_run()
        self.db.oracle().write()
        svg = self.svg_of(self.analyse(), "db_activity_ora_ORCL_PDB1")
        for text in ("statements/s", "transactions/s", "active sessions (right axis)"):
            self.assertIn(text, svg)

    def test_activity_for_postgresql_has_transactions_only(self):
        self.os_run()
        self.db.postgresql().write()
        svg = self.svg_of(self.analyse(), "db_activity_pg_5432")
        self.assertIn("transactions/s", svg)
        self.assertNotIn("statements/s", svg)

    def test_cache_chart_has_threshold_lines_and_physical_reads(self):
        self.os_run()
        self.db.postgresql().write()
        svg = self.svg_of(self.analyse(), "db_cache_pg_5432")
        self.assertIn("warn 95 %", svg)
        self.assertIn("crit 90 %", svg)
        self.assertIn("MiB/s", svg)

    def test_cache_axis_range_starts_below_the_lowest_value_or_the_crit_line(self):
        series = Series("db.cache_hit_pct", "", [1, 2, 3], [99.0, 97.0, 99.5], 1)
        self.assertEqual(dbcharts.cache_axis_range(series, 90.0), (85.0, 101.0))
        low = Series("db.cache_hit_pct", "", [1, 2], [40.0, 99.0], 1)
        self.assertEqual(dbcharts.cache_axis_range(low, 90.0), (35.0, 101.0))


class ProcessChartTests(DbChartCase):
    def test_roles_are_summed_per_sample_and_listed_in_the_chart(self):
        self.os_run()
        busy = POSTGRES_PROCESSES + [(103, 10.0, 0.0, 0.0, "postgres: bob mydb 10.0.0.6(1) idle")]
        # Twenty samples with the database busy, then twenty in which only another program shows up.
        log = pidstat_text(busy, last_sample=20) + pidstat_text(
            [(7, 1.0, 0.0, 0.0, "sshd -D")], first_sample=21, last_sample=40).replace(PIDSTAT_HEADER, "")
        write_text(os.path.join(self.tmp, "db01_os_20261016_151320", "pidstat_20261016.log"), log)
        self.db.postgresql().write()
        result = self.analyse()
        instance = self.instance(result, "pg:5432")
        by_role, ranked = dbcharts.role_series(result.host, instance)
        self.assertEqual(ranked[0], "client backend")
        cpu, io = by_role["client backend"]
        self.assertEqual(set(cpu.values[:20]), {40.0})               # 30 % + 10 % in each of the 20 samples
        self.assertEqual(set(io.values[:20]), {400.0})                # kB/s read + write
        self.assertEqual(cpu.values[20:], [0.0] * 20)                # pidstat lists only active tasks
        svg = self.svg_of(result, "db_processes_pg_5432")
        for text in ("client backend", "checkpointer", "background writer"):
            self.assertIn(text, svg)

    def test_processes_of_other_programs_are_not_counted(self):
        self.os_run(pidstat=POSTGRES_PROCESSES + [(7, 90.0, 0.0, 0.0, "stress --cpu 4")])
        self.db.postgresql().write()
        result = self.analyse()
        by_role, _ = dbcharts.role_series(result.host, self.instance(result, "pg:5432"))
        self.assertEqual(max(by_role["client backend"][0].values), 30.0)

    def test_more_roles_than_colours_are_merged_into_other_roles(self):
        commands = [(100 + number, float(number), 0.0, 0.0, "postgres: %s" % title) for number, title in enumerate(
            ("checkpointer", "background writer", "walwriter", "autovacuum worker x", "walsender r 10.0.0.9(1) s",
             "app mydb 10.0.0.5(2) SELECT", "archiver"), 1)]
        self.os_run(pidstat=commands + [(99, 0.0, 0.0, 0.0, "postmaster -D /var/lib/pgsql/16/data")])
        self.db.postgresql().write()
        result = self.analyse()
        by_role, ranked = dbcharts.role_series(result.host, self.instance(result, "pg:5432"))
        self.assertEqual(len(ranked), dbcharts.MAX_ROLE_LINES + 1)
        self.assertEqual(ranked[-1], "other roles")
        self.assertLessEqual(len(ranked), len(dbcharts.SERIES_COLORS))
        # The merged roles are the ones that did the least; nothing is lost from the total.
        total = sum(sum(cpu.values) for cpu, _ in by_role.values())
        self.assertAlmostEqual(total, len(by_role[ranked[0]][0].values) * sum(range(1, 8)))
        self.assertIn("other roles", self.svg_of(result, "db_processes_pg_5432"))

    def test_a_single_process_role_still_draws(self):
        self.os_run(pidstat=[(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")])
        self.db.mysql().write()
        result = self.analyse()
        by_role, ranked = dbcharts.role_series(result.host, self.instance(result, "my:3306"))
        self.assertEqual(ranked, ["server"])
        self.assertIn("<svg", self.svg_of(result, "db_processes_my_3306"))

    def test_thread_rows_are_not_counted_twice(self):
        process = pidstat_text([(1, 30.0, 4.0, 5.0, "mysqld --datadir=/var/lib/mysql")], last_sample=5)
        thread = pidstat_text([(1, 30.0, 4.0, 5.0, "|__mysqld")], last_sample=5).replace(PIDSTAT_HEADER, "")
        self.os_run()
        write_text(os.path.join(self.tmp, "db01_os_20261016_151320", "pidstat_20261016.log"), process + thread)
        self.db.mysql().write()
        result = self.analyse()
        by_role, _ = dbcharts.role_series(result.host, self.instance(result, "my:3306"))
        self.assertEqual(max(by_role["server"][0].values), 30.0)


if __name__ == "__main__":
    unittest.main()
