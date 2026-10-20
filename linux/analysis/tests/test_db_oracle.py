"""collect-oracle-metrics.sh 3.0.0 against a fake sqlplus.

There is no Oracle server in the test environment, so the SQL is checked as
text and the output handling is checked with canned rows. What the SQL does on
a real server is listed in docs/specs/db-collectors-v3.md section 16.
"""

import os
import re
import shutil
import subprocess
import time

import dbsupport
from dbsupport import CollectorTestCase, header_matches_rows
from helpers import read_text

SCRIPT = "collect-oracle-metrics.sh"
PASSWORD = "s3cret-pw"
CONNECT = "monitor/%s@//localhost:1521/ORCLPDB1" % PASSWORD

# Views the collector may read (licensing statement, spec section 5.4).
ALLOWED_VIEWS = {
    "v$sysstat", "v$sys_time_model", "v$system_event", "v$system_wait_class", "v$session",
    "v$resource_limit", "v$instance", "v$database", "v$version", "v$parameter", "v$datafile",
    "v$tempfile", "v$logfile", "v$asm_disk_stat",
}


def short_host_name():
    return os.uname().nodename.split(".")[0]


class OracleFakeClientTests(CollectorTestCase):
    def run_oracle(self, args=None, fake_env=None, connect=CONNECT, **kwargs):
        """Runs the collector with the connect string in the environment, as the doc recommends."""
        env = {"ORACLE_CONNECT_STRING": connect}
        env.update(fake_env or {})
        return self.run_collector(SCRIPT, args=args, fake_env=env, **kwargs)

    def fresh_run(self, **kwargs):
        """Same, after removing the output and log of an earlier run in this test."""
        shutil.rmtree(os.path.join(self.tmp, "out"), ignore_errors=True)
        log_path = os.path.join(self.tmp, "fake.log")
        if os.path.exists(log_path):
            os.remove(log_path)
        return self.run_oracle(**kwargs)

    def logged_sql(self, run):
        return "\n".join(call for call in run.client_calls() if call.startswith("stdin:"))

    # ---- output shape -------------------------------------------------

    def test_sqlplus_11_2_and_19_write_matching_headers_and_rows(self):
        for release in ("11.2", "19"):
            with self.subTest(release=release):
                run = self.fresh_run(fake_env={"FAKE_SQLPLUS_RELEASE": release})
                self.assertEqual(run.process.returncode, 0, run.process.stderr)
                stats = header_matches_rows(self, run, "oracle_stats.csv")
                self.assertEqual(len(stats), 3, "header plus two samples")
                self.assertEqual(len(stats[0]), 31)
                waits = header_matches_rows(self, run, "oracle_waits.csv")
                self.assertEqual(len(waits), 11, "header plus five rows for each of two samples")
                header_matches_rows(self, run, "oracle_active.csv")
                self.assertEqual(run.text("errors.log"), "")
                self.assertEqual(run.manifest()["notes"], "", "no setup query failed on this release")

    def test_stats_columns_keep_the_old_names_first(self):
        run = self.run_oracle()
        header = run.lines("oracle_stats.csv")[0].split(",")
        self.assertEqual(header[:11], [
            "ts", "physical_reads", "physical_writes", "session_logical_reads", "redo_size", "user_calls",
            "db_block_gets", "consistent_gets", "parse_count_total", "parse_count_hard", "active_sessions",
        ])
        for name in (
            "physical_read_total_io_requests", "physical_write_total_io_requests", "physical_read_total_bytes",
            "physical_write_total_bytes", "user_commits", "user_rollbacks", "execute_count", "sorts_disk",
            "sorts_memory", "redo_log_space_requests", "logons_current", "db_time_us", "db_cpu_us",
            "user_sessions", "user_active_sessions", "processes_current", "processes_limit",
            "sessions_current", "sessions_limit", "server_start_marker",
        ):
            self.assertIn(name, header)
        self.assertEqual(
            run.lines("oracle_waits.csv")[0], "ts,kind,name,total_waits,time_waited_us,total_waits_fg,time_waited_us_fg")
        self.assertEqual(run.lines("oracle_active.csv")[0], "ts,state_class,sessions")

    def test_a_15_digit_counter_arrives_intact(self):
        run = self.run_oracle()
        rows = dbsupport.csv_rows(run.text("oracle_stats.csv"))
        header = rows[0]
        self.assertEqual(rows[1][header.index("physical_reads")], "123456789012345")
        self.assertEqual(rows[1][header.index("redo_size")], "812345678901234")

    def test_unlimited_limit_is_an_empty_field(self):
        run = self.run_oracle()
        rows = dbsupport.csv_rows(run.text("oracle_stats.csv"))
        header = rows[0]
        self.assertEqual(rows[1][header.index("processes_limit")], "")
        self.assertEqual(rows[1][header.index("sessions_limit")], "472")

    def test_quoted_wait_name_with_a_comma_is_one_field(self):
        run = self.run_oracle()
        names = [row[2] for row in dbsupport.csv_rows(run.text("oracle_waits.csv"))]
        self.assertIn("Other, misc", names)

    def test_one_logon_per_sample(self):
        run = self.run_oracle(count=3)
        sample_calls = [call for call in run.client_calls() if call.startswith("stdin:") and "SELECT 'S'" in call]
        self.assertEqual(len(sample_calls), 3)

    # ---- the SQL that is sent ----------------------------------------

    def test_no_markup_csv_and_the_sqlplus_settings_line(self):
        run = self.run_oracle()
        self.assertNotIn("MARKUP", run.text("sample.sql"))
        self.assertNotIn("MARKUP", self.logged_sql(run))
        self.assertIn(
            "SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF TRIMSPOOL ON TRIMOUT ON LINESIZE 32767 TAB OFF",
            run.text("sample.sql"))
        self.assertTrue(run.text("sample.sql").rstrip().endswith("EXIT"))

    def test_both_whenever_lines_are_sent_before_the_logon(self):
        run = self.run_oracle()
        for call in run.client_calls():
            if call.startswith("stdin:"):
                self.assertTrue(call.startswith(
                    "stdin: WHENEVER SQLERROR EXIT FAILURE WHENEVER OSERROR EXIT FAILURE CONNECT <redacted> "), call[:120])

    def test_every_number_goes_through_to_char_tm9(self):
        sql = self.run_oracle().text("sample.sql")
        for line in sql.splitlines():
            if "||" not in line:
                continue
            is_string_column = "REPLACE(" in line or line.strip() == "SELECT 'S'"
            self.assertTrue("'TM9')" in line or is_string_column, line)
        self.assertEqual(sql.count("TO_CHAR("), sql.count("'TM9')"))

    def test_record_letters_and_wait_sql(self):
        sql = self.run_oracle().text("sample.sql")
        for fragment in (
            "SELECT 'S'", "'E,\"class\",\"'", "'E,\"event\",\"'", "SELECT 'A,\"'",
            "FROM v$system_wait_class", "WHERE wait_class <> 'Idle'", "time_waited * 10000", "time_waited_fg * 10000",
            "FROM v$system_event", "time_waited_micro_fg",
            "'db file sequential read'", "'log file sync'", "'enq: TX - row lock contention'",
            "'latch: cache buffers chains'", "'resmgr:cpu quantum'", "'gc cr block busy'",
        ):
            self.assertIn(fragment, sql)

    def test_user_active_sessions_filter(self):
        sql = self.run_oracle().text("sample.sql")
        self.assertIn("NOT (state = 'WAITING' AND wait_class = 'Idle')", sql)
        self.assertIn("sid <> TO_NUMBER(SYS_CONTEXT('USERENV', 'SID'))", sql)
        self.assertIn("CASE WHEN state = 'WAITING' THEN wait_class ELSE 'ON CPU' END", sql)
        self.assertIn("lim.processes_limit = 'UNLIMITED'", sql)

    def test_aliases_fit_the_30_character_limit_of_oracle_11_2(self):
        sql = self.run_oracle().text("sample.sql")
        for alias in re.findall(r"\bAS ([a-z_0-9]+)", sql):
            self.assertLessEqual(len(alias), 30, alias)

    def test_no_blank_line_inside_a_statement(self):
        sql = self.run_oracle().text("sample.sql")
        statements = [part for part in sql.split(";") if part.strip()]
        for statement in statements:
            self.assertNotIn("\n\n", statement.strip(), statement[:80])

    def test_only_unlicensed_views_are_read(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_ASM": "1"})
        sent = (self.logged_sql(run) + run.text("sample.sql")).lower().replace("v\\$", "v$")
        self.assertNotRegex(sent, r"active_session_history|dba_hist|awr")
        used = set(re.findall(r"\bv\$[a-z_]+", sent))
        self.assertTrue(used)
        self.assertLessEqual(used, ALLOWED_VIEWS)

    def test_columns_missing_on_older_releases_are_only_asked_for_on_newer_ones(self):
        run = self.run_oracle(fake_env={"FAKE_SQLPLUS_RELEASE": "11.2"})
        sent = self.logged_sql(run)
        for fragment in ("version_full", "'cdb='", "CON_NAME"):
            self.assertNotIn(fragment, sent)
        run = self.fresh_run(fake_env={"FAKE_ORACLE_VERSION": "12.1.0.2.0"})
        sent = self.logged_sql(run)
        self.assertIn("'cdb='", sent)
        self.assertIn("CON_NAME", sent)
        self.assertNotIn("version_full", sent)
        run = self.fresh_run()
        self.assertIn("version_full", self.logged_sql(run))

    # ---- routing, timeout and failures -------------------------------

    def test_bad_rows_go_to_the_error_log_not_the_csv(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_BAD_ROW": "wrong_count"})
        self.assertIn("S row has 3 fields, expected 30", run.text("errors.log"))
        self.assertEqual(len(run.lines("oracle_stats.csv")), 3)
        run = self.fresh_run(fake_env={"FAKE_ORACLE_BAD_ROW": "unexpected"})
        self.assertIn("unexpected output: ORA-12541: TNS:no listener", run.text("errors.log"))
        self.assertEqual(len(run.lines("oracle_stats.csv")), 3)

    def test_a_hung_client_is_cut_off_and_the_schedule_holds(self):
        started = time.time()
        run = self.run_oracle(fake_env={"FAKE_FAIL": "timeout"}, count=3, interval=1)
        elapsed = time.time() - started
        self.assertEqual(run.text("errors.log").count("sample timed out after 1 s"), 3)
        self.assertLess(elapsed, 12, "three 1 s samples must not wait for the 30 s fake")
        self.assertEqual(len(run.lines("oracle_stats.csv")), 1, "header only")

    def test_connection_failure_is_reported_without_the_password(self):
        run = self.run_oracle(fake_env={"FAKE_FAIL": "connect"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("ORA-01017", run.process.stderr)
        self.assertNotIn(PASSWORD, run.process.stderr + run.process.stdout)

    def test_missing_connect_string_is_refused(self):
        env = {"PATH": dbsupport.FAKE_DIR + os.pathsep + os.environ["PATH"], "HOME": self.home}
        result = subprocess.run(
            [dbsupport.script_path(SCRIPT), "-c", "1", "-i", "1", "--foreground"], capture_output=True, text=True,
            env=env, stdin=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no connect string supplied", result.stderr)

    def test_old_style_count_with_a_connect_string_is_refused_without_echoing_it(self):
        env = {"PATH": dbsupport.FAKE_DIR + os.pathsep + os.environ["PATH"], "HOME": self.home}
        result = subprocess.run(
            [dbsupport.script_path(SCRIPT), "-c", CONNECT, "-i", "1"], capture_output=True, text=True,
            env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(PASSWORD, result.stderr)

    def test_releases_before_11_2_are_refused(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_VERSION": "10.2.0.5.0"})
        self.assertNotEqual(run.process.returncode, 0)
        self.assertIn("Oracle 11.2 or later is needed", run.process.stderr)

    def test_unreadable_database_view_becomes_a_note_and_the_run_continues(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_NO_DATABASE_VIEW": "1"})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertIn("ORA-00942", run.manifest()["notes"])
        self.assertIn("SELECT_CATALOG_ROLE", run.manifest()["notes"])
        self.assertNotIn("setting.log_mode", run.manifest())
        self.assertEqual(run.text("errors.log"), "", "optional setup errors are notes, not sample errors")

    # ---- manifest ----------------------------------------------------

    def test_manifest_keys(self):
        run = self.run_oracle()
        manifest = run.manifest()
        self.assertEqual(manifest["script_version"], "3.0.0")
        self.assertEqual(manifest["collector_schema"], "3")
        self.assertEqual(manifest["db_engine"], "oracle")
        self.assertEqual(manifest["db_instance"], "ORCL/CDB$ROOT")
        self.assertEqual(manifest["server_hostname"], "dbserver.example.com")
        self.assertEqual(manifest["server_start"], "2026-10-20 08:00:00")
        self.assertEqual(manifest["server_version_num"], "192100")
        self.assertEqual(manifest["files"], "oracle_stats.csv oracle_waits.csv oracle_active.csv")
        self.assertEqual(manifest["query_timeout_s"], "1")
        self.assertIn("19c", manifest["server_version"])
        self.assertEqual(manifest["setting.cluster_database"], "FALSE")
        self.assertEqual(manifest["setting.con_name"], "CDB$ROOT")
        self.assertEqual(manifest["setting.cdb"], "YES")
        self.assertEqual(manifest["setting.database_role"], "PRIMARY")
        self.assertEqual(manifest["setting.log_mode"], "ARCHIVELOG")
        self.assertEqual(manifest["setting.cpu_count"], "8")
        self.assertEqual(manifest["setting.db_block_size"], "8192")
        self.assertEqual(manifest["setting.sga_target"], "2147483648")
        self.assertNotIn("setting.resource_manager_plan", manifest, "an empty value is not recorded")
        self.assertNotIn("target", manifest, "the connect string can hold a password")

    def test_version_number_for_11_2_uses_version(self):
        run = self.run_oracle(fake_env={"FAKE_SQLPLUS_RELEASE": "11.2"})
        manifest = run.manifest()
        self.assertEqual(manifest["server_version_num"], "110200")
        self.assertEqual(manifest["db_instance"], "ORCL")
        self.assertNotIn("setting.con_name", manifest)
        self.assertNotIn("setting.cdb", manifest)

    def test_pluggable_database_gets_a_note_and_its_name(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_CON_NAME": "ORCLPDB1"})
        manifest = run.manifest()
        self.assertEqual(manifest["db_instance"], "ORCL/ORCLPDB1")
        self.assertEqual(manifest["setting.con_name"], "ORCLPDB1")
        self.assertIn("connected to pluggable database ORCLPDB1", manifest["notes"])
        self.assertIn("CDB$ROOT", manifest["notes"])

    def test_root_container_has_no_pluggable_database_note(self):
        run = self.run_oracle()
        self.assertNotIn("pluggable", run.manifest()["notes"])

    def test_rac_is_recorded_and_noted(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_RAC": "1"})
        self.assertEqual(run.manifest()["setting.cluster_database"], "TRUE")
        self.assertIn("cluster_database is TRUE", run.manifest()["notes"])

    # ---- secrets -----------------------------------------------------

    def test_password_is_not_in_any_output_or_in_any_argument(self):
        connect_log = os.path.join(self.tmp, "connect.log")
        run = self.run_oracle(fake_env={"FAKE_CONNECT_LOG": connect_log, "FAKE_ORACLE_BAD_ROW": "unexpected"})
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        for name in ("manifest.txt", "errors.log", "sample.sql", "oracle_stats.csv", "oracle_waits.csv", "db_paths.txt"):
            self.assertNotIn(PASSWORD, run.text(name), name)
        self.assertNotIn(PASSWORD, run.process.stdout + run.process.stderr)
        calls = run.client_calls()
        self.assertTrue(calls)
        self.assertNotIn(PASSWORD, "\n".join(calls), "the fake logs its arguments and its redacted input")
        for call in calls:
            if call.startswith("args:"):
                self.assertEqual(call, "args: -s /nolog")
        self.assertIn(CONNECT, read_text(connect_log), "the logon is sent on standard input")

    def test_connect_option_is_redacted_in_the_command_line(self):
        run = self.run_collector(SCRIPT, args=["--connect", CONNECT])
        self.assertEqual(run.process.returncode, 0, run.process.stderr)
        self.assertNotIn(PASSWORD, run.text("manifest.txt"))
        self.assertIn("--connect <redacted>", run.manifest()["command_line"])
        self.assertNotIn(PASSWORD, "\n".join(run.client_calls()))

    # ---- locality ----------------------------------------------------

    def test_locality_from_the_connect_string(self):
        short = short_host_name()
        cases = [
            ("bequeath", "monitor/%s" % PASSWORD, {}, "yes", "socket"),
            ("bequeath as sysdba", "/ as sysdba", {}, "yes", "socket"),
            ("localhost", "monitor/%s@//localhost:1521/svc" % PASSWORD, {}, "yes", "loopback"),
            ("127.0.0.1 as sysdba", "sys/%s@//127.0.0.1:1521/svc as sysdba" % PASSWORD, {}, "yes", "loopback"),
            ("ipv6 loopback", "monitor/%s@//[::1]:1521/svc" % PASSWORD, {}, "yes", "loopback"),
            ("descriptor", "monitor/%s@(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=localhost)(PORT=1521))"
                           "(CONNECT_DATA=(SERVICE_NAME=svc)))" % PASSWORD, {}, "yes", "loopback"),
            ("easy connect without slashes", "monitor/%s@localhost/svc" % PASSWORD, {}, "yes", "loopback"),
            ("server hostname", "monitor/%s@//db1.invalid:1521/svc" % PASSWORD,
             {"FAKE_ORACLE_HOST": short + ".example.com"}, "yes", "server hostname"),
            ("remote", "monitor/%s@//db1.invalid:1521/svc" % PASSWORD, {}, "no", "none"),
            ("remote tcps", "monitor/%s@tcps://db1.invalid:1522/svc" % PASSWORD, {}, "no", "none"),
            ("password with an at sign", 'monitor/"p@ss"@//db1.invalid/svc', {}, "no", "none"),
            ("tns alias, other server", "monitor/%s@PRODDB" % PASSWORD, {}, "unknown", "none"),
            ("tns alias, same server", "monitor/%s@PRODDB" % PASSWORD,
             {"FAKE_ORACLE_HOST": short}, "yes", "server hostname"),
        ]
        for label, connect, fake_env, expected_local, expected_basis in cases:
            with self.subTest(case=label):
                run = self.fresh_run(connect=connect, fake_env=fake_env, count=1)
                self.assertEqual(run.process.returncode, 0, run.process.stderr)
                manifest = run.manifest()
                self.assertEqual(manifest["target_local"], expected_local)
                self.assertEqual(manifest["target_local_basis"], expected_basis)
                self.assertEqual(run.exists("db_paths.txt"), expected_local == "yes")

    # ---- db_paths.txt ------------------------------------------------

    def test_paths_are_directories_of_the_files(self):
        run = self.run_oracle()
        paths = run.lines("db_paths.txt")
        self.assertEqual(paths, [
            "path.data.1=/u01/oradata/ORCL",
            "path.data.2=/u02/oradata/ORCL",
            "path.temp.1=/u01/oradata/ORCL",
            "path.redo.1=/u03/redo/ORCL",
        ])

    def test_no_paths_for_a_remote_server(self):
        run = self.run_oracle(connect="monitor/%s@//db1.invalid:1521/svc" % PASSWORD)
        self.assertFalse(run.exists("db_paths.txt"))
        sent = self.logged_sql(run)
        self.assertNotIn("v$datafile", sent, "no file queries for a remote server")

    def test_asm_groups_and_disks(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_ASM": "1"})
        paths = run.lines("db_paths.txt")
        for expected in (
            "path.data.1=+DATA", "path.temp.1=+DATA", "path.redo.1=+FRA",
            "path.asm_disk.1=/dev/mapper/asm_data1", "path.asm_disk.2=/dev/mapper/asm_fra1",
        ):
            self.assertIn(expected, paths)
        self.assertEqual([line for line in paths if line.startswith("path.data.")], ["path.data.1=+DATA"])
        self.assertNotIn("AFD", run.manifest()["notes"])

    def test_asm_filter_driver_label_is_noted(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_ASM": "1", "FAKE_ORACLE_AFD": "1"})
        paths = run.lines("db_paths.txt")
        self.assertIn("path.asm_disk.2=AFD:FRA1", paths)
        self.assertIn("path.asm_disk.2.note=ASM Filter Driver or ASMLib label; it cannot be mapped to a block device here", paths)
        self.assertIn("AFD or ASMLib labels", run.manifest()["notes"])

    def test_asm_disks_are_not_queried_without_asm_files(self):
        run = self.run_oracle()
        self.assertNotIn("v$asm_disk_stat", self.logged_sql(run))

    def test_unreadable_asm_disk_view_is_noted(self):
        run = self.run_oracle(fake_env={"FAKE_ORACLE_ASM": "1", "FAKE_ORACLE_NO_ASM_VIEW": "1"})
        self.assertIn("ASM disks could not be read", run.manifest()["notes"])
        self.assertIn("path.data.1=+DATA", run.lines("db_paths.txt"))

    # ---- text --------------------------------------------------------

    def test_help_and_header_carry_the_licensing_statement(self):
        result = subprocess.run([dbsupport.script_path(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        for fragment in ("Diagnostics Pack", "v$sysstat", "v$asm_disk_stat", "SELECT_CATALOG_ROLE", "CDB$ROOT",
                         "oracle_waits.csv", "oracle_active.csv", "db_paths.txt"):
            self.assertIn(fragment, result.stdout)
        header = read_text(dbsupport.script_path(SCRIPT)).split("set -euo pipefail")[0]
        self.assertIn("Diagnostics Pack", header)


if __name__ == "__main__":
    import unittest
    unittest.main()
