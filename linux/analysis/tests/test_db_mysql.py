"""collect-mysql-metrics.sh 3.0.0 against fake mysql and mariadb clients, plus a live server when one is reachable."""

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

import dbsupport
from dbsupport import CollectorTestCase, header_matches_rows
from helpers import read_text, write_text

SCRIPT = "collect-mysql-metrics.sh"

# The 19 columns of earlier versions, in their original order.
EARLIER_COLUMNS = [
    "Threads_connected", "Threads_running", "Questions", "Slow_queries",
    "Com_select", "Com_insert", "Com_update", "Com_delete",
    "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
    "Innodb_row_lock_waits", "Innodb_row_lock_time", "Innodb_row_lock_time_avg",
    "Created_tmp_disk_tables", "Created_tmp_tables", "Open_files", "Open_tables",
    "Aborted_connects", "Aborted_clients",
]
ADDED_COLUMNS = (
    "Uptime Threads_created Connections Max_used_connections Com_commit Com_rollback "
    "Select_full_join Select_scan Sort_merge_passes Table_locks_waited "
    "Innodb_buffer_pool_wait_free Innodb_buffer_pool_pages_total Innodb_buffer_pool_pages_dirty "
    "Innodb_buffer_pool_pages_free Innodb_buffer_pool_pages_flushed "
    "Innodb_data_reads Innodb_data_writes Innodb_data_read Innodb_data_written Innodb_data_fsyncs "
    "Innodb_os_log_written Innodb_os_log_fsyncs Innodb_log_waits "
    "Innodb_row_lock_current_waits Innodb_rows_read Innodb_rows_inserted Innodb_rows_updated Innodb_rows_deleted "
    "Innodb_checkpoint_age Innodb_checkpoint_max_age Innodb_history_list_length "
    "Bytes_received Bytes_sent"
).split()
REPLICA_COLUMNS = ["history_list_length", "replica_lag_s", "replica_io_running", "replica_sql_running"]
ALL_COLUMNS = ["ts"] + EARLIER_COLUMNS + ADDED_COLUMNS + REPLICA_COLUMNS

# label -> (VERSION() text, db_engine, server_version_num, statement the replica check must use)
VERSIONS = {
    "MySQL 5.5": ("5.5.62-log", "mysql", "50562", "SLAVE"),
    "MySQL 8.0.21": ("8.0.21", "mysql", "80021", "SLAVE"),
    "MySQL 8.0.22": ("8.0.22", "mysql", "80022", "REPLICA"),
    "MySQL 8.4": ("8.4.0", "mysql", "80400", "REPLICA"),
    "MariaDB 10.4": ("10.4.32-MariaDB", "mariadb", "100432", "SLAVE"),
    "MariaDB 11.4": ("11.4.2-MariaDB-ubu2404", "mariadb", "110402", "REPLICA"),
}


def sample_calls(run):
    """The client calls that take a sample (the ones asking for SHOW GLOBAL STATUS;)."""
    return [call for call in run.client_calls() if "SHOW GLOBAL STATUS; " in call]


def replica_calls(run):
    """The SHOW REPLICA/SLAVE STATUS calls (not the MariaDB multi-source check)."""
    return [call for call in run.client_calls() if "STATUS\\G" in call and "ALL SLAVES" not in call]


def row_values(run, name="mysql_stats.csv", row=1):
    """One data row of a collector CSV as a dict keyed by column name."""
    rows = dbsupport.csv_rows(run.text(name))
    return dict(zip(rows[0], rows[row]))


