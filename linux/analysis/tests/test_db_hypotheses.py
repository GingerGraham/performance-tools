"""Database hypotheses D1-D9, their links to the host hypotheses, next steps and text output (spec 11, 12.1, 13.3)."""

import contextlib
import io
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

from perfdiag import analysis, bundle, cli, dbhypotheses, findings, loader, report  # noqa: E402
from perfdiag.hypotheses import Builder  # noqa: E402
from perfdiag.model import Hypothesis  # noqa: E402
from perfdiag.thresholds import Settings  # noqa: E402
from helpers import read_text, write_text  # noqa: E402
from synth import Synth  # noqa: E402
from synth_db import DbSynth, MssqlFile, host_hardware, write_pidstat  # noqa: E402

SAMPLES = 120
WINDOW = (20, 100)
REMOTE_NOTE = ("the database server is remote (dbhost.example.com:5432); the OS data in this bundle is from db01, "
               "the collecting machine, and is not used as evidence")
ABOVE_BLOCK_LAYER = "look above the block layer: filesystem locks, cgroup io.max throttling"


def busy(value, quiet=0.0, window=WINDOW):
    """A per-sample list: `value` inside the window, `quiet` outside."""
    return [value if window[0] <= index < window[1] else quiet for index in range(SAMPLES)]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as exit_request:
            code = exit_request.code
    return code, out.getvalue(), err.getvalue()


class HypothesisCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbhyp-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = DbSynth(self.tmp)

    def os_host(self, disks, mounts, **options):
        """A Synth with these disks ({name: media class}) and mount points; the disks start calm."""
        synth = Synth(self.tmp, **options)
        for name in disks:
            synth.add_disk(name)
        synth.hardware = host_hardware(disks, mounts)
        return synth

    def slow_disk(self, synth, name, await_ms, window=WINDOW, tps=400.0, write_kBps=80000.0):
        disk = synth.disks[name]
        synth.fill(disk["await"], window[0], window[1], await_ms)
        synth.fill(disk["tps"], window[0], window[1], tps)
        synth.fill(disk["wkB"], window[0], window[1], write_kBps)

    def settings(self, **pairs):
        settings = Settings()
        settings.set("analysis.sustain_s", "10", "test")
        settings.set("analysis.gap_samples", "2", "test")
        for key, value in pairs.items():
            settings.set(key.replace("__", "."), value, "test")
        return settings

    def analyse(self, **pairs):
        settings = self.settings(**pairs)
        with bundle.open_bundle(self.tmp) as opened:
            host = loader.load_host(opened, "db01", settings=settings)
            return analysis.run_analysis(host, settings)

    def of(self, result, code, subject=None):
        found = [h for h in result.hypotheses if h.id == code and (subject is None or h.subject == subject)]
        return found[0] if found else None

    def codes(self, result):
        return [h.id for h in result.hypotheses]


