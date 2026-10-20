"""collect-mssql-metrics.sh 3.0.0 against a fake sqlcmd."""

import os
import shutil
import subprocess
import time

import dbsupport
from dbsupport import CollectorTestCase, header_matches_rows

SCRIPT = "collect-mssql-metrics.sh"

# SQL Server 2019 is major version 15, SQL Server 2022 is 16.
VERSIONS = {"2019": "15", "2022": "16"}

STATS_COLUMNS = (
    "ts,buffer_cache_hit_ratio,page_life_expectancy,checkpoint_pages_total,lazy_writes_total,"
    "memory_grants_pending,target_server_memory_kb,total_server_memory_kb,batch_requests_total,"
    "sql_compilations_total,sql_recompilations_total,"
    "user_connections,processes_blocked,lock_waits_total,lock_wait_time_ms_total,deadlocks_total,"
    "log_flushes_total,log_flush_wait_time_ms_total,log_bytes_flushed_total,page_reads_total,"
    "page_writes_total,free_list_stalls_total,full_scans_total,page_splits_total,"
    "workfiles_created_total,worktables_created_total,memory_grants_outstanding,"
    "active_requests,runnable_tasks,work_queue_count,active_workers,busy_schedulers,server_start_marker"
).split(",")

# Every column fed by a performance counter except the finished percentage.
COUNTER_COLUMNS = [name for name in STATS_COLUMNS[2:27]]

LOCAL_HOST_NAME = os.uname().nodename.split(".")[0]


def sample_calls(run):
    """The logged sqlcmd calls that run the sample file."""
    return [call for call in run.client_calls() if call.startswith("args:") and " -i " in call]


def start_up_calls(run):
    return [call for call in run.client_calls() if call.startswith("args:") and " -Q " in call]