class MysqlFakeClientTests(CollectorTestCase):
    def reset_output(self):
        """Forget the previous run so a test can run the collector again."""
        shutil.rmtree(os.path.join(self.tmp, "out"), ignore_errors=True)
        log_path = os.path.join(self.tmp, "fake.log")
        if os.path.exists(log_path):
            os.remove(log_path)

    def test_every_version_writes_matching_headers_and_rows(self):
        for label, (version, engine, number, _statement) in VERSIONS.items():
            with self.subTest(version=label):
                run = self.run_collector(SCRIPT, fake_env={"FAKE_MYSQL_VERSION": version})
                self.assertEqual(run.process.returncode, 0, run.process.stderr)
                stats = header_matches_rows(self, run, "mysql_stats.csv")
                self.assertEqual(len(stats), 3, "header plus two samples")
                self.assertEqual(len(stats[0]), len(ALL_COLUMNS))
                header_matches_rows(self, run, "mysql_file_io.csv")
                self.assertEqual(run.text("errors.log"), "", label)
                self.assertEqual(run.manifest()["db_engine"], engine, label)
                self.assertEqual(run.manifest()["server_version_num"], number, label)
                self.reset_output()

    def test_header_keeps_the_earlier_columns_first(self):
        run = self.run_collector(SCRIPT, count=1)
        header = dbsupport.csv_rows(run.text("mysql_stats.csv"))[0]
        self.assertEqual(header, ALL_COLUMNS)
        self.assertEqual(header[:20], ["ts"] + EARLIER_COLUMNS)
        self.assertEqual(header[-4:], REPLICA_COLUMNS)

    def test_values_land_in_the_column_of_the_same_name(self):
        # The fake reports variables in a different order from the CSV, with the
        # position as the value; the row must still follow the header order.
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "10.11.6-MariaDB"})
        values = row_values(run)
        self.assertEqual(values["Questions"], "40")
        self.assertEqual(values["Threads_connected"], "46")
        self.assertEqual(values["Bytes_sent"], "4")
        self.assertEqual(values["history_list_length"], "25")

    def test_unreported_variables_are_empty_fields(self):
        run = self.run_collector(SCRIPT, count=1)
        values = row_values(run)
        for name in ("Innodb_checkpoint_age", "Innodb_checkpoint_max_age", "Innodb_history_list_length"):
            self.assertEqual(values[name], "", name)
        self.assertNotEqual(values["Questions"], "")
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_OMIT": "Questions Bytes_sent Uptime"})
        header_matches_rows(self, run, "mysql_stats.csv")
        values = row_values(run)
        self.assertEqual((values["Questions"], values["Bytes_sent"], values["Uptime"]), ("", "", ""))
        self.assertEqual(run.text("errors.log"), "")

    def test_one_sample_is_one_client_call_for_a_server_that_is_not_a_replica(self):
        run = self.run_collector(SCRIPT, count=3)
        self.assertEqual(len(sample_calls(run)), 3)
        self.assertEqual(len(replica_calls(run)), 1, "only the check at the start")

    def test_sample_sql_reads_status_history_and_file_io(self):
        run = self.run_collector(SCRIPT, count=1)
        call = sample_calls(run)[0]
        self.assertIn("SHOW GLOBAL STATUS;", call)
        self.assertIn("SELECT 'H', COUNT FROM information_schema.INNODB_METRICS", call)
        self.assertIn("SELECT 'P'", call)
        self.assertIn("performance_schema.file_summary_by_event_name", call)

    def test_client_arguments(self):
        run = self.run_collector(SCRIPT, count=1)
        first = run.client_calls()[0]
        for argument in ("--batch", "--skip-column-names", "--connect-timeout=5", "-u root", "-h localhost"):
            self.assertIn(argument, first)
        self.assertNotIn("--protocol", first)
        # A bare localhost gets no -P: the MariaDB client would switch to TCP.
        self.assertNotIn(" -P ", first)

    def test_remote_host_gets_host_and_port(self):
        run = self.run_collector(SCRIPT, count=1, args=["--host", "db1.invalid", "--port", "3307"])
        first = run.client_calls()[0]
        self.assertIn("-h db1.invalid -P 3307", first)
        self.assertNotIn("--protocol", first)
        self.assertEqual(run.manifest()["db_instance"], "db1.invalid:3307")

    def test_localhost_with_a_port_uses_tcp(self):
        run = self.run_collector(SCRIPT, count=1, args=["--host", "localhost", "--port", "3307"])
        for call in run.client_calls():
            if "SHOW GLOBAL STATUS" in call or "SELECT 1" in call:
                self.assertIn("-h localhost -P 3307 --protocol=TCP", call)
        self.assertIn("connecting over TCP", run.manifest()["notes"])
        self.assertIn("NOTE: --host localhost with --port 3307", run.process.stderr)
        self.assertEqual(run.manifest()["db_instance"], "localhost:3307")

    def test_port_alone_with_the_default_host_also_uses_tcp(self):
        run = self.run_collector(SCRIPT, count=1, args=["--port", "3307"])
        self.assertIn("--protocol=TCP", run.client_calls()[0])

    def test_socket_is_passed_through(self):
        run = self.run_collector(SCRIPT, count=1, args=["--socket", "/run/mysqld/other.sock"])
        for call in run.client_calls():
            if call.startswith("client=") and "--version" not in call:
                self.assertIn("--socket /run/mysqld/other.sock", call)
                self.assertNotIn(" -h ", call)
                self.assertNotIn(" -P ", call)
        manifest = run.manifest()
        self.assertEqual(manifest["db_instance"], "/run/mysqld/other.sock")
        self.assertEqual(manifest["target_local"], "yes")
        self.assertEqual(manifest["target_local_basis"], "socket")
        self.assertEqual(manifest["target"], "root@/run/mysqld/other.sock")

    def test_socket_cannot_be_combined_with_host_or_port(self):
        for extra in (["--host", "db1"], ["--port", "3307"]):
            with self.subTest(extra=extra):
                run = self.run_collector(SCRIPT, count=1, args=["--socket", "/run/x.sock"] + extra)
                self.assertEqual(run.process.returncode, 2)
                self.assertIn("--socket cannot be combined with --host or --port", run.process.stderr)

    def test_mysql_client_is_used_when_mysql_is_not_mariadb(self):
        run = self.run_collector(SCRIPT, count=1)
        names = {call.split()[0] for call in run.client_calls()}
        self.assertEqual(names, {"client=mysql"})

    def make_mysql_symlink_to_mariadb(self):
        """A directory with mysql -> mariadb -> the fake MariaDB client, as on a MariaDB 11 host."""
        directory = os.path.join(self.tmp, "mariadb-bin")
        os.makedirs(directory)
        os.symlink(os.path.join(dbsupport.FAKE_DIR, "mariadb"), os.path.join(directory, "mariadb"))
        os.symlink("mariadb", os.path.join(directory, "mysql"))
        return directory

    def test_mysql_symlinked_to_mariadb_runs_mariadb_and_errors_log_stays_empty(self):
        directory = self.make_mysql_symlink_to_mariadb()
        path = directory + os.pathsep + dbsupport.FAKE_DIR + os.pathsep + os.environ["PATH"]
        run = self.run_collector(SCRIPT, fake_env={"PATH": path, "FAKE_MYSQL_VERSION": "11.4.2-MariaDB"})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        names = {call.split()[0] for call in run.client_calls()}
        self.assertEqual(names, {"client=mariadb"})
        self.assertEqual(run.text("errors.log"), "")
        self.assertNotIn("Deprecated program name", run.process.stderr)
        self.assertEqual(len(run.lines("mysql_stats.csv")), 3)

    def test_the_fake_mariadb_client_warns_when_it_is_started_as_mysql(self):
        # Guards the test above: the deprecation line really is produced under the name mysql.
        directory = self.make_mysql_symlink_to_mariadb()
        result = subprocess.run(
            [os.path.join(directory, "mysql"), "-e", "SELECT 1"], capture_output=True, text=True,
            env={"PATH": os.environ["PATH"]},
        )
        self.assertIn("Deprecated program name", result.stderr)

    def choose_client(self, bin_directory):
        script_text = read_text(dbsupport.script_path(SCRIPT))
        function = re.search(r"^choose_client\(\) \{\n.*?^\}\n", script_text, re.S | re.M).group(0)
        env = {"PATH": bin_directory}
        result = subprocess.run([shutil.which("bash"), "-c", function + "\nchoose_client\n"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_choose_client(self):
        readlink = shutil.which("readlink")
        fake_mysql = os.path.join(dbsupport.FAKE_DIR, "mysql")
        fake_mariadb = os.path.join(dbsupport.FAKE_DIR, "mariadb")
        cases = {
            "only mysql": ({"mysql": fake_mysql}, "mysql"),
            "only mariadb": ({"mariadb": fake_mariadb}, "mariadb"),
            "both, separate programs": ({"mysql": fake_mysql, "mariadb": fake_mariadb}, "mysql"),
            "neither": ({}, ""),
        }
        for label, (links, expected) in cases.items():
            with self.subTest(case=label):
                directory = tempfile.mkdtemp(prefix="choose-client-", dir=self.tmp)
                os.symlink(readlink, os.path.join(directory, "readlink"))
                for name, target in links.items():
                    os.symlink(target, os.path.join(directory, name))
                self.assertEqual(self.choose_client(directory), expected)
        with self.subTest(case="mysql is a symlink to mariadb"):
            directory = tempfile.mkdtemp(prefix="choose-client-", dir=self.tmp)
            os.symlink(readlink, os.path.join(directory, "readlink"))
            os.symlink(fake_mariadb, os.path.join(directory, "mariadb"))
            os.symlink("mariadb", os.path.join(directory, "mysql"))
            self.assertEqual(self.choose_client(directory), "mariadb")

    def test_missing_client_is_reported(self):
        # An empty directory first on PATH does not hide a real mysql, so run the
        # script with a PATH that only has the tools it needs before the client check.
        directory = os.path.join(self.tmp, "no-client-bin")
        os.makedirs(directory)
        for tool in ("bash", "env", "basename", "readlink", "cat", "date", "uname", "mktemp", "rm", "grep", "awk", "nproc", "hostname", "mkdir", "sleep", "sort", "timeout", "tr", "id", "ip", "getent"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, os.path.join(directory, tool))
        script = dbsupport.script_path(SCRIPT)
        result = subprocess.run(
            [os.path.join(directory, "bash"), script, "-c", "1", "-i", "1", "--foreground", "-o", os.path.join(self.tmp, "out")],
            capture_output=True, text=True, env={"PATH": directory, "HOME": self.home}, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no MySQL/MariaDB client ('mysql' or 'mariadb') found", result.stderr)

    def test_connection_failure_is_reported(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "connect"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("cannot connect to MySQL/MariaDB", run.process.stderr)
        self.assertIn("run this script with sudo", run.process.stderr)

    def test_replica_statement_follows_the_version_gate(self):
        for label, (version, _engine, _number, statement) in VERSIONS.items():
            with self.subTest(version=label):
                run = self.run_collector(SCRIPT, count=2, fake_env={"FAKE_MYSQL_VERSION": version, "FAKE_MYSQL_REPLICA": "1"})
                self.assertEqual(run.text("errors.log"), "", label)
                calls = replica_calls(run)
                self.assertEqual(len(calls), 3, "one at the start, one per sample")
                for call in calls:
                    self.assertIn("SHOW %s STATUS\\G" % statement, call)
                    self.assertIn("--column-names", call)
                values = row_values(run)
                self.assertEqual(values["replica_lag_s"], "7", label)
                self.assertEqual(values["replica_io_running"], "1", label)
                self.assertEqual(values["replica_sql_running"], "1", label)
                header_matches_rows(self, run, "mysql_stats.csv")
                self.reset_output()

    def test_replica_columns_are_empty_for_a_server_that_is_not_a_replica(self):
        run = self.run_collector(SCRIPT, count=1)
        values = row_values(run)
        self.assertEqual([values[name] for name in REPLICA_COLUMNS[1:]], ["", "", ""])

    def test_replica_lag_is_the_largest_across_channels(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_CHANNELS": "3", "FAKE_MYSQL_LAG": "4"})
        self.assertEqual(row_values(run)["replica_lag_s"], "14")

    def test_replica_with_a_stopped_io_thread(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_IO_RUNNING": "Connecting"})
        values = row_values(run)
        self.assertEqual(values["replica_io_running"], "0")
        self.assertEqual(values["replica_sql_running"], "1")

    def test_replica_lag_null_is_an_empty_field(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_LAG": "NULL"})
        values = row_values(run)
        self.assertEqual(values["replica_lag_s"], "")
        self.assertEqual(values["replica_sql_running"], "1")

    def test_replica_status_without_the_privilege_adds_a_note(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_NO_REPLICA_PRIVILEGE": "1"})
        self.assertIn("SHOW REPLICA STATUS failed for this login (grant REPLICATION CLIENT)", run.manifest()["notes"])
        self.assertEqual(row_values(run)["replica_lag_s"], "")
        self.assertEqual(len(replica_calls(run)), 1, "no per-sample calls after the failed check")

    def test_mariadb_replica_privilege_is_named_for_10_5_9_and_later(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={
            "FAKE_MYSQL_VERSION": "10.11.6-MariaDB", "FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_NO_REPLICA_PRIVILEGE": "1",
        })
        self.assertIn("grant REPLICA MONITOR", run.manifest()["notes"])

    def test_mariadb_multi_source_adds_a_note(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={
            "FAKE_MYSQL_VERSION": "10.11.6-MariaDB", "FAKE_MYSQL_REPLICA": "1", "FAKE_MYSQL_CHANNELS": "2",
        })
        self.assertIn("MariaDB multi-source replication (2 connections)", run.manifest()["notes"])
        self.assertEqual(row_values(run)["replica_lag_s"], "7", "default connection only")

    def test_mysql_5_5_has_no_history_query(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "5.5.62-log"})
        self.assertNotIn("INNODB_METRICS", sample_calls(run)[0])
        self.assertEqual(row_values(run)["history_list_length"], "")
        self.assertIn("no information_schema.INNODB_METRICS", run.manifest()["notes"])
        self.assertEqual(run.text("errors.log"), "")

    def test_history_query_needs_the_process_privilege(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_NO_PROCESS": "1"})
        self.assertNotIn("INNODB_METRICS", sample_calls(run)[0])
        self.assertEqual(row_values(run)["history_list_length"], "")
        self.assertIn("INNODB_METRICS is not readable for this login (grant PROCESS)", run.manifest()["notes"])
        self.assertEqual(run.text("errors.log"), "")

    def test_performance_schema_off_means_no_file_io_file(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_PERFSCHEMA": "OFF"})
        self.assertFalse(run.exists("mysql_file_io.csv"))
        self.assertNotIn("performance_schema.", sample_calls(run)[0])
        self.assertEqual(run.manifest()["files"], "mysql_stats.csv")
        self.assertIn("performance_schema is off", run.manifest()["notes"])
        self.assertEqual(run.text("errors.log"), "")

    def test_file_io_needs_select_on_performance_schema(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_NO_PERFSCHEMA_SELECT": "1"})
        self.assertFalse(run.exists("mysql_file_io.csv"))
        self.assertIn("grant SELECT on performance_schema", run.manifest()["notes"])

    def test_file_io_rows(self):
        run = self.run_collector(SCRIPT, count=2)
        rows = header_matches_rows(self, run, "mysql_file_io.csv")
        self.assertEqual(rows[0], ["ts", "event_name", "count_read", "sum_timer_read_ps", "count_write", "sum_timer_write_ps", "count_misc", "sum_timer_misc_ps"])
        self.assertEqual(len(rows), 1 + 2 * 2, "two events per sample")
        self.assertEqual(rows[1][1], "wait/io/file/innodb/innodb_data_file")
        self.assertEqual(rows[1][2:], ["10", "1000", "5", "500", "2", "200"])

    def test_bad_rows_go_to_the_error_log_not_the_csv(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MYSQL_BAD_ROW": "wrong_count"})
        self.assertIn("P row has 3 fields, expected 7", run.text("errors.log"))
        self.assertEqual(len(run.lines("mysql_stats.csv")), 3)
        self.assertEqual(len(run.lines("mysql_file_io.csv")), 1 + 2 * 2, "the bad row is not in the CSV")
        self.reset_output()
        run = self.run_collector(SCRIPT, fake_env={"FAKE_MYSQL_BAD_ROW": "unexpected"})
        self.assertIn("unexpected output: Warning,1287,something is deprecated", run.text("errors.log"))
        self.assertEqual(len(run.lines("mysql_stats.csv")), 3)

    def test_event_name_with_a_comma_is_one_field(self):
        # Quoted by the SQL, so route_sample_output counts it as one field.
        run = self.run_collector(SCRIPT, count=1)
        rows = dbsupport.csv_rows(run.text("mysql_file_io.csv"))
        self.assertTrue(all(len(row) == 8 for row in rows))

    def test_a_hung_client_is_cut_off_and_the_schedule_holds(self):
        started = time.time()
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "timeout"}, count=3, interval=1)
        elapsed = time.time() - started
        self.assertEqual(run.text("errors.log").count("sample timed out after 1 s"), 3)
        self.assertLess(elapsed, 10, "three 1 s samples must not wait for the 30 s fake")
        self.assertEqual(len(run.lines("mysql_stats.csv")), 1, "header only")

    def test_a_failed_sample_leaves_no_row(self):
        run = self.run_collector(SCRIPT, fake_env={"FAKE_FAIL": "sample_error"})
        self.assertIn("client exited with status 1", run.text("errors.log"))
        self.assertIn("Lock wait timeout exceeded", run.text("errors.log"))
        self.assertEqual(len(run.lines("mysql_stats.csv")), 1, "header only")

    def test_manifest_keys(self):
        run = self.run_collector(SCRIPT, args=["--user", "monitor"])
        manifest = run.manifest()
        self.assertEqual(manifest["script_version"], "3.0.0")
        self.assertEqual(manifest["collector_schema"], "3")
        self.assertEqual(manifest["db_engine"], "mysql")
        self.assertEqual(manifest["db_instance"], "localhost:3306")
        self.assertEqual(manifest["server_version"], "8.0.36")
        self.assertEqual(manifest["server_version_num"], "80036")
        self.assertEqual(manifest["server_hostname"], "fakedb-host-01")
        self.assertEqual(manifest["server_start"], "2026-10-20 08:00:00")
        self.assertEqual(manifest["files"], "mysql_stats.csv mysql_file_io.csv")
        self.assertEqual(manifest["query_timeout_s"], "1")
        self.assertEqual(manifest["target"], "monitor@localhost:3306")
        self.assertEqual(manifest["target_local"], "yes")
        self.assertEqual(manifest["target_local_basis"], "loopback")
        self.assertEqual(manifest["setting.innodb_buffer_pool_size"], "134217728")
        self.assertEqual(manifest["setting.max_connections"], "151")
        self.assertEqual(manifest["setting.performance_schema"], "ON")
        self.assertEqual(manifest["setting.version_comment"], "MySQL Community Server - GPL")
        self.assertIn("end", manifest)
        self.assertEqual(manifest["notes"], "")

    def test_mariadb_is_recorded_as_mariadb_but_the_run_tag_stays_mysql(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "10.11.6-MariaDB-log", "FAKE_MYSQL_COMMENT": "mariadb.org binary distribution"})
        self.assertEqual(run.manifest()["db_engine"], "mariadb")
        self.assertIn("_mysql_", os.path.basename(run.run_dir))
        self.assertEqual(run.manifest()["setting.version_comment"], "mariadb.org binary distribution")

    def test_mariadb_replication_prefix_in_the_version_is_ignored(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "5.5.5-10.4.32-MariaDB"})
        self.assertEqual(run.manifest()["db_engine"], "mariadb")
        self.assertEqual(run.manifest()["server_version_num"], "100432")

    def test_mysql_5_5_5_is_not_mariadb(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "5.5.5-log"})
        self.assertEqual(run.manifest()["db_engine"], "mysql")
        self.assertEqual(run.manifest()["server_version_num"], "50505")

    def test_a_setting_the_server_does_not_have_is_not_recorded(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "8.0.36"})
        self.assertEqual(run.manifest()["setting.innodb_redo_log_capacity"], "104857600")
        self.reset_output()
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "10.4.32-MariaDB"})
        self.assertNotIn("setting.innodb_redo_log_capacity", run.manifest())
        self.assertNotIn("setting.innodb_data_home_dir", run.manifest(), "an empty value is not recorded")

    def test_no_password_in_the_manifest_or_logs(self):
        run = self.run_collector(SCRIPT, args=["--user", "monitor"], fake_env={"MYSQL_PWD": "s3cret-value"})
        self.assertNotIn("s3cret-value", run.text("manifest.txt"))
        self.assertNotIn("s3cret-value", run.text("errors.log"))
        self.assertNotIn("s3cret-value", run.process.stdout + run.process.stderr)

    # Locality ----------------------------------------------------------------

    def test_locality_socket(self):
        run = self.run_collector(SCRIPT, count=1, args=["--socket", "/run/mysqld/mysqld.sock"])
        self.assertEqual(run.manifest()["target_local"], "yes")
        self.assertEqual(run.manifest()["target_local_basis"], "socket")

    def test_locality_loopback(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            with self.subTest(host=host):
                run = self.run_collector(SCRIPT, count=1, args=["--host", host])
                self.assertEqual(run.manifest()["target_local"], "yes")
                self.assertEqual(run.manifest()["target_local_basis"], "loopback")
                self.reset_output()

    def test_locality_server_hostname(self):
        own_name = socket.gethostname().split(".")[0]
        run = self.run_collector(SCRIPT, count=1, args=["--host", "db1.invalid"], fake_env={"FAKE_MYSQL_HOSTNAME": own_name})
        self.assertEqual(run.manifest()["target_local"], "yes")
        self.assertEqual(run.manifest()["target_local_basis"], "server hostname")
        self.assertEqual(run.manifest()["server_hostname"], own_name)

    def test_locality_remote(self):
        run = self.run_collector(SCRIPT, count=1, args=["--host", "db1.invalid"])
        self.assertEqual(run.manifest()["target_local"], "no")
        self.assertEqual(run.manifest()["target_local_basis"], "none")
        self.assertFalse(run.exists("db_paths.txt"))

    def test_locality_unknown_when_the_server_reports_no_host_name(self):
        run = self.run_collector(SCRIPT, count=1, args=["--host", "db1.invalid"], fake_env={"FAKE_MYSQL_HOSTNAME": ""})
        self.assertEqual(run.manifest()["target_local"], "unknown")
        self.assertFalse(run.exists("db_paths.txt"))

    # Paths -------------------------------------------------------------------

    def test_paths_for_a_local_server(self):
        run = self.run_collector(SCRIPT, count=1)
        self.assertEqual(run.lines("db_paths.txt"), [
            "path.data.1=/var/lib/mysql",
            "path.redo.1=/var/lib/mysql",
            "path.redo.2=/var/lib/mysql/#innodb_redo",
            "path.temp.1=/tmp",
        ])

    def test_redo_directory_below_the_log_directory_from_8_0_30(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "8.0.36"})
        # The fake reports 8.0.36, so this is the 8.0.30+ case.
        self.assertIn("path.redo.2=/var/lib/mysql/#innodb_redo", run.lines("db_paths.txt"))
        self.reset_output()
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "8.0.29"})
        self.assertNotIn("path.redo.2=/var/lib/mysql/#innodb_redo", run.lines("db_paths.txt"))
        self.reset_output()
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "11.4.2-MariaDB"})
        self.assertEqual([line for line in run.lines("db_paths.txt") if "redo" in line], ["path.redo.1=/var/lib/mysql"])

    def test_relative_and_absolute_redo_directories(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "8.0.20", "FAKE_MYSQL_REDO_DIR": "redo/"})
        self.assertIn("path.redo.1=/var/lib/mysql/redo", run.lines("db_paths.txt"))
        self.reset_output()
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_VERSION": "8.0.20", "FAKE_MYSQL_REDO_DIR": "/fast/redo/"})
        self.assertIn("path.redo.1=/fast/redo", run.lines("db_paths.txt"))

    def test_every_temp_directory_is_listed(self):
        run = self.run_collector(SCRIPT, count=1, fake_env={"FAKE_MYSQL_TMPDIR": "/tmp:/var/tmp/", "FAKE_MYSQL_INNODB_TMPDIR": "/fast/tmp"})
        paths = run.lines("db_paths.txt")
        self.assertIn("path.temp.1=/tmp", paths)
        self.assertIn("path.temp.2=/var/tmp", paths)
        self.assertIn("path.temp.3=/fast/tmp", paths)

    def test_no_db_paths_for_a_remote_server(self):
        run = self.run_collector(SCRIPT, count=1, args=["--host", "db1.invalid"])
        self.assertFalse(run.exists("db_paths.txt"))
        self.assertNotIn("db_paths", run.manifest()["files"])

    # Help and options --------------------------------------------------------

    def test_help_names_socket_privileges_and_the_sudo_path(self):
        result = subprocess.run([dbsupport.script_path(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for text in ("--socket", "GRANT PROCESS, REPLICATION CLIENT", "sudo ./collect-mysql-metrics.sh", "unix_socket", "Debian and Ubuntu"):
            self.assertIn(text, result.stdout)

    def test_version_option(self):
        result = subprocess.run([dbsupport.script_path(SCRIPT), "-V"], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "collect-mysql-metrics.sh 3.0.0")

    def test_invalid_port_is_refused(self):
        run = self.run_collector(SCRIPT, count=1, args=["--port", "70000"])
        self.assertEqual(run.process.returncode, 2)


def live_server_reachable():
    client = shutil.which("mariadb") or shutil.which("mysql")
    if not client:
        return False
    try:
        result = subprocess.run(
            [client, "--batch", "--skip-column-names", "--connect-timeout=3", "-e", "SELECT 1"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


@unittest.skipUnless(live_server_reachable(), "no MySQL or MariaDB server reachable with the default client login")
class MysqlLiveServerTests(CollectorTestCase):
    def run_live(self, count=3, interval=1, extra_path=None, args=None):
        script = dbsupport.script_path(SCRIPT)
        base = os.path.join(self.tmp, "out")
        env = dict(os.environ, HOME=self.home)
        if extra_path:
            env["PATH"] = extra_path + os.pathsep + env["PATH"]
        # Use the login the reachability check used: the client's default is the OS user.
        user = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        process = subprocess.run(
            [script, "-c", str(count), "-i", str(interval), "--foreground", "-o", base, "--user", user] + list(args or []),
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
        )
        return dbsupport.CollectorRun(process, base, os.path.join(self.tmp, "none.log"))

    def test_samples_parse_and_errors_log_is_empty(self):
        run = self.run_live()
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertEqual(run.text("errors.log"), "")
        rows = header_matches_rows(self, run, "mysql_stats.csv")
        self.assertEqual(rows[0], ALL_COLUMNS)
        self.assertEqual(len(rows), 4)

    def test_values_are_numbers_and_counters_do_not_go_backwards(self):
        run = self.run_live()
        rows = dbsupport.csv_rows(run.text("mysql_stats.csv"))
        for row in rows[1:]:
            for name, value in zip(rows[0][1:], row[1:]):
                self.assertTrue(value == "" or re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", value), "%s=%r" % (name, value))
        questions = [int(row[rows[0].index("Questions")]) for row in rows[1:]]
        self.assertEqual(questions, sorted(questions))
        uptime = [int(row[rows[0].index("Uptime")]) for row in rows[1:]]
        self.assertEqual(uptime, sorted(uptime))
        self.assertGreaterEqual(uptime[-1] - uptime[0], 1)

    def test_counters_move_under_load(self):
        client = shutil.which("mariadb") or shutil.which("mysql")
        load = subprocess.Popen(
            ["bash", "-c", 'for i in $(seq 1 30); do "$0" -e "SELECT 1" >/dev/null 2>&1; sleep 0.1; done', client],
        )
        self.addCleanup(load.wait)
        run = self.run_live(count=4, interval=1)
        rows = dbsupport.csv_rows(run.text("mysql_stats.csv"))
        questions = [int(row[rows[0].index("Questions")]) for row in rows[1:]]
        self.assertGreater(questions[-1] - questions[0], 5)

    def test_manifest_and_paths(self):
        run = self.run_live(count=1)
        manifest = run.manifest()
        self.assertEqual(manifest["collector_schema"], "3")
        self.assertIn(manifest["db_engine"], ("mysql", "mariadb"))
        self.assertGreater(int(manifest["server_version_num"]), 50000)
        self.assertEqual(manifest["target_local"], "yes")
        self.assertTrue(manifest["server_hostname"])
        self.assertTrue(manifest["setting.datadir"])
        paths = dict(line.split("=", 1) for line in run.lines("db_paths.txt"))
        self.assertEqual(paths["path.data.1"], manifest["setting.datadir"].rstrip("/") or "/")
        self.assertIn("path.redo.1", paths)
        self.assertIn("path.temp.1", paths)
        self.assertIn("mysql_stats.csv", manifest["files"])

    def test_socket_option_against_the_live_server(self):
        client = shutil.which("mariadb") or shutil.which("mysql")
        result = subprocess.run([client, "--batch", "--skip-column-names", "-e", "SELECT @@socket"], capture_output=True, text=True)
        socket_path = result.stdout.strip()
        if not socket_path or not os.path.exists(socket_path):
            self.skipTest("the server has no Unix socket path to pass")
        run = self.run_live(count=1, args=["--socket", socket_path])
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertEqual(run.text("errors.log"), "")
        self.assertEqual(run.manifest()["db_instance"], socket_path)

    def test_mariadb_client_is_used_when_mysql_is_a_symlink_to_it(self):
        mysql_path = shutil.which("mysql")
        if not mysql_path or os.path.basename(os.path.realpath(mysql_path)) != "mariadb":
            self.skipTest("mysql is not a symlink to mariadb on this host")
        shim_directory = os.path.join(self.tmp, "shims")
        os.makedirs(shim_directory)
        log_path = os.path.join(self.tmp, "invoked.log")
        real_mariadb = os.path.realpath(mysql_path)
        # Both names log how they were started, then run the real client. The
        # shim for mysql is a symlink to the shim for mariadb, as on the host.
        write_text(os.path.join(shim_directory, "mariadb"), '#!/usr/bin/env bash\necho "$(basename "$0")" >> "%s"\nexec "%s" "$@"\n' % (log_path, real_mariadb))
        os.chmod(os.path.join(shim_directory, "mariadb"), 0o755)
        os.symlink("mariadb", os.path.join(shim_directory, "mysql"))
        run = self.run_live(count=2, extra_path=shim_directory)
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertEqual(run.text("errors.log"), "")
        invoked = set(read_text(log_path).split())
        self.assertEqual(invoked, {"mariadb"})


if __name__ == "__main__":
    unittest.main()