# ---------------------------------------------------------------------------
# Process-only database and H hypotheses
# ---------------------------------------------------------------------------
class ProcessOnlyTests(HypothesisCase):
    def storage_incident(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/pgsql": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 20.0)
        write_pidstat(synth.write(), [
            (11, "postgres: checkpointer", 5.0, 0.0, busy(61440.0)),
            (12, "postgres: app db 10.0.0.5(51234) idle", 2.0, 0.0, busy(10240.0)),
            (13, "postgres -D /var/lib/pgsql/16/data", 0.5, 0.0, 0.0)])
        return self.analyse()

    def test_postgres_titles_alone_give_h1_a_database_context_with_the_checkpointer(self):
        result = self.storage_incident()
        h1 = self.of(result, "H1", "nvme1n1")
        self.assertEqual(len(h1.database_context), 1)
        context = h1.database_context[0]
        self.assertEqual((context["instance"], context["source"], context["engine"]), ("pg:proc", "process", "postgresql"))
        self.assertEqual(context["roles_by"], "io")
        roles = {item["role"]: item for item in context["roles"]}
        self.assertEqual(sorted(roles), ["checkpointer", "client backend"])
        self.assertAlmostEqual(roles["checkpointer"]["wr_kBps"], 61440.0 * 80 / 80, delta=1.0)
        self.assertEqual(context["roles"][0]["role"], "checkpointer")          # busiest first
        self.assertIsNone(context["read_latency_ms"])                          # no collector, no database figures
        self.assertEqual(context["waits"], [])

    def test_next_steps_name_the_engine_and_ask_for_the_collector(self):
        h1 = self.of(self.storage_incident(), "H1", "nvme1n1")
        hint = ("Run linux/db/collect-postgresql-metrics.sh alongside collect-os-metrics.sh during the next "
                "occurrence for database-level evidence.")
        self.assertEqual(h1.next_steps[-1], hint)
        self.assertIn("PostgreSQL: review checkpoint settings (checkpoint_timeout, max_wal_size) to spread write bursts.",
                      h1.next_steps)

    def test_the_finding_text_shows_the_database_line(self):
        result = self.storage_incident()
        text = findings.hypothesis_text(result, self.of(result, "H1", "nvme1n1"))
        self.assertIn("Database pg:proc, PostgreSQL (process data only): checkpointer 60.0 MiB/s writes, "
                      "client backend 10.0 MiB/s writes.", text)

    def test_a_process_only_instance_has_no_d_hypotheses(self):
        self.assertEqual([c for c in self.codes(self.storage_incident()) if c.startswith("D")], [])

    def test_without_postgres_in_the_top_processes_h1_has_no_database_context(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/data": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 20.0)
        write_pidstat(synth.write(), [(11, "rsync -a /a /b", 5.0, 0.0, busy(80000.0)),
                                      (12, "postgres: checkpointer", 1.0, 0.0, 0.0)])
        h1 = self.of(self.analyse(), "H1", "nvme1n1")
        self.assertEqual(h1.database_context, [])
        self.assertEqual(h1.links, [])
        self.assertFalse(any("collect-postgresql" in step for step in h1.next_steps))


# ---------------------------------------------------------------------------
# PostgreSQL write bursts: H7 and D6
# ---------------------------------------------------------------------------
class CheckpointBurstTests(HypothesisCase):
    def scenario(self, recorded=None, **analysis_settings):
        """recorded: server settings the collector wrote to the manifest; the rest are perfdiag settings."""
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/pgsql": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 40.0, write_kBps=200000.0)
        synth.fill(synth.psi_io_full, 20, 100, 10.0)
        synth.write()
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data", settings=recorded or {})
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.fill("backend_writes_ps", 20, 100, 80.0)
        run.fill("ckpt_req_ps", 20, 100, 0.05)
        run.write()
        return self.analyse(**analysis_settings)

    def test_h7_and_d6_are_both_raised_and_linked_both_ways(self):
        result = self.scenario()
        h7, d6 = self.of(result, "H7", "nvme1n1"), self.of(result, "D6", "pg:5432")
        self.assertIsNotNone(h7)
        self.assertIsNotNone(d6)
        self.assertIn(("D6", "pg:5432"), h7.links)
        self.assertIn(("H7", "nvme1n1"), d6.links)

    def test_each_side_gets_a_supporting_fact_about_the_other(self):
        result = self.scenario()
        h7, d6 = self.of(result, "H7", "nvme1n1"), self.of(result, "D6", "pg:5432")
        fact = [f for f in h7.supporting_facts if f.endswith("(D6)")][0]
        self.assertRegex(fact, r"^PostgreSQL \(pg:5432\) buffer flush stalls averaged 5\.00 /s in this window \(D6\)$")
        reverse = [f for f in d6.supporting_facts if f.endswith("(H7)")][0]
        self.assertRegex(reverse, r"^nvme1n1 \(/var/lib/pgsql\) await averaged [\d.]+ ms \(H7\)$")

    def test_the_requested_checkpoint_fact_is_on_d6(self):
        d6 = self.of(self.scenario(), "D6", "pg:5432")
        facts = [f for f in d6.supporting_facts if "checkpoints were requested" in f]
        self.assertEqual(len(facts), 1)
        self.assertRegex(facts[0], r"^[34] of [34] checkpoints were requested \(WAL volume reached max_wal_size\), not timed$")

    def test_the_requested_share_has_a_setting(self):
        d6 = self.of(self.scenario(db__checkpoint_req__warn_pct=101), "D6", "pg:5432")
        self.assertFalse([f for f in d6.supporting_facts if "checkpoints were requested" in f])

    def test_backend_writes_and_the_mapped_device_are_evidence_of_d6(self):
        d6 = self.of(self.scenario(), "D6", "pg:5432")
        self.assertTrue(any(f.startswith("backends wrote 84 %") for f in d6.supporting_facts))
        self.assertTrue(any(e.rule == "disk_await" and e.device == "nvme1n1" for e in d6.supporting))
        self.assertEqual(d6.confidence, "high")

    def test_h7_carries_the_database_context_with_the_share_of_the_device(self):
        h7 = self.of(self.scenario(), "H7", "nvme1n1")
        context = h7.database_context[0]
        self.assertEqual((context["instance"], context["source"]), ("pg:5432", "collector"))
        self.assertAlmostEqual(context["transactions_ps"], 51.0, delta=0.5)
        self.assertAlmostEqual(context["write_share_pct"], 100.0 * (10 + 5 + 80) * 8 / 200000.0, delta=0.1)   # 8 kB blocks
        self.assertEqual(context["waits"][0]["category"], "cpu")
        self.assertEqual(context["roles"], [])                     # no pidstat in this bundle

    def test_h1_on_the_same_device_is_linked_too(self):
        result = self.scenario()
        self.assertIn(("D6", "pg:5432"), self.of(result, "H1", "nvme1n1").links)

    def test_next_steps_come_from_the_engine_table_with_recorded_settings(self):
        result = self.scenario(recorded={"max_wal_size": "1GB"})
        d6 = self.of(result, "D6", "pg:5432")
        self.assertEqual(d6.next_steps, [
            "Raise max_wal_size (now 1GB) if checkpoints are requested; tune bgwriter_lru_maxpages if backends write "
            "their own buffers."])
        self.assertEqual(self.of(result, "H7", "nvme1n1").next_steps[-1],
                         "PostgreSQL: review checkpoint settings (checkpoint_timeout, max_wal_size) to spread write bursts.")

    def test_d6_next_step_leaves_out_a_setting_the_run_did_not_record(self):
        d6 = self.of(self.scenario(), "D6", "pg:5432")
        self.assertTrue(d6.next_steps[0].startswith("Raise max_wal_size if checkpoints are requested;"))

    def test_d6_lists_no_process_section_but_keeps_the_top_io_processes_in_the_data(self):
        result = self.scenario()
        d6 = self.of(result, "D6", "pg:5432")
        self.assertEqual(findings.hypothesis_sections(result, d6)["processes"], [])


# ---------------------------------------------------------------------------
# MySQL lock waits on an idle host
# ---------------------------------------------------------------------------
class LockContentionTests(HypothesisCase):
    def scenario(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.mysql(settings={"innodb_flush_log_at_trx_commit": "2", "sync_binlog": "1"})
        run.fill("row_lock_ms_ps", 30, 100, 3000.0)
        run.fill("row_lock_waits_ps", 30, 100, 10.0)
        run.write()
        return self.analyse()

    def test_d3_is_ranked_first_and_no_host_hypothesis_is_raised(self):
        result = self.scenario()
        self.assertEqual(self.codes(result), ["D3"])
        self.assertEqual(result.hypotheses[0].subject, "my:3306")

    def test_the_idle_host_fact_is_present(self):
        d3 = self.scenario().hypotheses[0]
        self.assertIn("host CPU idle averaged 95 % and no disk latency episode", d3.supporting_facts)

    def test_no_idle_host_fact_when_the_host_was_busy(self):
        synth = self.os_host({"vda": "virtual"}, {"/": "vda"})
        for key in synth.cpu_idle:
            synth.fill(synth.cpu_idle[key], 30, 100, 3.0)
        synth.write()
        run = self.db.mysql()
        run.fill("row_lock_ms_ps", 30, 100, 3000.0)
        run.write()
        d3 = self.of(self.analyse(), "D3")
        self.assertFalse([f for f in d3.supporting_facts if f.startswith("host CPU idle")])

    def test_next_step_is_the_mysql_blocker_query(self):
        d3 = self.scenario().hypotheses[0]
        self.assertEqual(d3.next_steps, [
            "Find blockers: sys.innodb_lock_waits (MySQL 8), or information_schema.INNODB_TRX for long transactions."])

    def test_a_stronger_database_problem_outranks_a_milder_host_one(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/mysql": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 5.0, window=(50, 90), write_kBps=100.0)
        synth.write()
        run = self.db.mysql(data_directory="/var/lib/mysql")
        run.fill("row_lock_ms_ps", 30, 100, 10000.0)
        run.write()
        result = self.analyse()
        self.assertEqual(self.codes(result)[:2], ["D3", "H1"])
        self.assertGreater(result.hypotheses[0].rank_score, result.hypotheses[1].rank_score)

    def test_repeated_short_lock_storms_are_a_recurring_d3(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.mysql()
        for first in (20, 40, 60, 80):
            run.fill("row_lock_ms_ps", first, first + 4, 3000.0)
        run.write()
        result = self.analyse()
        d3 = self.of(result, "D3", "my:3306")
        self.assertTrue(d3.recurring)
        self.assertEqual(d3.primary.kind, "transient")
        summary = findings.hypothesis_sections(result, d3)["summary"]
        self.assertIn("my:3306 lock waits exceeded 1 sessions in 4 bursts", summary[0])

    def test_deadlocks_in_the_window_are_a_fact(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.mssql()
        run.fill("lock_wait_ms_ps", 30, 100, 3000.0)
        run.fill("lock_waits_ps", 30, 100, 10.0)
        run.fill("deadlocks_ps", 30, 100, 0.1)
        run.waits["LCK_M_X"] = {"tasks_ps": [5.0] * run.n, "signal_ms_ps": [0.0] * run.n,
                                "wait_ms_ps": [3000.0 if 30 <= i < 100 else 0.0 for i in range(run.n)]}
        run.write()
        d3 = self.of(self.analyse(), "D3")
        self.assertTrue(any(f.endswith("deadlocks in this window") for f in d3.supporting_facts), d3.supporting_facts)


# ---------------------------------------------------------------------------
# Oracle: reads slow above the block layer
# ---------------------------------------------------------------------------
class OracleReadTests(HypothesisCase):
    def scenario(self, device_await):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/u01/oradata": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", device_await, tps=300.0, write_kBps=100.0)
        synth.write()
        run = self.db.oracle(datafile_directory="/u01/oradata")
        run.events["db file sequential read"] = {"waits_ps": busy(200.0, 20.0), "latency_ms": busy(25.0, 1.0)}
        run.write()
        return self.analyse()

    def test_slow_reads_with_a_normal_device_get_the_above_the_block_layer_note(self):
        d1 = self.of(self.scenario(0.5), "D1", "ora:ORCL/PDB1")
        self.assertTrue(any(ABOVE_BLOCK_LAYER in note for note in d1.notes), d1.notes)
        self.assertIn("the device's own latency stayed under its warn threshold while the database waited", d1.notes[0])
        self.assertEqual(d1.primary.threshold["warn"], 2.0)                    # judged as NVMe: the device is mapped
        self.assertEqual(d1.primary.threshold["source"], "default:nvme")
        self.assertFalse([e for e in d1.supporting if e.rule.startswith("disk_")])

    def test_the_note_belongs_in_notes_not_in_against(self):
        result = self.scenario(0.5)
        d1 = self.of(result, "D1", "ora:ORCL/PDB1")
        self.assertEqual(d1.contradicting_facts, [])
        self.assertTrue(findings.hypothesis_sections(result, d1)["notes"])
        self.assertIn("Note: the device's own latency stayed under", findings.hypothesis_text(result, d1))

    def test_a_slow_device_removes_the_note_and_links_d1_to_h1(self):
        result = self.scenario(40.0)
        d1, h1 = self.of(result, "D1", "ora:ORCL/PDB1"), self.of(result, "H1", "nvme1n1")
        self.assertEqual(d1.notes, [])
        self.assertTrue(any(e.rule == "disk_await" and e.device == "nvme1n1" for e in d1.supporting))
        self.assertIn(("H1", "nvme1n1"), d1.links)
        self.assertIn(("D1", "ora:ORCL/PDB1"), h1.links)
        self.assertTrue(any(f.startswith("Oracle (ora:ORCL/PDB1) sessions waiting on IO averaged") and f.endswith("(D1)")
                            for f in h1.supporting_facts), h1.supporting_facts)

    def test_io_waits_and_the_read_latency_are_database_side_evidence(self):
        d1 = self.of(self.scenario(0.5), "D1", "ora:ORCL/PDB1")
        self.assertTrue(any(e.rule == "db_io_wait" for e in d1.supporting))

    def test_oracle_d1_next_steps(self):
        d1 = self.of(self.scenario(0.5), "D1", "ora:ORCL/PDB1")
        self.assertEqual(d1.next_steps[0], "Find the SQL with the most physical reads: v$sqlstats ordered by disk_reads.")


# ---------------------------------------------------------------------------
# SQL Server: per-device file statistics
# ---------------------------------------------------------------------------
class SqlServerDeviceTests(HypothesisCase):
    def scenario(self):
        synth = self.os_host({"sdb": "ssd", "sdc": "ssd"}, {"/var/opt/mssql/data": "sdb", "/mnt/data2": "sdc"})
        for name in ("sdb", "sdc"):
            self.slow_disk(synth, name, 30.0, tps=300.0, write_kBps=100.0)
        synth.write()
        files = [
            MssqlFile("app", "ROWS", "/var/opt/mssql/data/app.mdf", reads_ps=100.0, writes_ps=20.0),
            MssqlFile("app2", "ROWS", "/mnt/data2/app2.ndf", reads_ps=100.0, read_latency_ms=2.0, writes_ps=10.0),
            MssqlFile("app", "LOG", "/mnt/data2/app_log.ldf", reads_ps=0.0, writes_ps=100.0),
        ]
        run = self.db.mssql(files=files, paths={"data": ["/var/opt/mssql/data", "/mnt/data2"], "log": ["/mnt/data2"]})
        files[0].fill("read_latency_ms", 20, 100, 30.0)
        run.write()
        return self.analyse()

    def test_d1_links_to_h1_on_the_slow_device_only(self):
        result = self.scenario()
        d1 = self.of(result, "D1", "mssql:MSSQLSERVER")
        self.assertIsNotNone(d1)
        self.assertEqual(d1.links, [("H1", "sdb")])
        self.assertIn(("D1", "mssql:MSSQLSERVER"), self.of(result, "H1", "sdb").links)
        self.assertEqual(self.of(result, "H1", "sdc").links, [])

    def test_the_other_device_still_shows_the_database_that_uses_it(self):
        sdc = self.of(self.scenario(), "H1", "sdc")
        self.assertEqual([entry["instance"] for entry in sdc.database_context], ["mssql:MSSQLSERVER"])

    def test_d1_is_judged_by_the_class_of_the_first_data_device_and_attributed_to_the_hot_one(self):
        result = self.scenario()
        d1 = self.of(result, "D1", "mssql:MSSQLSERVER")
        self.assertEqual(d1.primary.threshold["source"], "default:ssd")
        self.assertTrue(any(e.device == "sdb" for e in d1.supporting))
        self.assertFalse(any(e.device == "sdc" for e in d1.supporting))
        self.assertEqual(dbhypotheses.scoped_devices(
            Builder(result), result.host.databases[0], "D1", d1.primary), ["sdb"])

    def test_mssql_d1_next_steps(self):
        d1 = self.of(self.scenario(), "D1", "mssql:MSSQLSERVER")
        self.assertEqual(len(d1.next_steps), 2)
        self.assertTrue(d1.next_steps[0].startswith("Compare the per-file read stall in sys.dm_io_virtual_file_stats"))


# ---------------------------------------------------------------------------
# Remote and DB-only bundles
# ---------------------------------------------------------------------------
class RemoteDatabaseTests(HypothesisCase):
    def scenario(self):
        synth = self.os_host({"vda": "cloud-ebs"}, {"/var/lib/pgsql": "vda"})
        self.slow_disk(synth, "vda", 40.0)
        synth.kernel_lines = []
        synth.write()
        run = self.db.postgresql(instance="dbhost.example.com:5432", local=False)
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        return self.analyse()

    def test_d_hypotheses_are_present_with_no_os_evidence(self):
        result = self.scenario()
        d1 = self.of(result, "D1", "pg:5432")
        self.assertIsNotNone(d1)
        self.assertTrue(all(e.rule.startswith("db_") for e in d1.supporting), [e.rule for e in d1.supporting])
        self.assertEqual(d1.supporting_facts, [])
        self.assertEqual(d1.top_processes, [])
        self.assertEqual(d1.kernel_events, [])
        self.assertEqual(findings.hypothesis_sections(result, d1)["not_seen"], [])

    def test_the_remote_note_is_exact(self):
        d1 = self.of(self.scenario(), "D1", "pg:5432")
        self.assertEqual(d1.notes, [REMOTE_NOTE])

    def test_the_host_hypotheses_do_not_link_to_a_remote_database(self):
        result = self.scenario()
        h1 = self.of(result, "H1", "vda")
        self.assertIsNotNone(h1)
        self.assertEqual(h1.database_context, [])
        self.assertEqual(h1.links, [])
        self.assertEqual(self.of(result, "D1", "pg:5432").links, [])

    def test_the_remote_instance_still_gets_indicators_and_a_header(self):
        result = self.scenario()
        self.assertIsNotNone(result.indicator("db_read_latency", "pg:5432"))
        self.assertIn("Database: PostgreSQL 16.4 (pg:5432), collector, remote\n", findings.render_text(result))

    def test_a_remote_note_in_the_text_output(self):
        result = self.scenario()
        self.assertIn("Note: " + REMOTE_NOTE + ".", findings.hypothesis_text(result, self.of(result, "D1", "pg:5432")))

    def test_unknown_locality_is_not_trusted_either(self):
        self.os_host({"vda": "ssd"}, {"/": "vda"}).write()
        run = self.db.postgresql(instance="dbhost.example.com:5432",
                                 extra_manifest={"target_local": "unknown", "target_local_basis": "none"})
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        d1 = self.of(self.analyse(), "D1", "pg:5432")
        self.assertEqual(len(d1.notes), 1)
        self.assertIn("it is not known whether the database server (dbhost.example.com:5432) runs on db01", d1.notes[0])
        self.assertEqual(d1.supporting_facts, [])


class DatabaseOnlyTests(HypothesisCase):
    def test_a_bundle_without_an_os_run_is_analysed_with_database_hypotheses(self):
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        result = self.analyse()
        self.assertEqual(self.codes(result), ["D1"])
        self.assertIn("no os or psar run in the bundle: only database collector data is available for this host",
                      result.host.notes)
        self.assertFalse([n for n in result.notes if n.startswith("PSI")])          # no OS data to lack PSI from
        text = findings.render_text(result)
        self.assertIn("Database: PostgreSQL 16.4 (pg:5432), collector, local\n", text)
        self.assertIn("NOTE: no os or psar run in the bundle", text)
        d1 = self.of(result, "D1")
        self.assertEqual(d1.notes, [])
        self.assertEqual(d1.supporting_facts, [])


# ---------------------------------------------------------------------------
# The other D codes
# ---------------------------------------------------------------------------
class OtherCodeTests(HypothesisCase):
    def calm_host(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()

    def test_d2_postgresql_uses_commit_waits_and_names_the_recorded_settings(self):
        self.calm_host()
        run = self.db.postgresql(settings={"wal_sync_method": "fdatasync", "synchronous_commit": "on"})
        run.waits[("IO", "WALSync")] = [5 if 30 <= i < 100 else 0 for i in range(run.n)]
        run.write()
        d2 = self.of(self.analyse(), "D2", "pg:5432")
        self.assertEqual(d2.primary.rule, "db_commit_wait")
        self.assertIn("sampled every 1 s, not averaged", d2.primary.notes)
        self.assertEqual(d2.next_steps, [
            "Put pg_wal on a low-latency device; check wal_sync_method (now fdatasync) and whether synchronous_commit "
            "(now on) or synchronous_standby_names adds replica round-trips."])

    def test_d2_mysql_commit_latency_and_the_durability_settings(self):
        self.calm_host()
        run = self.db.mysql(settings={"innodb_flush_log_at_trx_commit": "2", "sync_binlog": "0"})
        run.fill("redo_write_latency_ms", 30, 100, 30.0)
        run.write()
        d2 = self.of(self.analyse(), "D2", "my:3306")
        self.assertEqual(d2.primary.rule, "db_commit_latency")
        self.assertEqual(d2.next_steps, [
            "innodb_flush_log_at_trx_commit is 2 and sync_binlog is 0; values other than 1 trade durability for latency "
            "and need sign-off."])

    def test_d2_replica_wait_is_a_fact(self):
        self.calm_host()
        run = self.db.postgresql()
        run.waits[("IO", "WALSync")] = [3 if 30 <= i < 100 else 0 for i in range(run.n)]
        run.waits[("IPC", "SyncRep")] = [2 if 30 <= i < 100 else 0 for i in range(run.n)]
        run.write()
        d2 = self.of(self.analyse(), "D2", "pg:5432")
        self.assertIn("synchronous replica waits averaged 2.00 sessions in this window", d2.supporting_facts)

    def test_d4_names_the_lwlocks(self):
        self.calm_host()
        run = self.db.postgresql()
        run.waits[("LWLock", "BufferMapping")] = [4 if 30 <= i < 100 else 0 for i in range(run.n)]
        run.waits[("LWLock", "LockManager")] = [1 if 30 <= i < 100 else 0 for i in range(run.n)]
        run.write()
        d4 = self.of(self.analyse(), "D4", "pg:5432")
        self.assertIn("latch waits by name: BufferMapping 4.00, LockManager 1.00 sessions", d4.supporting_facts)
        self.assertTrue(d4.next_steps[0].startswith("Group the waits by name"))

    def test_d5_oracle_resource_manager_and_an_idle_host(self):
        self.calm_host()
        run = self.db.oracle()
        run.set_default("cpu_sessions", 3.0)
        run.events["resmgr:cpu quantum"] = {"waits_ps": [100.0] * run.n, "latency_ms": [10.0] * run.n}
        run.write()
        d5 = self.of(self.analyse(), "D5", "ora:ORCL/PDB1")
        self.assertIn("Resource Manager limited CPU; the plan, not the host, set the limit", d5.supporting_facts)
        self.assertEqual(len(d5.contradicting_facts), 1)
        self.assertTrue(d5.contradicting_facts[0].startswith("host CPU idle averaged 95 % while database CPU demand was high"))
        self.assertEqual(d5.confidence, "low")
        self.assertEqual(d5.next_steps, ["Top SQL by CPU in v$sqlstats; check resource_manager_plan."])

    def test_d5_with_a_busy_host_has_the_cpu_episode_and_no_against(self):
        synth = self.os_host({"vda": "virtual"}, {"/": "vda"})
        for key in synth.cpu_idle:
            synth.fill(synth.cpu_idle[key], 20, 100, 2.0)
        synth.write()
        run = self.db.oracle(settings={"resource_manager_plan": "DEFAULT_PLAN"})
        run.set_default("cpu_sessions", 3.0)
        run.write()
        result = self.analyse()
        d5 = self.of(result, "D5", "ora:ORCL/PDB1")
        self.assertTrue(any(e.rule == "cpu_busy" for e in d5.supporting))
        self.assertEqual(d5.contradicting_facts, [])
        self.assertEqual(d5.next_steps, ["Top SQL by CPU in v$sqlstats; check resource_manager_plan (now DEFAULT_PLAN)."])
        self.assertEqual(findings.not_seen_lines(result, d5), ["no IO pressure or disk latency episode in this window."])

    def test_d7_memory_grants_pending(self):
        self.calm_host()
        run = self.db.mssql()
        run.fill("memory_grants_pending", 30, 100, 3)
        run.write()
        d7 = self.of(self.analyse(), "D7", "mssql:MSSQLSERVER")
        self.assertTrue(d7.next_steps[0].startswith("Find the queries waiting for a grant"))

    def test_d8_connection_exhaustion_and_its_generic_step(self):
        self.calm_host()
        run = self.db.mysql()
        run.fill("threads_connected", 30, 100, 148)
        run.write()
        d8 = self.of(self.analyse(), "D8", "my:3306")
        self.assertEqual(d8.primary.rule, "db_connections")
        self.assertEqual(d8.next_steps, ["Compare client pool sizes with max_connections (now 151); check for sessions "
                                         "left open."])

    def test_d9_replication_lag(self):
        self.calm_host()
        run = self.db.postgresql()
        run.fill("replication_lag_s", 30, 100, 120)
        run.write()
        d9 = self.of(self.analyse(), "D9", "pg:5432")
        self.assertEqual(d9.primary.rule, "db_repl_lag")
        self.assertEqual(d9.title, "Replication lag")

    def test_d1_postgresql_steps_with_and_without_the_recorded_buffer_size(self):
        self.calm_host()
        run = self.db.postgresql(settings={"shared_buffers": "4GB"})
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        d1 = self.of(self.analyse(), "D1", "pg:5432")
        self.assertEqual(d1.next_steps[1], "Check shared_buffers (now 4GB) against the working set.")
        self.assertEqual(dbhypotheses.steps_for("D1", "postgresql", {})[1], "Check shared_buffers against the working set.")

    def test_d1_notes_a_read_rate_that_fell(self):
        self.calm_host()
        run = self.db.postgresql()
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("blks_read_ps", 70, 100, 30.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        d1 = self.of(self.analyse(), "D1", "pg:5432")
        self.assertEqual(len(d1.contradicting_facts), 1)
        self.assertTrue(d1.contradicting_facts[0].startswith("the read rate fell during the window"))
        self.assertEqual(d1.confidence, "low")

    def test_d1_without_a_device_mapping_words_disk_episodes_as_unconfirmed_facts(self):
        synth = self.os_host({"vda": "ssd"}, {"/": "vda"})
        self.slow_disk(synth, "vda", 30.0, tps=300.0)
        synth.write()
        run = self.db.postgresql()                   # local, but the collector wrote no file paths
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()
        d1 = self.of(self.analyse(), "D1", "pg:5432")
        self.assertTrue(any(f.startswith("await on vda averaged") and f.endswith("(not confirmed as this database's device)")
                            for f in d1.supporting_facts), d1.supporting_facts)
        self.assertFalse([e for e in d1.supporting if e.rule.startswith("disk_")])
        self.assertEqual(d1.links, [])                # an unmapped instance is not placed on any device


# ---------------------------------------------------------------------------
# Text output and sections
# ---------------------------------------------------------------------------
class TextOutputTests(HypothesisCase):
    def test_the_header_line_of_a_collector_instance(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/pgsql": "nvme1n1"})
        synth.write()
        self.db.postgresql(data_directory="/var/lib/pgsql/16/data").write()
        text = findings.render_text(self.analyse())
        lines = text.splitlines()
        self.assertEqual(lines[2], "Database: PostgreSQL 16.4 (pg:5432), collector, local, data on nvme1n1, WAL on nvme1n1")
        self.assertTrue(lines[1].startswith("Window: "))

    def test_the_header_line_of_a_process_only_instance(self):
        synth = self.os_host({"sdb": "ssd"}, {"/var/lib/mysql": "sdb"})
        write_pidstat(synth.write(), [
            (1, "sshd -D", 0.1, 0.0, 0.0),
            (2, "/usr/sbin/mysqld --datadir=/var/lib/mysql", [1.0 if i < 115 else None for i in range(SAMPLES)], 0.0, 0.0)])
        text = findings.render_text(self.analyse())
        self.assertIn("Database: mysqld (process data only), seen in 96 % of pidstat samples, data on sdb\n", text)

    def test_two_instances_get_two_header_lines_and_their_own_hypotheses(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        for port, stamp in (("5432", "20261016_151320"), ("5433", "20261016_151321")):
            run = self.db.postgresql(instance="localhost:" + port, stamp=stamp)
            run.waits[("Lock", "transactionid")] = [4 if 30 <= i < 100 else 0 for i in range(run.n)]
            run.write()
        result = self.analyse()
        lines = [line for line in findings.render_text(result).splitlines() if line.startswith("Database:")]
        self.assertEqual(lines, ["Database: PostgreSQL 16.4 (pg:5432), collector, local",
                                 "Database: PostgreSQL 16.4 (pg:5433), collector, local"])
        self.assertEqual(sorted(h.subject for h in result.hypotheses if h.id == "D3"), ["pg:5432", "pg:5433"])

    def test_a_host_without_databases_has_no_database_line(self):
        self.os_host({"vda": "ssd"}, {"/": "vda"}).write()
        self.assertNotIn("Database", findings.render_text(self.analyse()))

    def test_the_sustained_table_shows_the_instance_as_the_device(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.oracle()
        run.events["db file sequential read"] = {"waits_ps": busy(200.0, 20.0), "latency_ms": busy(25.0, 1.0)}
        run.write()
        text = findings.render_text(self.analyse())
        rows = [line for line in text.splitlines() if " db_read_latency " in line]
        self.assertEqual(len(rows), 1)
        self.assertIn("ora:ORCL/PDB1", rows[0])
        header = [line for line in text.splitlines() if line.startswith("id ")][0]
        self.assertEqual(header.index("sev"), rows[0].index("warn") if " warn " in rows[0] else rows[0].index("crit"))

    def test_subject_label_is_the_instance_label(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.mysql()
        run.fill("row_lock_ms_ps", 30, 100, 3000.0)
        run.write()
        result = self.analyse()
        self.assertEqual(findings.subject_label(result, result.hypotheses[0]), "MySQL 8.0.36 (localhost:3306)")
        self.assertIn("D3 Lock contention — MySQL 8.0.36 (localhost:3306) — confidence", findings.hypothesis_text(
            result, result.hypotheses[0]))

    def test_the_sections_have_the_new_keys_for_every_hypothesis(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/pgsql": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 40.0, write_kBps=200000.0)
        synth.fill(synth.psi_io_full, 20, 100, 10.0)
        synth.write()
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.write()
        result = self.analyse()
        for hypothesis in result.hypotheses:
            sections = findings.hypothesis_sections(result, hypothesis)
            for key in ("database", "links", "notes", "processes"):
                self.assertIn(key, sections)
        h7 = findings.hypothesis_sections(result, self.of(result, "H7"))
        self.assertEqual(len(h7["database"]), 1)
        self.assertTrue(h7["database"][0].startswith("Database pg:5432, PostgreSQL 16.4 (localhost:5432): "))
        self.assertEqual(h7["links"], ["See also: D6 pg:5432."])
        d6 = findings.hypothesis_sections(result, self.of(result, "D6"))
        self.assertEqual(d6["database"], [])
        self.assertIn("See also: H1 nvme1n1", d6["links"][0])

    def test_not_seen_groups_cover_the_database_codes(self):
        self.assertIn("D1", findings.STORAGE_CODES)
        self.assertIn("D2", findings.STORAGE_CODES)
        self.assertIn("D6", findings.STORAGE_CODES)
        self.assertIn("D5", findings.CPU_CODES)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
class UnitTests(unittest.TestCase):
    def test_settings_placeholders(self):
        self.assertEqual(dbhypotheses.fill_settings("Check a{now:a} and b{is:b}.", {"a": "1", "b": "2"}),
                         "Check a (now 1) and b is 2.")
        self.assertEqual(dbhypotheses.fill_settings("Check a{now:a} and b{is:b}.", {}), "Check a and b.")

    def test_mariadb_uses_the_mysql_steps_and_any_covers_every_engine(self):
        self.assertEqual(dbhypotheses.steps_for("D3", "mariadb", {}), dbhypotheses.steps_for("D3", "mysql", {}))
        self.assertEqual(len(dbhypotheses.steps_for("D8", "oracle", {})), 1)
        self.assertEqual(dbhypotheses.steps_for("D7", "postgresql", {}), [])

    def test_the_old_postgresql_lines_moved_into_the_table(self):
        line = "PostgreSQL: review checkpoint settings (checkpoint_timeout, max_wal_size) to spread write bursts."
        self.assertEqual(dbhypotheses.DB_NEXT_STEPS[("H1", "postgresql")], (line,))
        self.assertEqual(dbhypotheses.DB_NEXT_STEPS[("H7", "postgresql")], (line,))
        from perfdiag import hypotheses
        self.assertFalse(hasattr(hypotheses, "DB_RUN_TAGS"))

    def test_a_supporting_fact_raises_confidence_and_never_lowers_it(self):
        hypothesis = Hypothesis(id="H1", title="t", subject="s", confidence="low", primary=None,
                                supporting=[object()], contradicting_facts=["x"])
        dbhypotheses.add_fact(hypothesis, "a fact")
        self.assertEqual(hypothesis.confidence, "medium")
        dbhypotheses.add_fact(hypothesis, "a fact")                       # the same fact twice counts once
        self.assertEqual(hypothesis.supporting_facts, ["a fact"])
        dbhypotheses.add_fact(hypothesis, "another")
        self.assertEqual(hypothesis.confidence, "medium")                 # 3 for, 1 against, still not clear of doubt
        forced = Hypothesis(id="H6", title="t", subject="s", confidence="high", primary=None,
                            contradicting_facts=["x", "y", "z"])
        dbhypotheses.add_fact(forced, "a fact")
        self.assertEqual(forced.confidence, "high")


# ---------------------------------------------------------------------------
# Command line and reports
# ---------------------------------------------------------------------------
class CommandLineTests(HypothesisCase):
    def scenario(self):
        self.os_host({"vda": "virtual"}, {"/": "vda"}).write()
        run = self.db.postgresql()
        run.fill("blks_read_ps", 20, 100, 200.0)
        run.fill("read_latency_ms", 20, 100, 40.0)
        run.write()

    def test_set_changes_the_minimum_denominator_the_loader_uses(self):
        self.scenario()
        code, out, _ = run_cli("analyse", self.tmp, "--sustain", "10s")
        self.assertEqual(code, 0)
        self.assertIn("D1 Database reads waiting on storage", out)
        code, out, _ = run_cli("analyse", self.tmp, "--sustain", "10s", "--set", "db.min_reads_ps=1000")
        self.assertEqual(code, 0)
        self.assertNotIn("D1 Database", out)

    def test_set_changes_a_threshold(self):
        self.scenario()
        code, out, _ = run_cli("analyse", self.tmp, "--sustain", "10s", "--set", "db.read_latency.warn_ms=100",
                               "--set", "db.read_latency.crit_ms=200")
        self.assertEqual(code, 0)
        self.assertNotIn("D1 Database", out)

    def test_config_file_sets_database_keys(self):
        self.scenario()
        path = os.path.join(self.tmp, "perfdiag.ini")
        write_text(path, "[db.read_latency]\nwarn_ms = 100\ncrit_ms = 200\n")
        code, out, _ = run_cli("analyse", self.tmp, "--sustain", "10s", "--config", path)
        self.assertEqual(code, 0)
        self.assertNotIn("D1 Database", out)

    def test_an_unknown_database_key_is_refused_with_a_suggestion(self):
        self.scenario()
        code, _, err = run_cli("analyse", self.tmp, "--set", "db.read_latency.warn=5")
        self.assertEqual(code, 2)
        self.assertIn("Did you mean: db.read_latency.warn_ms", err)

    def test_findings_json_still_writes_with_database_hypotheses(self):
        self.scenario()
        path = os.path.join(self.tmp, "findings.json")
        code, _, _ = run_cli("analyse", self.tmp, "--sustain", "10s", "--json", path)
        self.assertEqual(code, 0)
        import json
        document = json.loads(read_text(path))
        self.assertEqual([h["id"] for h in document["hypotheses"]], ["D1"])


@unittest.skipUnless(HAVE_MPL, "matplotlib not installed")
class ReportDoesNotCrashTests(HypothesisCase):
    def test_markdown_and_html_render_linked_database_findings(self):
        synth = self.os_host({"nvme1n1": "nvme"}, {"/var/lib/pgsql": "nvme1n1"})
        self.slow_disk(synth, "nvme1n1", 40.0, write_kBps=200000.0)
        synth.fill(synth.psi_io_full, 20, 100, 10.0)
        write_pidstat(synth.write(), [(11, "postgres: checkpointer", 5.0, 0.0, busy(61440.0))])
        run = self.db.postgresql(data_directory="/var/lib/pgsql/16/data")
        run.fill("backend_fsyncs_ps", 20, 100, 5.0)
        run.write()
        remote = self.db.postgresql(instance="dbhost.example.com:5432", local=False, stamp="20261016_151321")
        remote.fill("blks_read_ps", 20, 100, 200.0)
        remote.fill("read_latency_ms", 20, 100, 40.0)
        remote.write()
        result = self.analyse()
        self.assertEqual(sorted(h.id for h in result.hypotheses if h.id.startswith("D")), ["D1", "D6"])
        out = os.path.join(self.tmp, "out")
        paths = report.write_reports([result], out, "both")
        self.assertTrue(any(path.endswith("report.md") for path in paths))
        self.assertIn("D6 Dirty-buffer flushing falling behind", read_text(os.path.join(out, "report.md")))
        self.assertIn("Dirty-buffer flushing falling behind", read_text(os.path.join(out, "report.html"), encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
