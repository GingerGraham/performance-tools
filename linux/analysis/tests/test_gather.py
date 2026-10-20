"""Tests for linux/os/gather-perf-diag.sh."""

import datetime
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from helpers import read_bytes, read_text, write_text

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "os", "gather-perf-diag.sh"))

SADC_CANDIDATES = (
    "/usr/lib64/sa/sadc", "/usr/lib/sa/sadc", "/usr/libexec/sysstat/sadc", "/usr/lib/sysstat/sadc",
)


def find_sadc():
    for path in SADC_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    return shutil.which("sadc")


def stamp(moment):
    return moment.strftime("%Y%m%d_%H%M%S")


def parse_kv(text):
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


PIDSTAT_SAMPLE = """Linux 6.1.0 (db01) \t2026-10-16 \t_x86_64_\t(2 CPU)

# Time        UID       PID    %usr %system  %guest   %wait    %CPU   CPU  minflt/s  majflt/s     VSZ     RSS   %MEM   kB_rd/s   kB_wr/s kB_ccwr/s iodelay   cswch/s nvcswch/s  Command
1792186348      0       100    1.00    0.00    0.00    0.00    1.00     0      0.00      0.00    1000     100   0.01      0.00      0.00      0.00       0      1.00      0.00  mysql -psecret --password=hunter2
1792186348      0       101    1.00    0.00    0.00    0.00    1.00     0      0.00      0.00    1000     100   0.01      0.00      0.00      0.00       0      1.00      0.00  PGPASSWORD=abc psql --token xyz
1792186348      0       102    1.00    0.00    0.00    0.00    1.00     0      0.00      0.00    1000     100   0.01      0.00      0.00      0.00       0      1.00      0.00  sshd -D
"""


class GatherCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sadc = find_sadc()
        cls.have_sadf = shutil.which("sadf") is not None
        cls.class_tmp = tempfile.mkdtemp(prefix="gather-sa-")
        cls.sa_template = None
        if cls.sadc and cls.have_sadf:
            cls.sa_template = os.path.join(cls.class_tmp, "sa_template")
            subprocess.run([cls.sadc, "-S", "XDISK,SNMP", "1", "3", cls.sa_template],
                           check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.class_tmp, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gather-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = os.path.join(self.tmp, "base")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.base)
        os.makedirs(self.out)
        self.now = datetime.datetime.now().replace(microsecond=0)

    def make_run(self, tag, when, with_sa=False, pidstat=None, duration=60, host="db01", base=None):
        name = "%s_%s_%s" % (host, tag, stamp(when))
        path = os.path.join(base or self.base, name)
        os.makedirs(path)
        manifest = [
            "script=test", "hostname=" + host, "cpus=2", "mem_total_kb=1000", "timezone=UTC +0000",
            "start=" + when.astimezone().isoformat(timespec="seconds"),
            "interval_s=10", "samples=6", "duration_s=%d" % duration, "psi=yes", "notes=",
        ]
        with open(os.path.join(path, "manifest.txt"), "w") as handle:
            handle.write("\n".join(manifest) + "\n")
        if with_sa:
            shutil.copy(self.sa_template, os.path.join(path, "sa_" + stamp(when)))
        if pidstat is not None:
            with open(os.path.join(path, "pidstat_%s.log" % stamp(when)), "w") as handle:
                handle.write(pidstat)
        return path

    def gather(self, *args, env=None):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        result = subprocess.run(["bash", SCRIPT, "--base", self.base, "-o", self.out] + list(args),
                                capture_output=True, text=True, env=full_env)
        return result

    def bundle_path(self, suffix=".tar.gz"):
        found = [f for f in os.listdir(self.out) if f.endswith(suffix)]
        self.assertEqual(len(found), 1, os.listdir(self.out))
        return os.path.join(self.out, found[0])

    def members(self, path=None):
        with tarfile.open(path or self.bundle_path()) as archive:
            return archive.getnames()

    def read_member(self, suffix):
        with tarfile.open(self.bundle_path()) as archive:
            for member in archive.getmembers():
                if member.name.endswith(suffix):
                    return archive.extractfile(member).read().decode()
        self.fail("no member ending in " + suffix)


