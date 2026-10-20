"""Readers for the CSV files the DB collectors write (docs/specs/db-collectors-v3.md sections 2-6).

Two shapes:

* wide: one row per sample, one column per counter (pg_stats.csv, mysql_stats.csv, ...).
* long: several rows per sample, identified by key columns (pg_waits.csv: wait type and event;
  mssql_files.csv: database, file type and path). Read into one WideTable per key.

Both are header-driven, so a column the collector does not write is simply absent. The first
column is `ts`, ISO 8601 with a UTC offset. An empty field, `NULL` or anything that is not a
number becomes NaN. A row whose field count differs from the header is skipped and counted;
the caller makes one note per file from the counts. Strings are CSV-quoted by the collector,
so the standard csv module reads a database name with a comma correctly.
"""

import csv
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..timeutil import TimeError, parse_iso


@dataclass
class WideTable:
    """Sample times (epoch seconds, sorted, one per time) and one value list per column."""

    times: List[int] = field(default_factory=list)
    columns: Dict[str, List[float]] = field(default_factory=dict)
    skipped_rows: int = 0         # field count differs from the header
    unreadable_rows: int = 0      # timestamp missing or not ISO 8601
    error: str = ""               # set when the file could not be used at all

    def __len__(self):
        return len(self.times)

    def get(self, column):
        """Values of `column`, or None when the collector did not write it."""
        return self.columns.get(column)

    def has_values(self, column):
        """True when the column exists and holds at least one number."""
        values = self.columns.get(column)
        return values is not None and any(not math.isnan(value) for value in values)

    def problems(self, file_name):
        """Notes describing skipped rows, one per kind. Empty when the file was clean."""
        found = []
        if self.error:
            found.append("%s: %s" % (file_name, self.error))
        if self.skipped_rows:
            found.append("%s: %d row%s skipped because the field count differs from the header" % (
                file_name, self.skipped_rows, "" if self.skipped_rows == 1 else "s"))
        if self.unreadable_rows:
            found.append("%s: %d row%s skipped because the timestamp could not be read" % (
                file_name, self.unreadable_rows, "" if self.unreadable_rows == 1 else "s"))
        return found


@dataclass
class LongTable:
    """WideTables by key. The key is a tuple with one text per key column."""

    key_columns: List[str] = field(default_factory=list)
    rows_by_key: Dict[Tuple[str, ...], WideTable] = field(default_factory=dict)
    skipped_rows: int = 0
    unreadable_rows: int = 0
    error: str = ""

    def __len__(self):
        return sum(len(table) for table in self.rows_by_key.values())

    def all_times(self):
        """Sorted distinct sample times in which any key has a row."""
        found = set()
        for table in self.rows_by_key.values():
            found.update(table.times)
        return sorted(found)

    def problems(self, file_name):
        return WideTable(skipped_rows=self.skipped_rows, unreadable_rows=self.unreadable_rows,
                         error=self.error).problems(file_name)


def to_number(text):
    """float for a numeric field; NaN for empty, NULL, text and non-finite values."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return math.nan
    if not math.isfinite(value):
        return math.nan
    return value


def _read_rows(path):
    """(header, good rows, skipped count, unreadable count, error). A good row is (epoch, fields)."""
    try:
        handle = open(path, newline="", encoding="utf-8", errors="replace")
    except OSError as error:
        return [], [], 0, 0, "cannot read file (%s)" % error.strerror
    with handle:
        reader = csv.reader(handle)
        header = []
        for row in reader:
            if row:
                header = [name.strip() for name in row]
                break
        if not header or header[0] != "ts":
            return [], [], 0, 0, "no header line starting with ts"
        rows = []
        skipped = unreadable = 0
        try:
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    skipped += 1
                    continue
                try:
                    epoch = parse_iso(row[0])
                except (TimeError, ValueError):
                    unreadable += 1
                    continue
                rows.append((epoch, row))
        except csv.Error as error:
            # An unbalanced quote swallows the rest of the file; keep what was read before it.
            return header, rows, skipped, unreadable, "unreadable CSV after row %d (%s)" % (len(rows) + 1, error)
    return header, rows, skipped, unreadable, ""


def _wide_from_rows(header, rows, numeric_indexes):
    """WideTable from (epoch, fields) rows: sorted by time; a repeated time keeps its last row."""
    last_by_time = {}
    for epoch, fields in rows:
        last_by_time[epoch] = fields
    times = sorted(last_by_time)
    columns = {}
    for index in numeric_indexes:
        columns[header[index]] = [to_number(last_by_time[epoch][index]) for epoch in times]
    return WideTable(times=times, columns=columns)


def read_wide_csv(path):
    """WideTable of a one-row-per-sample file. Never raises: a bad file gives an empty table with `error`."""
    header, rows, skipped, unreadable, error = _read_rows(path)
    if not header or header[0] != "ts":
        return WideTable(error=error)
    table = _wide_from_rows(header, rows, range(1, len(header)))
    table.skipped_rows = skipped
    table.unreadable_rows = unreadable
    table.error = error
    return table


def read_long_csv(path, key_columns):
    """LongTable of a file whose rows are identified by `key_columns` (text, after ts).

    All other columns are numbers. A file missing one of the key columns is unusable.
    """
    header, rows, skipped, unreadable, error = _read_rows(path)
    table = LongTable(key_columns=list(key_columns), skipped_rows=skipped, unreadable_rows=unreadable, error=error)
    if not header or header[0] != "ts":
        return table
    missing = [name for name in key_columns if name not in header]
    if missing:
        table.error = "missing column %s" % ", ".join(missing)
        return table
    key_indexes = [header.index(name) for name in key_columns]
    numeric_indexes = [index for index in range(1, len(header)) if index not in key_indexes]
    grouped: Dict[Tuple[str, ...], list] = {}
    for epoch, fields in rows:
        key = tuple(fields[index] for index in key_indexes)
        grouped.setdefault(key, []).append((epoch, fields))
    for key, key_rows in grouped.items():
        table.rows_by_key[key] = _wide_from_rows(header, key_rows, numeric_indexes)
    return table


def read_optional_wide(path) -> Optional[WideTable]:
    """read_wide_csv, or None when the file does not exist."""
    if not os.path.isfile(path):
        return None
    return read_wide_csv(path)


def read_optional_long(path, key_columns) -> Optional[LongTable]:
    if not os.path.isfile(path):
        return None
    return read_long_csv(path, key_columns)
