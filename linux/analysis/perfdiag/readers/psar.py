"""Reader for psar CSV output (docs/psar.md).

One directory per run: cpu.csv mem.csv disk.csv net.csv load.csv plus
manifest.txt. The first column 'ts' is ISO 8601 with a UTC offset. disk.csv has
a 'device' column and net.csv an 'iface' column after it.
"""

import csv
import math
import os

from ..timeutil import parse_iso

KEY_COLUMN = {"disk": "device", "net": "iface"}
FILES = ("cpu", "mem", "disk", "net", "load")


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return math.nan


def read_psar_file(path, key_column=None):
    """(columns, rows) where rows are (epoch, key, {column: float})."""
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.DictReader(handle)
        columns = [name for name in (reader.fieldnames or []) if name not in ("ts", key_column)]
        for record in reader:
            try:
                epoch = parse_iso(record["ts"])
            except (KeyError, ValueError):
                continue
            key = record.get(key_column, "") if key_column else ""
            rows.append((epoch, key, {name: _to_float(record.get(name)) for name in columns}))
    return columns, rows


def read_psar_directory(directory):
    """Dict file stem -> (columns, rows) for the psar CSV files present."""
    tables = {}
    for stem in FILES:
        path = os.path.join(directory, stem + ".csv")
        if os.path.isfile(path):
            columns, rows = read_psar_file(path, KEY_COLUMN.get(stem))
            if rows:
                tables[stem] = (columns, rows)
    return tables
