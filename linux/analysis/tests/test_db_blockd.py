"""Shared block D of the DB collectors: identical copies, routing, locality, paths."""

import os
import subprocess
import tempfile
import unittest

import dbsupport
from helpers import read_text


class BlockDIdenticalTests(unittest.TestCase):
    def test_block_d_is_identical_in_all_four_collectors(self):
        reference = dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0])
        for script in dbsupport.DB_SCRIPTS[1:]:
            self.assertEqual(dbsupport.block_d_text(script), reference, script)

    def test_every_collector_is_version_3(self):
        for script in dbsupport.DB_SCRIPTS:
            self.assertIn('SCRIPT_VERSION="3.0.0"', read_text(dbsupport.script_path(script)), script)

    @unittest.skipUnless(dbsupport.shellcheck_available(), "shellcheck is not installed")
    def test_shellcheck_is_clean(self):
        for script in dbsupport.DB_SCRIPTS:
            result = subprocess.run(["shellcheck", "-x", dbsupport.script_path(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, "%s\n%s" % (script, result.stdout))


class CsvFieldCountTests(unittest.TestCase):
    def count(self, row):
        result = dbsupport.run_bash(
            dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0]) + '\ncsv_field_count "$ROW"\n',
            env={"PATH": os.environ["PATH"], "ROW": row},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return int(result.stdout.strip())

    def test_plain_fields(self):
        self.assertEqual(self.count("1,2,3"), 3)

    def test_empty_fields_count(self):
        self.assertEqual(self.count("1,,3,"), 4)

    def test_single_field(self):
        self.assertEqual(self.count("abc"), 1)

    def test_quoted_comma_is_not_a_separator(self):
        self.assertEqual(self.count('"app,db",2,3'), 3)

    def test_doubled_quote_inside_a_quoted_field(self):
        self.assertEqual(self.count('"say ""hi, there""",2'), 2)

    def test_empty_quoted_field(self):
        self.assertEqual(self.count('"x","",3'), 3)


class RouteSampleOutputTests(unittest.TestCase):
    def route(self, output):
        directory = tempfile.mkdtemp(prefix="route-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", directory]))
        code = (
            'OUTDIR="%s"; ERROR_LOG="%s/errors.log"; : > "$ERROR_LOG"\n'
            'register_record S stats.csv "ts,a,b,c"\n'
            'register_record W waits.csv "ts,kind,sessions"\n'
            'route_sample_output "T1" "$SAMPLE"\n'
        ) % (directory, directory)
        result = dbsupport.run_bash(
            "set -euo pipefail\n" + dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0]) + "\n" + code,
            env={"PATH": os.environ["PATH"], "SAMPLE": output},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return (
            read_text(os.path.join(directory, "stats.csv")),
            read_text(os.path.join(directory, "waits.csv")),
            read_text(os.path.join(directory, "errors.log")),
        )

    def test_rows_go_to_the_file_for_their_record_letter(self):
        stats, waits, errors = self.route('S,1,2,3\nW,"IO,read",4\n')
        self.assertEqual(stats, "ts,a,b,c\nT1,1,2,3\n")
        self.assertEqual(waits, 'ts,kind,sessions\nT1,"IO,read",4\n')
        self.assertEqual(errors, "")

    def test_unexpected_record_letter_goes_to_the_error_log(self):
        stats, _, errors = self.route("S,1,2,3\nTime: 0.12 ms\n")
        self.assertEqual(stats, "ts,a,b,c\nT1,1,2,3\n")
        self.assertIn("T1 unexpected output: Time: 0.12 ms", errors)

    def test_wrong_field_count_goes_to_the_error_log(self):
        stats, _, errors = self.route("S,1,2\nS,1,2,3\n")
        self.assertEqual(stats, "ts,a,b,c\nT1,1,2,3\n")
        self.assertIn("S row has 2 fields, expected 3: S,1,2", errors)

    def test_blank_lines_and_carriage_returns_are_ignored(self):
        stats, _, errors = self.route("\nS,1,2,3\r\n\n")
        self.assertEqual(stats, "ts,a,b,c\nT1,1,2,3\n")
        self.assertEqual(errors, "")

    def test_a_missing_summary_row_leaves_no_row(self):
        stats, waits, _ = self.route('W,"IO",2\n')
        self.assertEqual(stats, "ts,a,b,c\n")
        self.assertEqual(waits, "ts,kind,sessions\nT1,\"IO\",2\n")


