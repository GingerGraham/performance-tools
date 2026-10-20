"""Readers for the DB collector CSV files (perfdiag.readers.dbcsv)."""

import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perfdiag.readers import dbcsv  # noqa: E402
from perfdiag.timeutil import parse_iso  # noqa: E402
from helpers import write_text  # noqa: E402


class DbCsvReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfdiag-dbcsv-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        write_text(path, text)
        return path

    def test_wide_table_is_header_driven_and_sorted_by_time(self):
        path = self.write("pg_stats.csv", (
            "ts,xact_commit,deadlocks\n"
            "2026-10-16T15:13:22+00:00,20,1\n"
            "2026-10-16T15:13:21+00:00,10,0\n"))
        table = dbcsv.read_wide_csv(path)
        self.assertEqual(table.times, [parse_iso("2026-10-16T15:13:21+00:00"), parse_iso("2026-10-16T15:13:22+00:00")])
        self.assertEqual(table.get("xact_commit"), [10.0, 20.0])
        self.assertIsNone(table.get("blks_read"))
        self.assertEqual(table.skipped_rows, 0)

    def test_empty_null_and_text_become_nan(self):
        path = self.write("pg_stats.csv", (
            "ts,a,b,c,d\n"
            "2026-10-16T15:13:21+00:00,,NULL,abc,1e3\n"))
        table = dbcsv.read_wide_csv(path)
        for column in ("a", "b", "c"):
            self.assertTrue(math.isnan(table.get(column)[0]), column)
        self.assertEqual(table.get("d"), [1000.0])
        self.assertFalse(table.has_values("a"))
        self.assertTrue(table.has_values("d"))

    def test_rows_with_the_wrong_field_count_are_skipped_and_counted(self):
        path = self.write("pg_stats.csv", (
            "ts,a,b\n"
            "2026-10-16T15:13:21+00:00,1,2\n"
            "2026-10-16T15:13:22+00:00,1\n"
            "Time: 0.123 ms\n"
            "2026-10-16T15:13:23+00:00,3,4\n"))
        table = dbcsv.read_wide_csv(path)
        self.assertEqual(len(table), 2)
        self.assertEqual(table.skipped_rows, 2)
        self.assertEqual(table.problems("pg_stats.csv"),
                         ["pg_stats.csv: 2 rows skipped because the field count differs from the header"])

    def test_unreadable_timestamp_rows_are_counted_separately(self):
        path = self.write("pg_stats.csv", "ts,a\nyesterday,1\n2026-10-16T15:13:21+00:00,2\n2026-10-16T15:13:22,3\n")
        table = dbcsv.read_wide_csv(path)
        self.assertEqual(len(table), 1)
        self.assertEqual(table.unreadable_rows, 2)
        self.assertEqual(table.skipped_rows, 0)

    def test_a_repeated_timestamp_keeps_the_last_row(self):
        path = self.write("pg_stats.csv", "ts,a\n2026-10-16T15:13:21+00:00,1\n2026-10-16T15:13:21+00:00,2\n")
        self.assertEqual(dbcsv.read_wide_csv(path).get("a"), [2.0])

    def test_file_without_a_ts_header_gives_an_error_not_an_exception(self):
        table = dbcsv.read_wide_csv(self.write("x.csv", "time,a\n1,2\n"))
        self.assertEqual(len(table), 0)
        self.assertIn("ts", table.error)
        self.assertEqual(len(dbcsv.read_wide_csv(os.path.join(self.tmp, "missing.csv"))), 0)
        self.assertIsNone(dbcsv.read_optional_wide(os.path.join(self.tmp, "missing.csv")))

    def test_long_table_groups_rows_by_key_and_reads_quoted_commas(self):
        path = self.write("pg_database.csv", (
            "ts,datname,numbackends,xact_commit\n"
            '2026-10-16T15:13:21+00:00,"app,db",2,100\n'
            '2026-10-16T15:13:21+00:00,"postgres",1,10\n'
            '2026-10-16T15:13:22+00:00,"app,db",2,150\n'
            '2026-10-16T15:13:22+00:00,"say ""hi""",1,5\n'))
        table = dbcsv.read_long_csv(path, ("datname",))
        self.assertEqual(sorted(table.rows_by_key), [("app,db",), ("postgres",), ('say "hi"',)])
        self.assertEqual(table.rows_by_key[("app,db",)].get("xact_commit"), [100.0, 150.0])
        self.assertEqual(table.rows_by_key[("app,db",)].get("numbackends"), [2.0, 2.0])
        self.assertIsNone(table.rows_by_key[("app,db",)].get("datname"))
        self.assertEqual(len(table.all_times()), 2)

    def test_long_table_with_two_key_columns_and_a_bad_row(self):
        path = self.write("pg_waits.csv", (
            "ts,wait_event_type,wait_event,sessions\n"
            '2026-10-16T15:13:21+00:00,"IO","DataFileRead",2\n'
            '2026-10-16T15:13:21+00:00,"CPU","CPU"\n'
            '2026-10-16T15:13:22+00:00,"IO","DataFileRead",3\n'))
        table = dbcsv.read_long_csv(path, ("wait_event_type", "wait_event"))
        self.assertEqual(table.skipped_rows, 1)
        self.assertEqual(table.rows_by_key[("IO", "DataFileRead")].get("sessions"), [2.0, 3.0])
        self.assertEqual(table.problems("pg_waits.csv"),
                         ["pg_waits.csv: 1 row skipped because the field count differs from the header"])

    def test_long_table_missing_a_key_column_is_unusable(self):
        path = self.write("pg_waits.csv", "ts,wait_event,sessions\n2026-10-16T15:13:21+00:00,x,1\n")
        table = dbcsv.read_long_csv(path, ("wait_event_type", "wait_event"))
        self.assertEqual(len(table), 0)
        self.assertIn("wait_event_type", table.error)

    def test_unbalanced_quote_keeps_the_rows_before_it(self):
        path = self.write("pg_database.csv", (
            "ts,datname,numbackends\n"
            '2026-10-16T15:13:21+00:00,"a",1\n'
            '2026-10-16T15:13:22+00:00,"unterminated,2\n'))
        table = dbcsv.read_long_csv(path, ("datname",))
        self.assertIn(("a",), table.rows_by_key)


if __name__ == "__main__":
    unittest.main()
