"""collect-postgresql-metrics.sh 3.0.0 against a fake psql, plus a live server when one is reachable."""

import os
import shutil
import subprocess
import time
import unittest

import dbsupport
from dbsupport import CollectorTestCase, header_matches_rows

SCRIPT = "collect-postgresql-metrics.sh"

VERSIONS = {
    "9.3": "90300",
    "9.6": "90600",
    "10": "100005",
    "14": "140010",
    "16": "160004",
    "17": "170002",
    "18": "180000",
}


class PostgresqlFakeClientTests(CollectorTestCase):
    def test_every_supported_version_writes_matching_headers_and_rows(self):
        for label, number in VERSIONS.items():
            with self.subTest(version=label):
                run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_VERSION": number})
                self.assertEqual(run.process.returncode, 0, run.process.stderr)
                stats = header_matches_rows(self, run, "pg_stats.csv")
                self.assertEqual(len(stats), 3, "header plus two samples")
                header_matches_rows(self, run, "pg_database.csv")
                self.assertEqual(run.text("errors.log"), "", label)
                self.assertEqual(run.exists("pg_waits.csv"), int(number) >= 90600, label)
                shutil.rmtree(os.path.join(self.tmp, "out"))

    def test_versions_below_9_2_are_refused(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_VERSION": "90100"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("PostgreSQL 9.2 or later is needed", run.process.stderr)

    def test_psql_is_called_with_x_and_on_error_stop(self):
        run = self.run_collector(SCRIPT)
        sample_calls = [call for call in run.client_calls() if call.startswith("args:") and " -f " in call]
        self.assertTrue(sample_calls)
        for call in sample_calls:
            self.assertIn(" -X ", call)
            self.assertIn("ON_ERROR_STOP=1", call)

    def test_default_host_is_the_socket_and_host_option_selects_tcp(self):
        run = self.run_collector(SCRIPT)
        self.assertNotIn(" -h ", run.client_calls()[0])
        self.assertEqual(run.manifest()["target_local_basis"], "socket")
        shutil.rmtree(os.path.join(self.tmp, "out"))
        os.remove(run.log_path)
        run = self.run_collector(SCRIPT, args=["--host", "localhost"])
        self.assertIn(" -h localhost", run.client_calls()[0])
        self.assertEqual(run.manifest()["target_local_basis"], "loopback")
        self.assertEqual(run.manifest()["db_instance"], "localhost:5432")

    def test_sample_sql_has_the_record_letters_and_client_backend_filter(self):
        run = self.run_collector(SCRIPT)
        sql = run.text("sample.sql")
        for letter in ("SELECT 'S'", "SELECT 'D'", "SELECT 'W'"):
            self.assertIn(letter, sql)
        self.assertIn("backend_type = 'client backend'", sql)
        self.assertIn("pid <> pg_backend_pid()", sql)
        self.assertIn("idle in transaction", sql)
        self.assertNotIn("FILTER (WHERE", sql)

    def test_old_server_uses_query_filter_instead_of_backend_type(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_VERSION": "90300"})
        sql = run.text("sample.sql")
        self.assertNotIn("backend_type", sql)
        self.assertIn("query NOT LIKE 'autovacuum:%'", sql)
        self.assertIn("pg_xlog_location_diff", sql)
        self.assertNotIn("wait_event", sql)

    def test_version_gates_choose_the_checkpoint_and_io_sources(self):
        sources = {
            "160004": ("pg_stat_io", "b.checkpoints_timed"),
            "170002": ("pg_stat_io", "pg_stat_checkpointer"),
            "150000": ("b.buffers_backend", "b.checkpoints_timed"),
        }
        for number, expected in sources.items():
            with self.subTest(version=number):
                run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_VERSION": number})
                for fragment in expected:
                    self.assertIn(fragment, run.text("sample.sql"))
                shutil.rmtree(os.path.join(self.tmp, "out"))

    def test_bad_rows_go_to_the_error_log_not_the_csv(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_BAD_ROW": "wrong_count"})
        self.assertIn("S row has 3 fields", run.text("errors.log"))
        self.assertEqual(len(run.lines("pg_stats.csv")), 3)
        shutil.rmtree(os.path.join(self.tmp, "out"))
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_BAD_ROW": "unexpected"})
        self.assertIn("unexpected output: Time: 0.123 ms", run.text("errors.log"))
        self.assertEqual(len(run.lines("pg_stats.csv")), 3)

    def test_database_name_with_a_comma_is_one_field(self):
        run = self.run_collector(SCRIPT)
        rows = dbsupport.csv_rows(run.text("pg_database.csv"))
        self.assertIn("app,db", [row[1] for row in rows])

    def test_a_hung_client_is_cut_off_and_the_schedule_holds(self):
        started = time.time()
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "timeout"}, count=3, interval=1)
        elapsed = time.time() - started
        self.assertIn("sample timed out after 1 s", run.text("errors.log"))
        self.assertEqual(run.text("errors.log").count("sample timed out"), 3)
        self.assertLess(elapsed, 10, "three 1 s samples must not wait for the 30 s fake")
        self.assertEqual(len(run.lines("pg_stats.csv")), 1, "header only")

    def test_connection_failure_is_reported(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "connect"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("cannot connect to PostgreSQL", run.process.stderr)

    def test_manifest_keys(self):
        run = self.run_collector(SCRIPT)
        manifest = run.manifest()
        self.assertEqual(manifest["script_version"], "3.0.0")
        self.assertEqual(manifest["collector_schema"], "3")
        self.assertEqual(manifest["db_engine"], "postgresql")
        self.assertEqual(manifest["server_version_num"], "160004")
        self.assertEqual(manifest["files"], "pg_stats.csv pg_database.csv pg_waits.csv")
        self.assertEqual(manifest["query_timeout_s"], "1")
        self.assertEqual(manifest["setting.shared_buffers"], "128 MB")
        self.assertEqual(manifest["setting.data_directory"], "/var/lib/pgsql/16/data")
        self.assertEqual(manifest["target_local"], "yes")

    def test_paths_are_written_for_a_local_server(self):
        run = self.run_collector(SCRIPT)
        paths = run.lines("db_paths.txt")
        self.assertIn("path.data.1=/var/lib/pgsql/16/data", paths)
        self.assertIn("path.wal.1=/var/lib/pgsql/16/data/pg_wal", paths)
        self.assertIn("path.wal.1.note=assumed inside the data directory; the pg_wal symlink could not be read", paths)
        self.assertIn("path.temp.1=/var/lib/pgsql/16/data/base/pgsql_tmp", paths)

    def test_no_db_paths_for_a_remote_server(self):
        run = self.run_collector(SCRIPT, args=["--host", "db1.invalid"])
        self.assertEqual(run.manifest()["target_local"], "unknown")
        self.assertFalse(run.exists("db_paths.txt"))

    def test_unreadable_data_directory_adds_a_note(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_NO_DATA_DIRECTORY": "1"})
        self.assertIn("data_directory is not readable for this login (grant pg_monitor)", run.manifest()["notes"])
        self.assertFalse(run.exists("db_paths.txt"))

    def test_track_io_timing_off_adds_a_note(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_PG_TRACK_IO": "off"})
        self.assertIn("track_io_timing is off", run.manifest()["notes"])

    def test_home_fallback_note_names_the_gather_search_path(self):
        script = dbsupport.script_path(SCRIPT)
        env = {
            "PATH": dbsupport.FAKE_DIR + os.pathsep + os.environ["PATH"],
            "HOME": self.home,
            "FAKE_LOG": os.path.join(self.tmp, "fake.log"),
        }
        if os.geteuid() == 0 or os.access("/var/log/perf-diag", os.W_OK):
            self.skipTest("/var/log/perf-diag is writable here, so the home fallback is not used")
        result = subprocess.run(
            [script, "-c", "1", "-i", "1", "--foreground"], capture_output=True, text=True, env=env,
            stdin=subprocess.DEVNULL,
        )
        self.assertIn("gather-perf-diag.sh finds runs under ~/perf-diag", result.stderr)

    def test_no_password_in_the_manifest(self):
        run = self.run_collector(SCRIPT, args=["--user", "monitor"], fake_env={"PGPASSWORD": "s3cret-value"})
        self.assertNotIn("s3cret-value", run.text("manifest.txt"))
        self.assertNotIn("s3cret-value", run.text("errors.log"))


def live_server_reachable():
    if not shutil.which("psql"):
        return False
    try:
        result = subprocess.run(
            ["psql", "-X", "-At", "-d", "postgres", "-c", "SELECT 1"], capture_output=True, text=True, timeout=5,
            env=dict(os.environ, PGCONNECT_TIMEOUT="3"),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


@unittest.skipUnless(live_server_reachable(), "no PostgreSQL server reachable over the default socket")
class PostgresqlLiveServerTests(CollectorTestCase):
    def run_live(self, **kwargs):
        script = dbsupport.script_path(SCRIPT)
        base = os.path.join(self.tmp, "out")
        env = dict(os.environ, HOME=self.home)
        process = subprocess.run(
            [script, "-c", "3", "-i", "2", "--foreground", "-o", base], capture_output=True, text=True, env=env,
            stdin=subprocess.DEVNULL, timeout=60,
        )
        return dbsupport.CollectorRun(process, base, os.path.join(self.tmp, "none.log"))

    def test_samples_parse_and_errors_log_is_empty(self):
        run = self.run_live()
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertEqual(run.text("errors.log"), "")
        header_matches_rows(self, run, "pg_stats.csv")
        header_matches_rows(self, run, "pg_database.csv")
        self.assertEqual(len(run.lines("pg_stats.csv")), 4)

    def test_psqlrc_with_timing_does_not_change_the_csv(self):
        with open(os.path.join(self.home, ".psqlrc"), "w") as handle:
            handle.write("\\timing on\n\\x on\n")
        run = self.run_live()
        self.assertEqual(run.text("errors.log"), "")
        self.assertEqual(len(run.lines("pg_stats.csv")), 4)

    def test_counters_cover_every_database(self):
        run = self.run_live()
        rows = dbsupport.csv_rows(run.text("pg_stats.csv"))
        header = rows[0]
        per_database = dbsupport.csv_rows(run.text("pg_database.csv"))
        database_header = per_database[0]
        first_ts = rows[1][0]
        total = int(rows[1][header.index("blks_hit")])
        parts = sum(
            int(row[database_header.index("blks_hit")])
            for row in per_database[1:] if row[0] == first_ts
        )
        # Counters move between the two statements, so the cluster total is at
        # least the sum of the per-database values read a moment later.
        self.assertLessEqual(parts, total + 1000)
        self.assertGreater(parts, 0)

    def test_the_collectors_own_session_is_not_counted(self):
        run = self.run_live()
        rows = dbsupport.csv_rows(run.text("pg_stats.csv"))
        header = rows[0]
        self.assertEqual(rows[1][header.index("active_connections")], "0")
        self.assertEqual(rows[1][header.index("total_connections")], "0")


if __name__ == "__main__":
    unittest.main()
