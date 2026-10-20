"""Database indicators, their thresholds and settings, and how episodes treat them (spec sections 10 and 13.3).

Indicators are built straight from hand-made instances where the point is a rule (thresholds, composites,
labels), and from synth_db bundles where the point is the path through loader and analysis.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag import analysis, bundle, episodes, loader  # noqa: E402
from perfdiag.model import DatabaseInstance, HostData, Series  # noqa: E402
from perfdiag.readers.manifest import parse_hardware  # noqa: E402
from perfdiag.rules import Context, PRIMARY, SUPPORTING, build_indicators  # noqa: E402
from perfdiag.thresholds import DB_KEYS, DEFAULTS, Settings, SettingsError  # noqa: E402
from synth import BASE_EPOCH, Synth  # noqa: E402
from synth_db import DbSynth, host_hardware  # noqa: E402

NAN = float("nan")


def db_series(metric, values, sub="", interval=1):
    times = [BASE_EPOCH + (index + 1) * interval for index in range(len(values))]
    return Series(metric, sub, times, list(values), interval, "db")


def make_instance(series=(), local=True, devices=None, basis=None, resets=(), engine="postgresql", interval_s=1):
    """A DatabaseInstance with the given (metric, values[, sub]) series."""
    instance = DatabaseInstance(
        id="%s:localhost:5432" % engine, short="pg:5432", engine=engine, label="PostgreSQL 16.4 (localhost:5432)",
        source="collector", local=local, interval_s=interval_s, devices=dict(devices or {}),
        basis=dict(basis or {}), resets=list(resets))
    for entry in series:
        metric, values = entry[0], entry[1]
        sub = entry[2] if len(entry) > 2 else ""
        instance.series[(metric, sub)] = db_series(metric, values, sub)
    return instance


def indicators_of(instance, hardware=None, **settings_pairs):
    host = HostData(host="db01")
    host.cpus = 2
    host.hardware = hardware
    host.databases = [instance]
    settings = Settings()
    for key, value in settings_pairs.items():
        settings.set(key.replace("__", "."), value, "test")
    found = build_indicators(Context(host, settings))
    return {indicator.name: indicator for indicator in found}


def storage_hardware():
    """An HDD at /data and an NVMe at /wal."""
    return parse_hardware(host_hardware({"sda": "hdd", "nvme0n1": "nvme"}, {"/data": "sda", "/wal": "nvme0n1"}))


class IndicatorTests(unittest.TestCase):
    def test_every_indicator_of_the_spec_exists_with_the_instance_as_its_device(self):
        series = [("db.read_latency_ms", [5.0] * 10), ("db.commit_latency_ms", [5.0] * 10),
                  ("db.wait.lock", [0.0] * 10), ("db.wait.latch", [0.0] * 10), ("db.wait.io_read", [1.0] * 10),
                  ("db.cpu_demand_per_cpu", [0.1] * 10), ("db.concurrency_per_cpu", [0.1] * 10),
                  ("db.flush_stalls_ps", [0.0] * 10), ("db.memory_grants_pending", [0.0] * 10),
                  ("db.connections_pct", [10.0] * 10), ("db.worker_queue", [0.0] * 10),
                  ("db.replication_lag_s", [0.0] * 10), ("db.cache_hit_pct", [99.0] * 10),
                  ("db.temp_kBps", [0.0] * 10), ("db.xact_age_s", [1.0] * 10), ("db.history_list", [10.0] * 10),
                  ("db.compile_pct", [1.0] * 10), ("db.dirty_pct", [5.0] * 10)]
        found = indicators_of(make_instance(series))
        expected = {"db_read_latency", "db_commit_latency", "db_lock_wait", "db_latch_wait", "db_io_wait",
                    "db_cpu_demand", "db_concurrency", "db_flush_stall", "db_memory_grants", "db_connections",
                    "db_worker_queue", "db_repl_lag", "db_cache_hit", "db_temp", "db_xact_age", "db_history_list",
                    "db_compile", "db_dirty"}
        self.assertEqual({name for name in found if name.startswith("db_")}, expected)
        for indicator in found.values():
            self.assertEqual(indicator.device, "pg:5432")
            self.assertTrue(indicator.title.startswith("pg:5432 "), indicator.title)
        self.assertEqual(found["db_read_latency"].metric, "db.read_latency_ms")

    def test_load_indicators_are_supporting_only(self):
        series = [("db.wait.io_read", [1.0] * 5), ("db.temp_kBps", [1.0] * 5), ("db.concurrency_per_cpu", [1.0] * 5),
                  ("db.cache_hit_pct", [99.0] * 5), ("db.read_latency_ms", [1.0] * 5), ("db.wait.lock", [1.0] * 5)]
        found = indicators_of(make_instance(series))
        for name in ("db_io_wait", "db_temp", "db_concurrency", "db_cache_hit"):
            self.assertEqual(found[name].role, SUPPORTING, name)
        for name in ("db_read_latency", "db_lock_wait"):
            self.assertEqual(found[name].role, PRIMARY, name)

    def test_directions_and_default_thresholds(self):
        series = [("db.cache_hit_pct", [99.0] * 5), ("db.wait.lock", [1.0] * 5), ("db.connections_pct", [1.0] * 5),
                  ("db.replication_lag_s", [1.0] * 5), ("db.flush_stalls_ps", [0.0] * 5)]
        found = indicators_of(make_instance(series))
        cache = found["db_cache_hit"]
        self.assertEqual((cache.direction, cache.warn, cache.crit), ("below", 95.0, 90.0))
        self.assertEqual((found["db_lock_wait"].direction, found["db_lock_wait"].warn, found["db_lock_wait"].crit),
                         ("above", 1.0, 5.0))
        self.assertEqual((found["db_connections"].warn, found["db_connections"].crit), (80.0, 95.0))
        self.assertEqual((found["db_repl_lag"].warn, found["db_repl_lag"].crit), (30.0, 300.0))
        self.assertEqual((found["db_flush_stall"].warn, found["db_flush_stall"].crit), (0.01, 1.0))

    def test_every_setting_of_section_10_2_is_a_default(self):
        spec_keys = """db.min_reads_ps db.min_commits_ps db.read_latency.warn_ms db.read_latency.crit_ms
            db.commit_latency.warn_ms db.commit_latency.crit_ms db.commit_wait.warn_sessions db.commit_wait.crit_sessions
            db.lock_wait.warn_sessions db.lock_wait.crit_sessions db.latch_wait.warn_sessions db.latch_wait.crit_sessions
            db.io_wait.warn_sessions db.io_wait.crit_sessions db.cpu_demand.warn_per_cpu db.cpu_demand.crit_per_cpu
            db.flush_stall.warn_ps db.flush_stall.crit_ps db.memory_grants.warn db.memory_grants.crit
            db.connections.warn_pct db.connections.crit_pct db.worker_queue.warn db.worker_queue.crit
            db.replication_lag.warn_s db.replication_lag.crit_s db.cache_hit.warn_pct db.cache_hit.crit_pct
            db.cache_hit.min_reads_ps db.temp.warn_kBps db.temp.crit_kBps db.xact_age.warn_s db.xact_age.crit_s
            db.history_list.warn db.history_list.crit db.compile.warn_pct db.compile.crit_pct db.dirty.warn_pct
            db.dirty.crit_pct db.checkpoint_req.warn_pct""".split()
        self.assertEqual(sorted(DB_KEYS), sorted(spec_keys))
        for key in spec_keys:
            self.assertIn(key, DEFAULTS)
        self.assertEqual(DEFAULTS["db.checkpoint_req.warn_pct"], "50")
        self.assertEqual(DEFAULTS["db.history_list.crit"], "1000000")


class LatencyThresholdTests(unittest.TestCase):
    SERIES = [("db.read_latency_ms", [5.0] * 10), ("db.commit_latency_ms", [5.0] * 10)]

    def test_a_mapped_local_data_device_sets_the_read_threshold_by_its_class(self):
        instance = make_instance(self.SERIES, devices={"data": ["nvme0n1"]})
        read = indicators_of(instance, storage_hardware())["db_read_latency"]
        self.assertEqual((read.warn, read.crit, read.threshold_source), (2.0, 10.0, "default:nvme"))

    def test_the_log_device_sets_the_commit_threshold_independently(self):
        instance = make_instance(self.SERIES, devices={"data": ["sda"], "wal": ["nvme0n1"]})
        found = indicators_of(instance, storage_hardware())
        self.assertEqual((found["db_read_latency"].warn, found["db_read_latency"].crit), (20.0, 50.0))
        self.assertEqual((found["db_commit_latency"].warn, found["db_commit_latency"].crit), (2.0, 10.0))

    def test_redo_and_log_roles_count_as_the_commit_device(self):
        for role in ("redo", "log"):
            instance = make_instance(self.SERIES, devices={role: ["sda"]})
            found = indicators_of(instance, storage_hardware())
            self.assertEqual(found["db_commit_latency"].warn, 20.0, role)

    def test_an_unmapped_instance_uses_the_db_settings(self):
        read = indicators_of(make_instance(self.SERIES), storage_hardware())["db_read_latency"]
        self.assertEqual((read.warn, read.crit, read.threshold_source), (10.0, 30.0, "default"))

    def test_a_remote_instance_uses_the_db_settings_even_with_a_device_list(self):
        instance = make_instance(self.SERIES, local=False, devices={"data": ["nvme0n1"]})
        read = indicators_of(instance, storage_hardware())["db_read_latency"]
        self.assertEqual((read.warn, read.crit), (10.0, 30.0))

    def test_a_user_setting_beats_the_device_class(self):
        instance = make_instance(self.SERIES, devices={"data": ["nvme0n1"]})
        found = indicators_of(instance, storage_hardware(), db__read_latency__warn_ms=25, db__read_latency__crit_ms=60)
        read = found["db_read_latency"]
        self.assertEqual((read.warn, read.crit, read.threshold_source), (25.0, 60.0, "config"))
        self.assertEqual(found["db_commit_latency"].warn, 10.0)         # the commit pair was not touched

    def test_the_class_override_of_a_device_flows_into_the_database_threshold(self):
        instance = make_instance(self.SERIES, devices={"data": ["nvme0n1"]})
        found = indicators_of(instance, storage_hardware(), device__nvme0n1__class="hdd")
        self.assertEqual(found["db_read_latency"].warn, 20.0)

    def test_postgresql_commit_waits_stand_in_only_without_a_latency_source(self):
        waits = [("db.wait.commit", [2.0] * 10)]
        self.assertIn("db_commit_wait", indicators_of(make_instance(waits)))
        both = indicators_of(make_instance(waits + [("db.commit_latency_ms", [2.0] * 10)]))
        self.assertNotIn("db_commit_wait", both)
        self.assertIn("db_commit_latency", both)


class CompositeTests(unittest.TestCase):
    def test_io_wait_adds_reads_and_writes_and_survives_one_missing_part(self):
        series = [("db.wait.io_read", [1.0, 2.0, NAN, NAN]), ("db.wait.io_write", [0.5, NAN, 3.0, NAN])]
        indicator = indicators_of(make_instance(series))["db_io_wait"]
        self.assertEqual(indicator.values[:3], [1.5, 2.0, 3.0])
        self.assertTrue(indicator.values[3] != indicator.values[3])        # no part has a value: blank
        self.assertEqual(indicator.role, SUPPORTING)

    def test_io_wait_with_only_reads(self):
        indicator = indicators_of(make_instance([("db.wait.io_read", [4.0] * 3)]))["db_io_wait"]
        self.assertEqual(indicator.values, [4.0] * 3)

    def test_memory_grants_take_the_worse_of_the_gauge_and_the_wait_time(self):
        series = [("db.memory_grants_pending", [0.0, 3.0, 0.0]), ("db.wait.memory", [2.0, 1.0, 0.0])]
        indicator = indicators_of(make_instance(series, engine="mssql"))["db_memory_grants"]
        self.assertEqual(indicator.values, [2.0, 3.0, 0.0])

    def test_memory_grants_from_waits_alone(self):
        indicator = indicators_of(make_instance([("db.wait.memory", [2.0] * 3)]))["db_memory_grants"]
        self.assertEqual(indicator.values, [2.0] * 3)
        self.assertEqual((indicator.warn, indicator.crit), (1.0, 5.0))

    def test_worker_queue_uses_queue_length_or_threadpool_waits(self):
        series = [("db.worker_queue", [0.0, 4.0]), ("db.wait.workers", [1.0, 0.0])]
        indicator = indicators_of(make_instance(series, engine="mssql"))["db_worker_queue"]
        self.assertEqual(indicator.values, [1.0, 4.0])
        self.assertEqual((indicator.warn, indicator.crit), (1.0, 10.0))

    def test_dirty_takes_dirty_pages_or_checkpoint_age(self):
        series = [("db.dirty_pct", [10.0, 80.0]), ("db.checkpoint_age_pct", [60.0, 30.0])]
        indicator = indicators_of(make_instance(series, engine="mariadb"))["db_dirty"]
        self.assertEqual(indicator.values, [60.0, 80.0])
        self.assertEqual((indicator.warn, indicator.crit), (75.0, 90.0))

    def test_concurrency_shares_the_cpu_demand_thresholds(self):
        indicator = indicators_of(make_instance([("db.concurrency_per_cpu", [1.0] * 3)], engine="mysql"),
                                  db__cpu_demand__warn_per_cpu=2.5)["db_concurrency"]
        self.assertEqual(indicator.warn, 2.5)


class SampledLabelTests(unittest.TestCase):
    def test_sampled_metrics_say_so_with_the_instance_interval(self):
        instance = make_instance([("db.wait.lock", [1.0] * 5), ("db.read_latency_ms", [1.0] * 5)],
                                 basis={"db.wait.lock": "sampled", "db.read_latency_ms": "time-averaged"},
                                 interval_s=15)
        found = indicators_of(instance)
        self.assertEqual(found["db_lock_wait"].labels, ["sampled every 15 s, not averaged"])
        self.assertEqual(found["db_read_latency"].labels, [])

    def test_a_composite_with_one_sampled_part_is_labelled(self):
        instance = make_instance([("db.memory_grants_pending", [1.0] * 5), ("db.wait.memory", [1.0] * 5)],
                                 basis={"db.memory_grants_pending": "sampled", "db.wait.memory": "time-averaged"})
        self.assertEqual(indicators_of(instance)["db_memory_grants"].labels, ["sampled every 1 s, not averaged"])

    def test_the_label_reaches_the_episode_notes(self):
        tmp = tempfile.mkdtemp(prefix="perfdiag-rules-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Synth(tmp).write()
        run = DbSynth(tmp).postgresql()
        run.waits[("Lock", "transactionid")] = [4 if 30 <= index < 100 else 0 for index in range(run.n)]
        run.write()
        result = analyse(tmp)
        episode = result.episodes_for("db_lock_wait", "pg:5432", "sustained")[0]
        self.assertIn("sampled every 1 s, not averaged", episode.notes)


class BreakTests(unittest.TestCase):
    def test_resets_become_breaks_of_the_indicator(self):
        instance = make_instance([("db.flush_stalls_ps", [1.0] * 5)], resets=[BASE_EPOCH + 3])
        self.assertEqual(indicators_of(instance)["db_flush_stall"].breaks, [BASE_EPOCH + 3])

    def test_os_indicators_have_no_breaks(self):
        tmp = tempfile.mkdtemp(prefix="perfdiag-rules-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        Synth(tmp).write()
        result = analyse(tmp)
        self.assertTrue(all(indicator.breaks == [] for indicator in result.indicators.values()))

    def test_segments_cut_at_each_break_and_keep_crit_values_aligned(self):
        from perfdiag.rules import Indicator
        indicator = Indicator("x", "d", "x", "", 1.0, 2.0, "above", [1, 2, 3, 4, 5, 6], [1, 1, 1, 1, 1, 1], 1,
                              crit_values=[10, 11, 12, 13, 14, 15], breaks=[3, 5, 99])
        segments = analysis._segments(indicator)
        self.assertEqual([[t for t, _ in points] for points, _ in segments], [[1, 2], [3, 4], [5, 6]])
        self.assertEqual([crit for _, crit in segments], [[10, 11], [12, 13], [14, 15]])
        plain = Indicator("x", "d", "x", "", 1.0, 2.0, "above", [1, 2, 3], [1, 1, 1], 1)
        self.assertEqual(len(analysis._segments(plain)), 1)


# ---------------------------------------------------------------------------
# Through loader and analysis
# ---------------------------------------------------------------------------
def analyse(root, settings=None, host="db01"):
    settings = settings or Settings()
    settings.set("analysis.sustain_s", "10", "test")
    settings.set("analysis.gap_samples", "2", "test")
    with bundle.open_bundle(root) as opened:
        data = loader.load_host(opened, host, settings=settings)
        return analysis.run_analysis(data, settings)


class AnalysisCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-rules-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = DbSynth(self.tmp)


class EpisodeTests(AnalysisCase):
    def test_a_counter_reset_in_the_middle_of_a_stall_splits_the_episode(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.reset_counters_at = [60]
        run.write()
        result = analyse(self.tmp)
        reset = BASE_EPOCH + 61
        instance = result.host.databases[0]
        self.assertEqual(instance.resets, [reset])
        found = sorted(result.episodes_for("db_flush_stall", "pg:5432"), key=lambda e: e.start)
        self.assertEqual([e.kind for e in found], ["sustained", "sustained"])
        for episode in found:
            self.assertFalse(episode.start < reset <= episode.end, "episode spans the reset: %r" % episode)
        # Without the break the two-sample gap tolerance would have bridged the blank sample.
        indicator = result.indicator("db_flush_stall", "pg:5432")
        rule = episodes.Rule(indicator.name, indicator.metric, indicator.warn, indicator.crit, indicator.direction)
        bridged = episodes.detect_episodes(indicator.points(), rule, indicator.device, 1, 10, 2, 3)
        self.assertEqual(len(bridged), 1)

    def test_a_marker_change_alone_also_splits(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.marker_changes_at = [60]
        run.write()
        found = analyse(self.tmp).episodes_for("db_flush_stall", "pg:5432")
        self.assertEqual(len(found), 2)

    def test_a_transient_cache_hit_dip_matters_when_reads_got_slow(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("blks_hit_ps", 50, 54, 800.0)               # 800 hits against 200 reads a second: 80 %
        run.fill("blks_read_ps", 50, 54, 200.0)
        run.fill("read_latency_ms", 49, 56, 40.0)
        run.write()
        result = analyse(self.tmp)
        cache = result.episodes_for("db_cache_hit", "pg:5432", "transient")
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache[0].impact, "impactful")
        self.assertIn("pg:5432 read latency peaked", cache[0].evidence[0])

    def test_a_transient_cache_hit_dip_without_slow_reads_is_absorbed(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("blks_hit_ps", 50, 54, 800.0)
        run.fill("blks_read_ps", 50, 54, 200.0)
        run.write()
        cache = analyse(self.tmp).episodes_for("db_cache_hit", "pg:5432", "transient")
        self.assertEqual([e.impact for e in cache], ["absorbed"])

    def test_cache_hit_pairs_with_its_own_instance_not_another_one(self):
        Synth(self.tmp).write()
        quiet = self.db.postgresql(instance="localhost:5432", stamp="20261016_151320")
        quiet.fill("blks_hit_ps", 50, 54, 800.0)
        quiet.fill("blks_read_ps", 50, 54, 200.0)
        quiet.write()
        slow = self.db.postgresql(instance="localhost:5433", stamp="20261016_151321")
        slow.fill("read_latency_ms", 49, 56, 40.0)
        slow.write()
        result = analyse(self.tmp)
        dip = result.episodes_for("db_cache_hit", "pg:5432", "transient")
        self.assertEqual([e.impact for e in dip], ["absorbed"])

    def temp_spike_scenario(self, slow_disk):
        """A 4 s temporary-file burst of the database on /var/lib/pgsql (nvme1n1) while `slow_disk` is slow."""
        synth = Synth(self.tmp)
        synth.add_disk("nvme1n1")
        synth.hardware = host_hardware({"nvme1n1": "nvme", "vda": "virtual"}, {"/var/lib/pgsql": "nvme1n1", "/": "vda"})
        for name in ("nvme1n1", "vda"):
            synth.fill(synth.disks[name]["tps"], 0, 120, 300.0)
        synth.fill(synth.disks[slow_disk]["await"], 49, 56, 30.0)
        synth.write()
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.fill("temp_bytes_ps", 50, 54, 20 * 1024 * 1024.0)
        run.write()
        return analyse(self.tmp).episodes_for("db_temp", "pg:5432", "transient")

    def test_a_temporary_file_burst_matters_when_the_database_s_own_disk_slowed(self):
        found = self.temp_spike_scenario("nvme1n1")
        self.assertEqual([e.impact for e in found], ["impactful"])
        self.assertIn("nvme1n1 await peaked", found[0].evidence[0])

    def test_a_temporary_file_burst_is_not_explained_by_another_disk_slowing(self):
        found = self.temp_spike_scenario("vda")
        self.assertEqual([e.impact for e in found], ["absorbed"])

    def test_a_load_indicator_alone_raises_no_hypothesis(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("blks_hit_ps", 20, 100, 1000.0)             # 83 % hit ratio for a long time
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.write()
        result = analyse(self.tmp)
        self.assertTrue(result.episodes_for("db_cache_hit", "pg:5432", "sustained"))
        self.assertEqual(result.hypotheses, [])

    def test_database_episodes_list_with_the_instance_as_device_and_the_supporting_flag(self):
        from perfdiag import findings
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("blks_hit_ps", 20, 100, 1000.0)
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.write()
        text = findings.render_text(analyse(self.tmp))
        row = [line for line in text.splitlines() if " db_cache_hit " in line][0]
        self.assertIn("pg:5432", row)
        self.assertTrue(row.endswith("[supporting]"))

    def test_no_database_leaves_the_columns_of_the_episode_table_alone(self):
        from perfdiag import findings
        synth = Synth(self.tmp)
        synth.fill(synth.disks["vda"]["await"], 10, 100, 30.0)
        synth.fill(synth.disks["vda"]["tps"], 10, 100, 300.0)
        synth.write()
        text = findings.render_text(analyse(self.tmp))
        self.assertIn("%-4s %-16s %-10s %-5s" % ("id", "indicator", "device", "sev"), text)
        self.assertNotIn("Database", text)


class SettingsOverrideTests(AnalysisCase):
    def scenario(self):
        Synth(self.tmp).write()
        run = self.db.postgresql()
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()

    def test_the_minimum_reads_setting_reaches_the_loader(self):
        self.scenario()
        self.assertTrue(analyse(self.tmp).episodes_for("db_read_latency", "pg:5432", "sustained"))
        settings = Settings()
        settings.set("db.min_reads_ps", "1000", "test")
        result = analyse(self.tmp, settings)
        self.assertIsNone(result.indicator("db_read_latency", "pg:5432"))

    def test_a_threshold_setting_moves_the_episode(self):
        self.scenario()
        settings = Settings()
        settings.set("db.read_latency.warn_ms", "60", "test")
        settings.set("db.read_latency.crit_ms", "90", "test")
        result = analyse(self.tmp, settings)
        self.assertEqual(result.episodes_for("db_read_latency", "pg:5432"), [])
        self.assertEqual(result.indicator("db_read_latency", "pg:5432").warn, 60.0)

    def test_the_ini_file_sets_database_keys_like_any_other(self):
        path = os.path.join(self.tmp, "perfdiag.ini")
        with open(path, "w") as handle:
            handle.write("[db.lock_wait]\nwarn_sessions = 7\ncrit_sessions = 9\n[db]\nmin_reads_ps = 50\n")
        settings = Settings()
        settings.load_ini(path)
        self.assertEqual(settings.float("db.lock_wait.warn_sessions"), 7.0)
        self.assertEqual(settings.float("db.min_reads_ps"), 50.0)
        self.assertEqual(settings.changed["db.min_reads_ps"], ("50", path))

    def test_validation_covers_the_new_keys(self):
        settings = Settings()
        with self.assertRaisesRegex(SettingsError, "needs a number"):
            settings.set("db.lock_wait.warn_sessions", "many", "test")
        with self.assertRaisesRegex(SettingsError, "Did you mean: db.lock_wait.warn_sessions"):
            settings.set("db.lock_wait.warn", "3", "test")


if __name__ == "__main__":
    unittest.main()
