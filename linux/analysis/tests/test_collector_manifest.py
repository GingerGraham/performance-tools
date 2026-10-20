"""tz_name in the manifest of collect-os-metrics.sh and psar.sh (F12)."""

import os
import re
import subprocess
import tempfile
import unittest

OS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "os")


def function_text(script, name):
    with open(os.path.join(OS_DIR, script)) as handle:
        text = handle.read()
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % name, text, re.S | re.M)
    if not match:
        raise AssertionError("%s not found in %s" % (name, script))
    return match.group(0)


class TzNameTests(unittest.TestCase):
    def detect(self, script, tz):
        env = dict(os.environ)
        env.pop("TZ", None)
        if tz is not None:
            env["TZ"] = tz
        code = function_text(script, "detect_tz_name") + "\ndetect_tz_name\n"
        return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env).stdout.strip()

    def test_tz_variable_wins(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            self.assertEqual(self.detect(script, "Europe/London"), "Europe/London")

    def test_posix_style_tz_with_leading_colon_is_not_used_as_a_name(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            self.assertNotEqual(self.detect(script, ":Europe/Paris"), ":Europe/Paris")

    def test_manifest_writes_the_key_after_timezone(self):
        for script in ("collect-os-metrics.sh", "psar.sh"):
            with open(os.path.join(OS_DIR, script)) as handle:
                text = handle.read()
            self.assertIn('echo "timezone=$(date +\'%Z %z\')"\n        echo "tz_name=$(detect_tz_name)"', text)


SIGNATURE_FUNCTIONS = ("classify_db_process", "db_scan", "db_engines_detected")
SIGNATURE_SCRIPTS = ("gather-perf-diag.sh", "collect-os-metrics.sh", "collect-hw-inventory.sh")

# ps -eo pid=,ppid=,user=,args= output. Each line is annotated with the engine it must give.
SIGNATURE_CASES = (
    (" 1234     1 postgres /usr/pgsql-16/bin/postgres -D /var/lib/pgsql/16/data", "postgresql"),
    (" 1235     1 postgres /usr/bin/postmaster -D /srv/pg", "postgresql"),
    (" 1240  1234 postgres postgres: checkpointer", "postgresql"),
    (" 1241  1234 postgres postgres: 16/main: walwriter", "postgresql"),
    (" 2001     1 mysql    /usr/sbin/mysqld --datadir=/var/lib/mysql", "mysql"),
    (" 2002     1 mysql    /usr/sbin/mariadbd", "mysql"),
    (" 2003     1 root     /bin/sh /usr/bin/mysqld_safe", ""),
    (" 2004     1 root     /usr/bin/mysqld_exporter", ""),
    (" 2005     1 root     /usr/bin/postgres_exporter", ""),
    (" 3000     1 oracle   ora_pmon_ORCLCDB", "oracle"),
    (" 3001     1 oracle   oracleORCLCDB (LOCAL=NO)", "oracle"),
    (" 3002     1 oracle   asm_pmon_+ASM", ""),
    (" 3003     1 oracle   ora_dbw0_ORCLCDB", ""),
    (" 4000     1 mssql    /opt/mssql/bin/sqlservr", "mssql"),
    (" 5000     1 alice    psql -h db01 postgres", ""),
    (" 5001     1 alice    vim /etc/mysqld.conf", ""),
    (" 5002     1 root     /usr/bin/mysql -e select", ""),
)


def signature_code(script):
    return "set -euo pipefail\n" + "".join(function_text(script, name) for name in SIGNATURE_FUNCTIONS)


def run_bash(code, env=None):
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=full_env)


class DatabaseSignatureTests(unittest.TestCase):
    """Spec 7.4: the signature table is the same in the three scripts."""

    def test_functions_are_textually_identical(self):
        for name in SIGNATURE_FUNCTIONS:
            texts = {script: function_text(script, name) for script in SIGNATURE_SCRIPTS}
            self.assertEqual(len(set(texts.values())), 1, name)

    def test_every_script_gives_the_same_answer(self):
        canned = "\n".join(line for line, _ in SIGNATURE_CASES) + "\n"
        answers = {}
        for script in SIGNATURE_SCRIPTS:
            code = signature_code(script) + 'printf "%s" "$CANNED" | db_scan\nprintf "%s" "$CANNED" | db_scan | db_engines_detected\n'
            result = run_bash(code, env={"CANNED": canned})
            self.assertEqual(result.returncode, 0, (script, result.stderr))
            answers[script] = result.stdout
        self.assertEqual(len(set(answers.values())), 1, answers)
        scan, _, detected = answers[SIGNATURE_SCRIPTS[0]].rstrip("\n").rpartition("\n")
        self.assertEqual(detected, "postgresql,mysql,oracle,mssql")
        expected = [engine for _, engine in SIGNATURE_CASES if engine]
        self.assertEqual([row.split("|")[0] for row in scan.split("\n")], expected)

    def test_oracle_sid_comes_from_args_not_comm(self):
        code = signature_code("collect-hw-inventory.sh") + 'echo " 9 1 oracle ora_pmon_ORCLCDB" | db_scan\n'
        row = run_bash(code).stdout.strip().split("|")
        self.assertEqual(row[0], "oracle")
        self.assertEqual(row[4], "ORCLCDB")
        row = run_bash(signature_code("gather-perf-diag.sh") + 'echo " 9 1 oracle oracleORCL2 (LOCAL=NO)" | db_scan\n').stdout.strip().split("|")
        self.assertEqual(row[4], "ORCL2")

    def test_scan_row_keeps_the_whole_command_line(self):
        code = signature_code("collect-os-metrics.sh") + 'echo " 7 1 postgres /usr/bin/postgres -D /a b|c" | db_scan\n'
        self.assertEqual(run_bash(code).stdout.strip(), "postgresql|7|1|postgres||/usr/bin/postgres -D /a b|c")

    def test_no_database_gives_an_empty_answer(self):
        code = signature_code("gather-perf-diag.sh") + 'echo " 1 0 root /sbin/init" | db_scan | db_engines_detected\n'
        self.assertEqual(run_bash(code).stdout, "\n")


class OsMetricsDetectionTests(unittest.TestCase):
    """Spec 7.2: collect-os-metrics.sh prints one NOTE per engine and starts nothing."""

    def setUp(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".ps", delete=False)
        self.addCleanup(os.unlink, handle.name)
        self.ps_file = handle.name
        handle.close()

    def run_detect(self, ps_text):
        with open(self.ps_file, "w") as handle:
            handle.write(ps_text)
        script = "collect-os-metrics.sh"
        code = (signature_code(script) + function_text(script, "note")
                + function_text(script, "db_process_list") + function_text(script, "db_display_name")
                + function_text(script, "detect_databases")
                + 'NOTES_TEXT=""; DURATION_SECS=7200; INTERVAL_SECS=30\ndetect_databases\n'
                + 'echo "DETECTED=${DB_DETECTED}"\n')
        return run_bash(code, env={"PERF_DIAG_PS_FILE": self.ps_file})

    def test_one_note_per_engine_with_the_collector_command_line(self):
        result = self.run_detect(" 1 0 postgres /usr/bin/postgres -D /d\n 2 1 mysql /usr/sbin/mysqld\n"
                                 " 3 1 oracle ora_pmon_ORCL\n 4 1 mssql /opt/mssql/bin/sqlservr\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        notes = [line for line in result.stderr.splitlines() if line.startswith("NOTE: ")]
        self.assertEqual(len(notes), 4)
        for engine, label in (("postgresql", "PostgreSQL"), ("mysql", "MySQL"), ("oracle", "Oracle"), ("mssql", "SQL Server")):
            matching = [n for n in notes if n.startswith("NOTE: %s is running on this host" % label)]
            self.assertEqual(len(matching), 1, notes)
            self.assertIn("linux/db/collect-%s-metrics.sh -d 7200 -i 30" % engine, matching[0])
        self.assertIn("DETECTED=postgresql,mysql,oracle,mssql", result.stdout)

    def test_nothing_printed_without_a_database(self):
        result = self.run_detect(" 1 0 root /sbin/init\n 2 1 alice psql -h db postgres\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.strip(), "DETECTED=")

    def test_manifest_key_is_written_only_when_something_was_found(self):
        with open(os.path.join(OS_DIR, "collect-os-metrics.sh")) as handle:
            text = handle.read()
        self.assertIn('if [[ -n "$DB_DETECTED" ]]; then\n    manifest_add db_detected "$DB_DETECTED"\nfi\nmanifest_add notes', text)


if __name__ == "__main__":
    unittest.main()