class QueryTimeoutTests(unittest.TestCase):
    def timeout_for(self, interval):
        result = dbsupport.run_bash(
            "set -euo pipefail\n" + dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0])
            + "\nINTERVAL_SECS=%d\nset_query_timeout\necho $QUERY_TIMEOUT_S\n" % interval,
            env={"PATH": os.environ["PATH"]},
        )
        return int(result.stdout.strip())

    def test_timeout_leaves_room_before_the_next_sample(self):
        self.assertEqual(self.timeout_for(1), 1)
        self.assertEqual(self.timeout_for(2), 1)
        self.assertEqual(self.timeout_for(3), 2)
        self.assertEqual(self.timeout_for(6), 5)
        self.assertEqual(self.timeout_for(7), 5)
        self.assertEqual(self.timeout_for(60), 58)


class LocalityTests(unittest.TestCase):
    def locality(self, host, server_host, local_addresses="", getent=""):
        definitions = (
            "local_addresses() { printf '%s' \"$FAKE_ADDRESSES\"; }\n"
            "getent() { printf '%s' \"$FAKE_GETENT\"; }\n"
        )
        code = (
            "set -euo pipefail\nshort_hostname() { echo dbhost; }\n"
            + dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0])
            + "\n" + definitions
            + 'resolve_target_locality "$HOST_ARG" "$SERVER_ARG"\necho "$TARGET_LOCAL $TARGET_LOCAL_BASIS"\n'
        )
        result = dbsupport.run_bash(code, env={
            "PATH": os.environ["PATH"], "FAKE_ADDRESSES": local_addresses, "FAKE_GETENT": getent,
            "HOST_ARG": host, "SERVER_ARG": server_host,
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_empty_host_is_the_socket(self):
        self.assertEqual(self.locality("", ""), "yes socket")

    def test_socket_directory_is_the_socket(self):
        self.assertEqual(self.locality("/var/run/postgresql", ""), "yes socket")

    def test_loopback_names_and_addresses(self):
        for host in ("localhost", "localhost.localdomain", "127.0.0.1", "::1", "[::1]"):
            self.assertEqual(self.locality(host, ""), "yes loopback", host)

    def test_server_hostname_matches_this_host(self):
        self.assertEqual(self.locality("db1.example.com", "dbhost.example.com"), "yes server hostname")

    def test_address_of_this_host(self):
        result = self.locality(
            "db1.example.com", "other", local_addresses="10.0.0.5\n192.168.1.9\n",
            getent="10.0.0.5 STREAM db1.example.com\n10.0.0.5 DGRAM\n",
        )
        self.assertEqual(result, "yes local address")

    def test_remote_with_known_server_hostname(self):
        result = self.locality(
            "db1.example.com", "otherhost", local_addresses="10.0.0.5\n",
            getent="10.9.9.9 STREAM db1.example.com\n",
        )
        self.assertEqual(result, "no none")

    def test_remote_without_a_server_hostname_is_unknown(self):
        result = self.locality(
            "db1.example.com", "", local_addresses="10.0.0.5\n", getent="10.9.9.9 STREAM db1.example.com\n",
        )
        self.assertEqual(result, "unknown none")


class DbPathsTests(unittest.TestCase):
    def paths_file(self, local, calls):
        directory = tempfile.mkdtemp(prefix="paths-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", directory]))
        code = (
            'OUTDIR="%s"\nTARGET_LOCAL="%s"\n%s\n' % (directory, local, calls)
        )
        result = dbsupport.run_bash(
            "set -euo pipefail\n" + dbsupport.block_d_text(dbsupport.DB_SCRIPTS[0]) + "\n" + code,
            env={"PATH": os.environ["PATH"]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        path = os.path.join(directory, "db_paths.txt")
        return read_text(path) if os.path.exists(path) else None

    def test_local_server_paths_are_numbered_per_role(self):
        text = self.paths_file("yes", 'add_db_path data /d1\nadd_db_path wal /w1 "assumed"\nadd_db_path data /d2\n')
        self.assertEqual(
            text.splitlines(),
            ["path.data.1=/d1", "path.wal.1=/w1", "path.wal.1.note=assumed", "path.data.2=/d2"],
        )

    def test_remote_server_writes_no_file(self):
        self.assertIsNone(self.paths_file("no", "add_db_path data /d1\n"))

    def test_duplicates_are_dropped(self):
        text = self.paths_file("yes", "add_db_path data /d1\nadd_db_path data /d1\n")
        self.assertEqual(text.splitlines(), ["path.data.1=/d1"])

    def test_at_most_fifty_entries_per_role(self):
        text = self.paths_file("yes", 'for n in $(seq 1 60); do add_db_path tablespace "/ts$n"; done\n')
        self.assertEqual(len(text.splitlines()), 50)


if __name__ == "__main__":
    unittest.main()