class SelectionTests(GatherCase):
    def test_since_excludes_old_run(self):  # T3
        self.make_run("os", self.now - datetime.timedelta(days=1))
        self.make_run("os", self.now - datetime.timedelta(minutes=10))
        result = self.gather("--since", "1h", "--no-inventory", "--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [n for n in self.members() if n.count("/") == 1 and "_os_" in n]
        self.assertEqual(len(names), 1)
        self.assertIn(stamp(self.now - datetime.timedelta(minutes=10)), names[0])

    def test_default_window_is_seven_days(self):
        self.make_run("os", self.now - datetime.timedelta(days=45))
        self.make_run("os", self.now - datetime.timedelta(days=2))
        self.gather("--no-inventory", "--no-kernel-log")
        names = [n for n in self.members() if n.count("/") == 1 and "_os_" in n]
        self.assertEqual(len(names), 1)

    def test_nothing_selected_is_exit_one(self):
        self.make_run("os", self.now - datetime.timedelta(days=45))
        result = self.gather("--no-inventory", "--no-kernel-log")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no run directories", result.stderr)

    def test_earlier_bundles_are_not_included(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        self.make_run("gather", self.now - datetime.timedelta(minutes=2))
        self.gather("--no-inventory", "--no-kernel-log")
        inner = [n.split("/")[1] for n in self.members() if n.count("/") >= 1 and n.split("/")[1]]
        self.assertFalse([n for n in inner if "_gather_" in n])

    def test_run_option_ignores_since(self):
        old = self.make_run("os", self.now - datetime.timedelta(days=30))
        result = self.gather("--run", old, "--since", "1h", "--no-inventory", "--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--since ignored", result.stderr)
        self.assertTrue([n for n in self.members() if "_os_" in n])

    def test_run_option_repeatable_and_deduplicated(self):
        first = self.make_run("os", self.now - datetime.timedelta(minutes=30))
        second = self.make_run("psar", self.now - datetime.timedelta(minutes=29))
        self.gather("--run", first, "--run", second, "--run", first, "--no-inventory", "--no-kernel-log")
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertEqual(manifest["run.1"], os.path.basename(first))
        self.assertEqual(manifest["run.2"], os.path.basename(second))
        self.assertNotIn("run.3", manifest)

    def test_run_option_rejects_missing_and_misnamed(self):
        self.assertEqual(self.gather("--run", "/nonexistent", "--no-inventory").returncode, 1)
        odd = os.path.join(self.tmp, "not_a_run")
        os.makedirs(odd)
        self.assertEqual(self.gather("--run", odd, "--no-inventory").returncode, 1)


ACCOUNT_OF_ENGINE = {"postgresql": "postgres", "mysql": "mysql", "oracle": "oracle", "mssql": "mssql"}

DB_RUN_FILES = {
    "postgresql": ("pg_stats.csv", "pg_activity.csv"),
    "mysql": ("mysql_stats.csv",),
    "oracle": ("oracle_stats.csv",),
    "mssql": ("mssql_stats.csv",),
}

PS_WITH_POSTGRESQL_AND_MYSQL = """\
    1     0 root     /sbin/init
 1234     1 postgres /usr/pgsql-16/bin/postgres -D /var/lib/pgsql/16/data
 1240  1234 postgres postgres: checkpointer
 2001     1 mysql    /usr/sbin/mysqld --datadir=/var/lib/mysql
 3000     1 root     /usr/bin/mysqld_exporter
"""

PS_WITHOUT_DATABASES = """\
    1     0 root     /sbin/init
 4000     1 alice    -bash
 4001  4000 alice    psql -h db01 postgres
 4002     1 root     /usr/bin/mysqld_safe
"""


class DatabaseGatherCase(GatherCase):
    """Default search bases, with account home directories under the test's own tmp.

    A getent stub on PATH maps account names to those homes; the default search
    also looks in the real /var/log/perf-diag, so the tests check for specific runs
    and bases and never for an exact total.
    """

    def setUp(self):
        super().setUp()
        self.homes = os.path.join(self.tmp, "homes")
        self.stub_bin = os.path.join(self.tmp, "stubbin")
        os.makedirs(self.stub_bin)
        self.passwd = os.path.join(self.tmp, "passwd")
        write_text(self.passwd, "")
        stub = os.path.join(self.stub_bin, "getent")
        write_text(stub, '#!/bin/sh\n[ "$1" = passwd ] || exit 2\ngrep "^$2:" "%s" || exit 2\n' % self.passwd)
        os.chmod(stub, 0o755)
        self.invoker_home = self.add_account(None, "invoker")
        self.ps_file = os.path.join(self.tmp, "ps.txt")
        write_text(self.ps_file, PS_WITHOUT_DATABASES)

    def add_account(self, name, dirname=None):
        """Creates <homes>/<dirname> and, when `name` is given, an account whose home it is."""
        home = os.path.join(self.homes, dirname or name)
        os.makedirs(home, exist_ok=True)
        if name:
            with open(self.passwd, "a") as handle:
                handle.write("%s:x:1000:1000::%s:/bin/bash\n" % (name, home))
        return home

    def make_db_run(self, home, engine, when=None):
        """A DB collector run under <home>/perf-diag with the files the collectors write."""
        base = os.path.join(home, "perf-diag")
        os.makedirs(base, exist_ok=True)
        run = self.make_run(engine, when or self.now - datetime.timedelta(minutes=30), base=base)
        for name in DB_RUN_FILES[engine] + ("db_paths.txt", "errors.log"):
            write_text(os.path.join(run, name), "timestamp,x\n1,2\n" if name.endswith(".csv") else "x\n")
        return run

    def gather_default(self, *args, sudo_user=None):
        """Runs gather with no --base, so the default search applies."""
        env = dict(os.environ)
        env["HOME"] = self.invoker_home
        env["PATH"] = self.stub_bin + os.pathsep + env["PATH"]
        env["PERF_DIAG_PS_FILE"] = self.ps_file
        env.pop("SUDO_USER", None)
        if sudo_user:
            env["SUDO_USER"] = sudo_user
        return subprocess.run(["bash", SCRIPT, "-o", self.out, "--no-inventory", "--no-kernel-log"] + list(args),
                              capture_output=True, text=True, env=env)

    def run_names(self):
        """Names of the run directories at the top of the bundle."""
        with tarfile.open(self.bundle_path()) as archive:
            return {m.name.split("/")[1] for m in archive.getmembers() if m.isdir() and m.name.count("/") == 1}

    def bundle_files(self):
        """Paths of the files in the bundle, relative to its root directory."""
        with tarfile.open(self.bundle_path()) as archive:
            return {m.name.split("/", 1)[1] for m in archive.getmembers() if m.isfile()}

    def make_invoker_os_run(self):
        """An OS run in ~/perf-diag of the invoking user."""
        base = os.path.join(self.invoker_home, "perf-diag")
        os.makedirs(base, exist_ok=True)
        return self.make_run("os", self.now - datetime.timedelta(minutes=30), base=base)


class SearchBaseTests(DatabaseGatherCase):
    def test_db_run_in_sudo_users_home_is_bundled(self):  # A1
        home = self.add_account("alice")
        run = self.make_db_run(home, "postgresql")
        self.make_invoker_os_run()
        result = self.gather_default(sudo_user="alice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(os.path.basename(run), self.run_names())
        # every file of the DB run is in the bundle
        files = self.bundle_files()
        for name in ("pg_stats.csv", "pg_activity.csv", "db_paths.txt", "manifest.txt", "errors.log"):
            self.assertIn("%s/%s" % (os.path.basename(run), name), files)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        bases = [value for key, value in manifest.items() if key.startswith("base.")]
        self.assertIn(os.path.join(home, "perf-diag"), bases)
        self.assertIn(os.path.join(self.invoker_home, "perf-diag"), bases)
        runs = [value for key, value in manifest.items() if key.startswith("run.") and not key.endswith(".bytes")]
        self.assertIn(os.path.basename(run), runs)

    def test_default_search_finds_service_account_homes(self):
        runs = {}
        for engine in ("postgresql", "mysql", "oracle", "mssql"):
            runs[engine] = self.make_db_run(self.add_account(ACCOUNT_OF_ENGINE[engine]), engine)
        result = self.gather_default()
        self.assertEqual(result.returncode, 0, result.stderr)
        for engine, run in runs.items():
            self.assertIn(os.path.basename(run), self.run_names(), engine)
            self.assertIn("%s/%s" % (os.path.basename(run), DB_RUN_FILES[engine][0]), self.bundle_files())

    def test_accounts_that_do_not_exist_are_skipped_silently(self):
        self.make_db_run(self.add_account("postgres"), "postgresql")
        result = self.gather_default()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("mysql", "oracle", "mssql"):
            self.assertNotIn(name, result.stderr)
        self.assertNotIn("cannot read", result.stderr)

    def test_invoking_users_own_home_is_searched(self):
        run = self.make_db_run(self.invoker_home, "mysql")
        self.assertEqual(self.gather_default().returncode, 0)
        self.assertIn(os.path.basename(run), self.run_names())

    def test_same_directory_is_listed_once(self):
        # HOME and the SUDO_USER home are the same directory under some sudo setups.
        home = self.add_account("alice")
        self.make_db_run(home, "mysql")
        self.invoker_home = os.path.join(self.tmp, "homelink")
        os.symlink(home, self.invoker_home)
        result = self.gather_default(sudo_user="alice")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        bases = [value for key, value in manifest.items() if key.startswith("base.")]
        self.assertEqual(len([b for b in bases if b.startswith((home, self.invoker_home))]), 1, bases)

    def test_base_given_twice_uses_exactly_those_two(self):
        first = os.path.join(self.tmp, "first")
        second = os.path.join(self.tmp, "second")
        os.makedirs(first)
        os.makedirs(second)
        one = self.make_run("os", self.now - datetime.timedelta(minutes=20), base=first)
        two = self.make_run("postgresql", self.now - datetime.timedelta(minutes=10), base=second)
        # runs in the default places must be ignored when --base is given
        decoy = self.make_db_run(self.add_account("postgres"), "postgresql", self.now - datetime.timedelta(minutes=5))
        env = dict(os.environ, HOME=self.invoker_home, PERF_DIAG_PS_FILE=self.ps_file,
                   PATH=self.stub_bin + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["bash", SCRIPT, "--base", first, "--base=" + second, "-o", self.out,
                                 "--no-inventory", "--no-kernel-log"], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_names(), {os.path.basename(one), os.path.basename(two)})
        self.assertNotIn(os.path.basename(decoy), self.run_names())
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertEqual(manifest["base.1"], first)
        self.assertEqual(manifest["base.2"], second)
        self.assertNotIn("base.3", manifest)

    def test_missing_explicit_base_is_an_error(self):
        result = subprocess.run(["bash", SCRIPT, "--base", self.base, "--base", os.path.join(self.tmp, "nope"),
                                 "-o", self.out, "--no-inventory"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("base directory not found", result.stderr)

    def test_nothing_found_in_default_bases_names_the_fix(self):
        # Only meaningful when the host has no real /var/log/perf-diag with runs.
        if os.path.isdir("/var/log/perf-diag") and os.listdir("/var/log/perf-diag"):
            self.skipTest("host has runs in /var/log/perf-diag")
        result = self.gather_default()
        self.assertEqual(result.returncode, 1)
        self.assertTrue("no perf-diag directory found" in result.stderr or "no run directories found" in result.stderr)

    def test_unreadable_base_is_named_and_the_rest_still_bundled(self):
        readable = self.make_db_run(self.add_account("postgres"), "postgresql")
        blocked_home = self.add_account("mysql")
        blocked_base = os.path.join(blocked_home, "perf-diag")
        os.makedirs(blocked_base)
        os.chmod(blocked_base, 0)
        self.addCleanup(os.chmod, blocked_base, 0o755)
        result = self.gather_default()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(os.path.basename(readable), self.run_names())
        if os.geteuid() == 0:
            return  # root can read everything: only the bundling half of the test applies
        self.assertIn("NOTE: cannot read %s (run with sudo to include it)" % blocked_base, result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertNotIn(blocked_base, manifest.values())

    def test_home_that_cannot_be_entered_is_named(self):
        self.make_db_run(self.add_account("postgres"), "postgresql")
        blocked_home = self.add_account("oracle")
        os.makedirs(os.path.join(blocked_home, "perf-diag"))
        os.chmod(blocked_home, 0)
        self.addCleanup(os.chmod, blocked_home, 0o755)
        result = self.gather_default()
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.geteuid() == 0:
            return
        self.assertIn("NOTE: cannot read %s (run with sudo to include it)" % os.path.join(blocked_home, "perf-diag"),
                      result.stderr)


class DatabaseDetectionTests(DatabaseGatherCase):
    def test_missing_run_is_a_note_and_db_detected_is_recorded(self):
        write_text(self.ps_file, PS_WITH_POSTGRESQL_AND_MYSQL)
        home = self.add_account("alice")
        self.make_db_run(home, "postgresql")
        self.make_invoker_os_run()
        result = self.gather_default(sudo_user="alice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NOTE: MySQL is running on this host but the bundle has no mysql run; perfdiag will use "
                      "process data only. Run linux/db/collect-mysql-metrics.sh alongside collect-os-metrics.sh next time.",
                      result.stderr)
        self.assertNotIn("PostgreSQL is running on this host", result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertEqual(manifest["db_detected"], "postgresql,mysql")
        self.assertIn("MySQL is running on this host", manifest["notes"])

    def test_exact_note_text_for_postgresql(self):
        write_text(self.ps_file, PS_WITH_POSTGRESQL_AND_MYSQL)
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PERF_DIAG_PS_FILE": self.ps_file})
        self.assertIn("NOTE: PostgreSQL is running on this host but the bundle has no postgresql run; perfdiag will "
                      "use process data only. Run linux/db/collect-postgresql-metrics.sh alongside "
                      "collect-os-metrics.sh next time.", result.stderr)

    def test_no_note_when_every_detected_engine_has_a_run(self):
        write_text(self.ps_file, PS_WITH_POSTGRESQL_AND_MYSQL)
        self.make_run("postgresql", self.now - datetime.timedelta(minutes=5))
        self.make_run("mysql", self.now - datetime.timedelta(minutes=4))
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PERF_DIAG_PS_FILE": self.ps_file})
        self.assertNotIn("is running on this host", result.stderr)
        self.assertEqual(parse_kv(self.read_member("gather_manifest.txt"))["db_detected"], "postgresql,mysql")

    def test_nothing_printed_or_recorded_without_a_database(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PERF_DIAG_PS_FILE": self.ps_file})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("is running on this host", result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertNotIn("db_detected", manifest)
        self.assertNotIn("base.2", manifest)

    def test_detection_uses_the_command_line_not_comm(self):
        # ps cuts nothing here, but a wrapper script named postgres-something must not match.
        write_text(self.ps_file, " 10 1 root /usr/bin/postgres_exporter --web.listen-address=:9187\n"
                                 " 11 1 root /opt/asm_pmon_+ASM\n")
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PERF_DIAG_PS_FILE": self.ps_file})
        self.assertNotIn("is running on this host", result.stderr)

    def test_oracle_and_sql_server_are_detected(self):
        write_text(self.ps_file, " 20 1 oracle ora_pmon_ORCLCDB\n 30 1 mssql /opt/mssql/bin/sqlservr\n")
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PERF_DIAG_PS_FILE": self.ps_file})
        self.assertIn("NOTE: Oracle is running on this host", result.stderr)
        self.assertIn("NOTE: SQL Server is running on this host", result.stderr)
        self.assertIn("collect-mssql-metrics.sh", result.stderr)
        self.assertEqual(parse_kv(self.read_member("gather_manifest.txt"))["db_detected"], "oracle,mssql")


class UsageTests(GatherCase):
    def test_usage_errors_exit_two(self):
        for args in (["--bogus"], ["-d", "5"], ["--since", "banana"], ["--format", "rar"], ["--since"]):
            result = subprocess.run(["bash", SCRIPT] + args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, (args, result.stderr))

    def test_help_and_version(self):
        for flag in ("-h", "--help", "-V", "--version"):
            self.assertEqual(subprocess.run(["bash", SCRIPT, flag], capture_output=True).returncode, 0)

    def test_help_documents_redaction_limits(self):
        text = subprocess.run(["bash", SCRIPT, "-h"], capture_output=True, text=True).stdout
        self.assertIn("NOT redacted", text)

    def test_zip_without_zip_binary_is_exit_one(self):  # T5
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        for name in ("basename", "dirname", "tar"):
            os.symlink(shutil.which(name), os.path.join(tools, name))
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = subprocess.run([shutil.which("bash"), SCRIPT, "--base", self.base, "-o", self.out, "--format", "zip"],
                                capture_output=True, text=True, env={"PATH": tools, "HOME": self.tmp})
        self.assertEqual(result.returncode, 1)
        self.assertIn("zip", result.stderr)
        self.assertIn("install", result.stderr)
        self.assertEqual(os.listdir(self.out), [])

    @unittest.skipUnless(shutil.which("zip"), "zip not installed")
    def test_zip_format(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--format", "zip", "--no-inventory", "--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.bundle_path(".zip")) as archive:
            self.assertTrue([n for n in archive.namelist() if n.endswith("gather_manifest.txt")])


class RedactionTests(GatherCase):
    def test_pidstat_redacted_original_untouched(self):  # T4
        run = self.make_run("os", self.now - datetime.timedelta(minutes=5), pidstat=PIDSTAT_SAMPLE)
        self.gather("--no-inventory", "--no-kernel-log")
        copy = self.read_member(".log")
        self.assertIn("mysql -p<redacted> --password=<redacted>", copy)
        self.assertIn("PGPASSWORD=<redacted> psql --token <redacted>", copy)
        self.assertIn("sshd -D", copy)
        self.assertNotIn("secret", copy)
        self.assertNotIn("hunter2", copy)
        original_name = [f for f in os.listdir(run) if f.startswith("pidstat")][0]
        original = read_text(os.path.join(run, original_name))
        self.assertIn("hunter2", original)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertEqual(manifest["redacted"], "yes")

    def test_no_redact_keeps_text(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5), pidstat=PIDSTAT_SAMPLE)
        result = self.gather("--no-redact", "--no-inventory", "--no-kernel-log")
        self.assertIn("hunter2", self.read_member(".log"))
        self.assertEqual(parse_kv(self.read_member("gather_manifest.txt"))["redacted"], "no")
        self.assertIn("not redacted", result.stderr)


class InventoryTests(GatherCase):
    def test_existing_inventory_is_used(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=20))
        hw = self.make_run("hw", self.now - datetime.timedelta(minutes=10))
        result = self.gather("--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Running", result.stdout)
        self.assertTrue([n for n in self.members() if os.path.basename(hw) in n])

    def test_inventory_generated_when_absent(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=20))
        result = self.gather("--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue([n for n in self.members() if n.endswith("hardware.txt")])

    def test_stale_inventory_triggers_fresh_one(self):
        self.make_run("hw", self.now - datetime.timedelta(hours=5))
        self.make_run("os", self.now - datetime.timedelta(minutes=20))
        self.gather("--no-kernel-log")
        self.assertEqual(len([n for n in self.members() if n.endswith("hardware.txt")]), 1)
        hw_dirs = {n.split("/")[1] for n in self.members() if "_hw_" in n}
        self.assertEqual(len(hw_dirs), 1)

    def test_no_inventory_flag(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=20))
        self.gather("--no-inventory", "--no-kernel-log")
        self.assertFalse([n for n in self.members() if "_hw_" in n])

    def test_warning_without_sibling_script(self):
        lone = os.path.join(self.tmp, "lone")
        os.makedirs(lone)
        shutil.copy(SCRIPT, lone)
        self.make_run("os", self.now - datetime.timedelta(minutes=20))
        result = subprocess.run(["bash", os.path.join(lone, "gather-perf-diag.sh"), "--base", self.base,
                                 "-o", self.out, "--no-kernel-log"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: no hardware inventory", result.stderr)


class ManifestTests(GatherCase):
    def test_manifest_hashes_match(self):  # T1 (hashes)
        self.make_run("os", self.now - datetime.timedelta(minutes=5), pidstat=PIDSTAT_SAMPLE)
        self.gather("--no-inventory", "--no-kernel-log")
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        hashes = {k[len("sha256."):]: v for k, v in manifest.items() if k.startswith("sha256.")}
        self.assertTrue(hashes)
        with tarfile.open(self.bundle_path()) as archive:
            root = archive.getnames()[0].split("/")[0]
            for relative, expected in hashes.items():
                data = archive.extractfile(root + "/" + relative).read()
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected, relative)
        self.assertNotIn("sha256.gather_manifest.txt", manifest)

    def test_manifest_keys(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5), duration=120)
        self.gather("--no-inventory", "--no-kernel-log")
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        for key in ("script", "script_version", "hostname", "start", "run.1", "run.1.bytes",
                    "window_start", "window_end", "kernel_log_source", "redacted", "notes"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["kernel_log_source"], "none")
        start = datetime.datetime.fromisoformat(manifest["window_start"])
        end = datetime.datetime.fromisoformat(manifest["window_end"])
        self.assertEqual((end - start).total_seconds(), 120)

    def test_no_stray_staging_directory(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        self.gather("--no-inventory", "--no-kernel-log")
        self.assertEqual(len([f for f in os.listdir(self.out) if f.startswith(".")]), 0)

    def test_pid_files_not_bundled(self):
        run = self.make_run("os", self.now - datetime.timedelta(minutes=5))
        write_text(os.path.join(run, "sadc.pid"), "123\n")
        self.gather("--no-inventory", "--no-kernel-log")
        self.assertFalse([n for n in self.members() if n.endswith(".pid")])


@unittest.skipUnless(find_sadc() and shutil.which("sadf"), "sysstat not installed")
class SadfExportTests(GatherCase):
    def test_exports_and_no_empty_csv(self):  # T1, T2
        self.make_run("os", self.now - datetime.timedelta(minutes=5), with_sa=True)
        result = self.gather("--no-inventory", "--no-kernel-log")
        self.assertEqual(result.returncode, 0, result.stderr)
        csvs = [n for n in self.members() if n.endswith(".csv")]
        names = {os.path.basename(n) for n in csvs}
        for expected in ("cpu.csv", "load.csv", "memory.csv", "disk.csv", "net_dev.csv"):
            self.assertIn(expected, names)
        self.assertIn("sa_", " ".join(self.members()))
        with tarfile.open(self.bundle_path()) as archive:
            for member in archive.getmembers():
                if member.name.endswith(".csv"):
                    text = archive.extractfile(member).read().decode()
                    self.assertTrue([l for l in text.splitlines() if l and not l.startswith("#")], member.name)
        # fchost is never collected on a VM: a NOTE, and no file.
        self.assertIn("NOTE:", result.stderr)
        self.assertNotIn("fchost.csv", names)

    def test_csv_shape(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5), with_sa=True)
        self.gather("--no-inventory", "--no-kernel-log")
        text = self.read_member("sadf/cpu.csv")
        header = text.splitlines()[0]
        self.assertTrue(header.startswith("# hostname;interval;timestamp;CPU;%usr"))
        first = text.splitlines()[1].split(";")
        self.assertTrue(first[2].isdigit(), "timestamp must be UTC epoch seconds")

    def test_raw_sa_file_unchanged(self):
        run = self.make_run("os", self.now - datetime.timedelta(minutes=5), with_sa=True)
        self.gather("--no-inventory", "--no-kernel-log")
        original = read_bytes(os.path.join(run, [f for f in os.listdir(run) if f.startswith("sa_")][0]))
        with tarfile.open(self.bundle_path()) as archive:
            for member in archive.getmembers():
                if os.path.basename(member.name).startswith("sa_"):
                    self.assertEqual(archive.extractfile(member).read(), original)
                    return
        self.fail("raw sa file missing")


FAKE_SADF_10 = os.path.join(os.path.dirname(__file__), "fake_sadf_10.sh")


def fake_sadf_10_directory(parent):
    """A directory holding a `sadf` that behaves like sysstat 10.1.5, to put first on PATH."""
    directory = os.path.join(parent, "fake-sadf-bin")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "sadf")
    shutil.copy(FAKE_SADF_10, target)
    os.chmod(target, 0o755)
    return directory


class OldSadfMemoryTests(GatherCase):
    """F4: sysstat 10.x rejects `-r ALL`; gather falls back to `-r`. Needs no real sysstat."""

    def test_memory_exported_with_plain_r(self):
        run = self.make_run("os", self.now - datetime.timedelta(minutes=5))
        with open(os.path.join(run, "sa_" + stamp(self.now - datetime.timedelta(minutes=5))), "wb") as handle:
            handle.write(b"dummy")
        path = fake_sadf_10_directory(self.tmp) + os.pathsep + os.environ["PATH"]
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PATH": path})
        self.assertEqual(result.returncode, 0, result.stderr)
        memory = self.read_member("sadf/memory.csv")
        self.assertIn("kbmemfree;kbmemused", memory)
        self.assertIn("exported with '-r'", result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        self.assertIn("memory exported with '-r' (this sadf does not accept '-r ALL')", manifest["notes"])

    def test_activity_missing_from_file_is_still_a_note(self):
        run = self.make_run("os", self.now - datetime.timedelta(minutes=5))
        with open(os.path.join(run, "sa_" + stamp(self.now - datetime.timedelta(minutes=5))), "wb") as handle:
            handle.write(b"dummy")
        path = fake_sadf_10_directory(self.tmp) + os.pathsep + os.environ["PATH"]
        result = self.gather("--no-inventory", "--no-kernel-log", env={"PATH": path})
        self.assertIn("cpu (-u ALL -P ALL) not in file or not supported", result.stderr)
        self.assertNotIn("sadf/cpu.csv", " ".join(self.members()))


class KernelLogTests(GatherCase):
    def test_kernel_log_written_with_source(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        result = self.gather("--no-inventory")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = parse_kv(self.read_member("gather_manifest.txt"))
        if manifest["kernel_log_source"] != "none":
            text = self.read_member("kernel_events.log")
            self.assertTrue(text.startswith("# source=" + manifest["kernel_log_source"]))

    def test_no_kernel_log_flag(self):
        self.make_run("os", self.now - datetime.timedelta(minutes=5))
        self.gather("--no-inventory", "--no-kernel-log")
        self.assertFalse([n for n in self.members() if n.endswith("kernel_events.log")])


if __name__ == "__main__":
    unittest.main()
