"""Helpers for testing the DB collector scripts with fake client programs.

The fake clients in tests/fakeclients/ stand in for psql, mysql, mariadb,
sqlplus and sqlcmd. They print canned output chosen by environment variables
and log every call (arguments and standard input) to the file named by
FAKE_LOG.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import read_text

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.normpath(os.path.join(TESTS_DIR, "..", "..", "db"))
FAKE_DIR = os.path.join(TESTS_DIR, "fakeclients")

DB_SCRIPTS = (
    "collect-postgresql-metrics.sh",
    "collect-mysql-metrics.sh",
    "collect-oracle-metrics.sh",
    "collect-mssql-metrics.sh",
)

BLOCK_D_START = "# ---------------------------------------------------------------------------\n# Shared block D:"
BLOCK_D_END = "# ---------------------------------------------------------------------------\n# Usage\n"


def script_path(script):
    return os.path.join(DB_DIR, script)


def block_d_text(script):
    """The shared block D of a collector script, as text."""
    text = read_text(script_path(script))
    start = text.index(BLOCK_D_START)
    end = text.index(BLOCK_D_END)
    return text[start:end]


def run_bash(code, env=None, cwd=None):
    """Runs a bash snippet and returns the CompletedProcess."""
    return subprocess.run(["bash", "-c", code], capture_output=True, text=True, env=env, cwd=cwd)


def run_block_d(code, extra_definitions=""):
    """Runs a snippet with block D sourced.

    Block D expects block B's short_hostname and a few variables; the stubs
    here supply them. extra_definitions is bash text placed after the stubs.
    """
    stubs = (
        "set -euo pipefail\n"
        "short_hostname() { echo testhost; }\n"
        "manifest_add() { echo \"$1=$2\" >> \"$MANIFEST_PATH\"; }\n"
        "MANIFEST_PATH=${MANIFEST_PATH:-/dev/null}\n"
        "note() { :; }\n"
        "die() { echo \"ERROR: $*\" >&2; exit 1; }\n"
    )
    source = stubs + block_d_text(DB_SCRIPTS[0]) + "\n" + extra_definitions + "\n" + code
    return run_bash(source)


class CollectorRun:
    """The result of one collector run against fake clients."""

    def __init__(self, process, base, log_path):
        self.process = process
        self.base = base
        self.log_path = log_path
        self.run_dir = None
        if os.path.isdir(base):
            runs = sorted(name for name in os.listdir(base) if os.path.isdir(os.path.join(base, name)))
            if runs:
                self.run_dir = os.path.join(base, runs[-1])

    def path(self, name):
        return os.path.join(self.run_dir, name)

    def exists(self, name):
        return self.run_dir is not None and os.path.exists(self.path(name))

    def text(self, name):
        return read_text(self.path(name)) if self.exists(name) else ""

    def lines(self, name):
        return [line for line in self.text(name).splitlines() if line]

    def manifest(self):
        values = {}
        for line in self.lines("manifest.txt"):
            key, _, value = line.partition("=")
            values[key] = value
        return values

    def client_calls(self):
        """The logged calls, one string per call."""
        if not os.path.exists(self.log_path):
            return []
        return [line for line in read_text(self.log_path).splitlines() if line]


class CollectorTestCase(unittest.TestCase):
    """Base class: a temp directory per test and a runner for one script."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dbcollector-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def run_collector(self, script, args=None, fake_env=None, count=2, interval=1, timeout=60):
        base = os.path.join(self.tmp, "out")
        log_path = os.path.join(self.tmp, "fake.log")
        env = {
            "PATH": FAKE_DIR + os.pathsep + os.environ["PATH"],
            "HOME": self.home,
            "FAKE_LOG": log_path,
            "LC_ALL": "C",
        }
        env.update(fake_env or {})
        command = [
            script_path(script), "-c", str(count), "-i", str(interval),
            "--foreground", "-o", base,
        ] + list(args or [])
        process = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)
        return CollectorRun(process, base, log_path)


def csv_rows(text):
    """Rows of a CSV text through the csv module, header included."""
    import csv
    import io
    return list(csv.reader(io.StringIO(text)))


def header_matches_rows(testcase, run, name):
    """Every data row of a collector CSV has as many fields as its header."""
    rows = csv_rows(run.text(name))
    testcase.assertTrue(rows, "%s is empty" % name)
    width = len(rows[0])
    for row in rows[1:]:
        testcase.assertEqual(len(row), width, "%s row %r" % (name, row))
    return rows


def shellcheck_available():
    return shutil.which("shellcheck") is not None


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", text)