class MssqlFakeClientTests(CollectorTestCase):
    def fresh_run(self, **kwargs):
        """Runs again after removing the output and the log of an earlier run in this test."""
        shutil.rmtree(os.path.join(self.tmp, "out"), ignore_errors=True)
        log_path = os.path.join(self.tmp, "fake.log")
        if os.path.exists(log_path):
            os.remove(log_path)
        return self.run_collector(SCRIPT, **kwargs)

    def test_both_versions_write_matching_headers_and_rows(self):
        for label, major in VERSIONS.items():
            with self.subTest(version=label):
                run = self.fresh_run(fake_env={"FAKE_MSSQL_MAJOR": major})
                self.assertEqual(run.process.returncode, 0, run.process.stderr)
                stats = header_matches_rows(self, run, "mssql_stats.csv")
                self.assertEqual(stats[0], STATS_COLUMNS)
                self.assertEqual(len(stats), 3, "header plus two samples")
                waits = header_matches_rows(self, run, "mssql_waits.csv")
                self.assertEqual(waits[0], ["ts", "wait_type", "waiting_tasks_count", "wait_time_ms", "signal_wait_time_ms"])
                self.assertEqual(len(waits), 1 + 3 * 2)
                files = header_matches_rows(self, run, "mssql_files.csv")
                self.assertEqual(files[0][1:4], ["database", "file_type", "physical_name"])
                self.assertEqual(len(files[0]), 10)
                self.assertEqual(len(files), 1 + 2 * 2)
                self.assertEqual(run.text("errors.log"), "")
                self.assertEqual(run.manifest()["server_version_num"], "%d0000" % int(major))

    def test_a_missing_counter_is_an_empty_field_not_the_word_null(self):
        run = self.run_collector(SCRIPT)
        header, first = dbsupport.csv_rows(run.text("mssql_stats.csv"))[:2]
        self.assertEqual(first[header.index("processes_blocked")], "")
        self.assertEqual(first[header.index("user_connections")], "11")
        self.assertNotIn("NULL", run.text("mssql_stats.csv"))

    def test_quoted_names_with_commas_are_one_field(self):
        run = self.run_collector(SCRIPT)
        files = dbsupport.csv_rows(run.text("mssql_files.csv"))
        self.assertIn("app,db", [row[1] for row in files])
        self.assertIn("/var/opt/mssql/data/app,db_log.ldf", [row[3] for row in files])

    def test_sqlcmd_is_called_with_login_timeout_query_timeout_and_b(self):
        run = self.run_collector(SCRIPT)
        calls = sample_calls(run)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn(" -l 5 ", call)
            self.assertIn(" -t 1 ", call)
            self.assertIn(" -b ", call)
            self.assertIn(" -h -1 -W ", call)
            self.assertIn(" -s, ", call)
            self.assertIn(" -w 65535 ", call)
        for call in start_up_calls(run):
            self.assertIn(" -l 5 ", call)
            self.assertIn(" -b ", call)
            if " -h -1 " in call:
                self.assertIn(" -t 30 ", call, "start-up queries are not held to the 1 s sample timeout")

    def test_tls_switches_are_added_and_reported(self):
        run = self.run_collector(SCRIPT)
        for call in sample_calls(run) + start_up_calls(run):
            self.assertIn(" -C -N ", call)
        self.assertEqual(run.manifest()["tls_trust_server_certificate"], "yes")

    def test_legacy_sqlcmd_gets_no_tls_switches_and_the_manifest_says_so(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_TOOLS": "legacy"})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        for call in sample_calls(run):
            self.assertNotIn(" -C ", call)
            self.assertNotIn(" -N ", call)
        self.assertEqual(run.manifest()["tls_trust_server_certificate"], "no")
        self.assertIn("does not list -C/-N", run.manifest()["notes"])

    def test_help_explains_the_certificate_trust_and_the_permissions(self):
        result = subprocess.run([dbsupport.script_path(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for text in (
            "tls_trust_server_certificate=yes",
            "VIEW SERVER STATE",
            "VIEW SERVER PERFORMANCE STATE",
            "VIEW ANY DEFINITION",
        ):
            self.assertIn(text, result.stdout)

    def test_counters_are_matched_for_named_instances(self):
        sql = self.run_collector(SCRIPT).text("sample.sql")
        for fragment in (
            "RTRIM(object_name) LIKE N'%:Locks'",
            "RTRIM(object_name) LIKE N'%:Databases'",
            "RTRIM(object_name) LIKE N'%:Buffer Manager'",
            "RTRIM(object_name) LIKE N'%:General Statistics'",
            "RTRIM(instance_name) = N'_Total'",
        ):
            self.assertIn(fragment, sql)
        self.assertNotIn("DATEDIFF_BIG", sql)
        self.assertIn("SET ANSI_WARNINGS OFF", sql)

    def test_every_total_instance_counter_asks_for_the_total_instance(self):
        sql = self.run_collector(SCRIPT).text("sample.sql")
        for counter in ("Lock Waits/sec", "Number of Deadlocks/sec", "Log Flushes/sec", "Log Flush Wait Time"):
            line = next(line for line in sql.splitlines() if "N'%s'" % counter in line)
            self.assertIn("RTRIM(instance_name) = N'_Total'", line, counter)

    def test_wait_and_file_queries_have_the_agreed_lists(self):
        sql = self.run_collector(SCRIPT).text("sample.sql")
        for wait in ("WRITELOG", "SOS_SCHEDULER_YIELD", "HADR_SYNC_COMMIT", "CMEMTHREAD", "PAGEIOLATCH[_]%", "PAGELATCH[_]%", "LCK[_]M[_]%"):
            self.assertIn(wait, sql)
        self.assertIn("sys.dm_io_virtual_file_stats(NULL, NULL)", sql)
        self.assertIn("JOIN sys.master_files", sql)
        self.assertIn("DB_NAME(vfs.database_id)", sql)

    def test_counter_types_are_recorded_for_every_counter_column(self):
        manifest = self.run_collector(SCRIPT).manifest()
        for column in COUNTER_COLUMNS:
            self.assertIn("counter_type." + column, manifest)
        self.assertEqual(manifest["counter_type.deadlocks_total"], "272696576")
        self.assertEqual(manifest["counter_type.page_life_expectancy"], "65792")
        self.assertNotIn("counter_type.buffer_cache_hit_ratio", manifest)

    def test_bad_rows_go_to_the_error_log_not_the_csv(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_BAD_ROW": "wrong_count"})
        self.assertIn("S row has 3 fields", run.text("errors.log"))
        self.assertEqual(len(run.lines("mssql_stats.csv")), 3)
        run = self.fresh_run(fake_env={"FAKE_MSSQL_BAD_ROW": "unexpected"})
        self.assertIn("unexpected output: Warning: Null value is eliminated", run.text("errors.log"))
        self.assertEqual(len(run.lines("mssql_stats.csv")), 3)

    def test_sqlcmd_errors_on_standard_output_are_logged_and_the_status_noted(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "error"})
        log = run.text("errors.log")
        self.assertIn("unexpected output: Msg 229, Level 14", log)
        self.assertIn("client exited with status 1", log)
        self.assertEqual(len(run.lines("mssql_stats.csv")), 1, "header only")

    def test_a_hung_client_is_cut_off_and_the_schedule_holds(self):
        started = time.time()
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "timeout"}, count=3, interval=1)
        elapsed = time.time() - started
        self.assertEqual(run.text("errors.log").count("sample timed out after 1 s"), 3)
        self.assertLess(elapsed, 10, "three 1 s samples must not wait for the 30 s fake")
        self.assertEqual(len(run.lines("mssql_stats.csv")), 1, "header only")

    def test_connection_failure_is_reported(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "connect"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("cannot connect to SQL Server", run.process.stderr)

    def test_old_and_unreadable_versions_are_refused(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_MAJOR": "10"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("SQL Server 2012 or later is needed", run.process.stderr)
        run = self.fresh_run(fake_env={"FAKE_MSSQL_MAJOR": "unreadable"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("could not read the SQL Server version", run.process.stderr)

    def test_manifest_keys(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_MAJOR": "16"})
        manifest = run.manifest()
        self.assertEqual(manifest["script_version"], "3.0.0")
        self.assertEqual(manifest["collector_schema"], "3")
        self.assertEqual(manifest["db_engine"], "mssql")
        self.assertEqual(manifest["db_instance"], "MSSQLSERVER")
        self.assertEqual(manifest["server_version"], "16.0.4345.5")
        self.assertEqual(manifest["server_version_num"], "160000")
        self.assertEqual(manifest["server_hostname"], LOCAL_HOST_NAME)
        self.assertEqual(manifest["server_start"], "2026-10-20 08:00:00")
        self.assertEqual(manifest["files"], "mssql_stats.csv mssql_waits.csv mssql_files.csv")
        self.assertEqual(manifest["query_timeout_s"], "1")
        self.assertEqual(manifest["target_local"], "yes")
        self.assertEqual(manifest["setting.max_degree_of_parallelism"], "0")
        self.assertEqual(manifest["setting.cost_threshold_for_parallelism"], "5")
        self.assertEqual(manifest["setting.max_worker_threads"], "0")
        self.assertEqual(manifest["setting.max_server_memory_mb"], "2147483647")
        self.assertEqual(manifest["setting.cpu_count"], "8")
        self.assertEqual(manifest["setting.scheduler_count"], "8")
        self.assertEqual(manifest["setting.max_workers_count"], "512")
        self.assertEqual(manifest["setting.edition"], "Developer Edition (64-bit)")
        self.assertEqual(manifest["notes"], "")
        self.assertIn("end", manifest)

    def test_named_instance_is_the_display_id_and_its_host_part_decides_locality(self):
        run = self.run_collector(SCRIPT, args=["--host", "localhost\\PROD"], fake_env={"FAKE_MSSQL_INSTANCE": "PROD"})
        self.assertEqual(run.manifest()["db_instance"], "PROD")
        self.assertEqual(run.manifest()["target_local_basis"], "loopback")
        self.assertIn(" -S localhost\\PROD ", sample_calls(run)[0])
        run = self.fresh_run(args=["--host", "127.0.0.1", "--port", "1434"])
        self.assertIn(" -S 127.0.0.1,1434 ", sample_calls(run)[0])
        self.assertEqual(run.manifest()["target_local_basis"], "loopback")

    def test_remote_server_is_not_local_and_writes_no_paths(self):
        run = self.run_collector(SCRIPT, args=["--host", "db1.invalid"], fake_env={"FAKE_MSSQL_MACHINE": "remote-sql-1"})
        manifest = run.manifest()
        self.assertEqual(manifest["target_local"], "no")
        self.assertEqual(manifest["target_local_basis"], "none")
        self.assertEqual(manifest["server_hostname"], "remote-sql-1")
        self.assertFalse(run.exists("db_paths.txt"))
        self.assertNotIn("path_namespace", manifest)

    def test_server_reporting_this_hosts_name_is_local(self):
        run = self.run_collector(SCRIPT, args=["--host", "db1.invalid,1433"], fake_env={"FAKE_MSSQL_MACHINE": LOCAL_HOST_NAME})
        self.assertEqual(run.manifest()["target_local"], "yes")
        self.assertEqual(run.manifest()["target_local_basis"], "server hostname")
        self.assertIn("path.data.1=/var/opt/mssql/data", run.lines("db_paths.txt"))

    def test_paths_are_directories_by_role_and_tempdb_files_are_temp(self):
        run = self.run_collector(SCRIPT)
        paths = run.lines("db_paths.txt")
        self.assertEqual(
            paths,
            [
                "path.data.1=/var/opt/mssql/data",
                "path.data.2=/data2/app",
                "path.log.1=/var/opt/mssql/data",
                "path.log.2=/log2",
                "path.temp.1=/var/opt/mssql/data",
                "path.temp.2=/tmpfast",
            ],
        )
        self.assertNotIn("path_namespace", run.manifest())

    def test_container_is_marked_when_the_server_name_differs_over_loopback(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_MACHINE": "3f4a9c1d2b7e"})
        self.assertEqual(run.manifest()["target_local"], "yes")
        self.assertEqual(run.manifest()["target_local_basis"], "loopback")
        self.assertEqual(run.manifest()["path_namespace"], "container")
        paths = run.lines("db_paths.txt")
        self.assertEqual(paths[0], "path_namespace=container")
        self.assertIn("path.data.1=/var/opt/mssql/data", paths)
        self.assertIn("runs in a container", run.manifest()["notes"])

    def test_missing_view_any_definition_adds_a_note_and_leaves_files_empty(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MSSQL_NO_DEFINITION": "1"})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertIn("VIEW ANY DEFINITION", run.manifest()["notes"])
        self.assertEqual(len(run.lines("mssql_files.csv")), 1, "header only")
        self.assertFalse(run.exists("db_paths.txt"))
        self.assertEqual(len(run.lines("mssql_stats.csv")), 3, "the other records are unaffected")

    def test_no_note_about_definitions_when_all_files_are_listed(self):
        run = self.run_collector(SCRIPT)
        self.assertNotIn("VIEW ANY DEFINITION", run.manifest()["notes"])

    def test_unreadable_server_state_names_the_permission_for_the_version(self):
        expected = {"15": "VIEW SERVER STATE", "16": "VIEW SERVER PERFORMANCE STATE"}
        for major, permission in expected.items():
            with self.subTest(major=major):
                run = self.fresh_run(fake_env={"FAKE_MSSQL_NO_SERVER_STATE": "1", "FAKE_MSSQL_MAJOR": major})
                notes = run.manifest()["notes"]
                self.assertIn("grant %s)" % permission, notes)
                self.assertNotIn("setting.cpu_count", run.manifest())
                self.assertFalse([key for key in run.manifest() if key.startswith("counter_type.")])

    def test_password_is_never_on_the_command_line_or_in_the_output(self):
        secret = "s3cret-value-9"
        run = self.run_collector(SCRIPT, args=["--user", "monitor"], fake_env={"SQLCMDPASSWORD": secret})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        for call in run.client_calls():
            self.assertNotIn(secret, call)
            self.assertNotIn(" -P ", call)
        self.assertIn(" -U monitor", sample_calls(run)[0])
        for name in ("manifest.txt", "errors.log", "sample.sql"):
            self.assertNotIn(secret, run.text(name))
        self.assertNotIn(secret, run.process.stdout + run.process.stderr)
        self.assertEqual(run.manifest()["target"], "monitor@localhost/master")

    def test_the_password_option_is_refused(self):
        run = self.run_collector(SCRIPT, args=["-P", "x"])
        self.assertEqual(run.process.returncode, 2)
        self.assertIn("SQLCMDPASSWORD", run.process.stderr)


if __name__ == "__main__":
    import unittest
    unittest.main()
