"""Reader for `sadf -d -U` output (semicolon separated, one row per record per device).

A line starting '# hostname' is a header and resets the column list, so files
with several header blocks parse correctly. The fourth column is a key column
when it is one of CPU, DEV, IFACE, FILESYSTEM, INTR or FCHOST; CPU -1 maps to
'all'.
"""

import math

KEY_COLUMNS = ("CPU", "DEV", "IFACE", "FILESYSTEM", "INTR", "FCHOST")
FIXED_COLUMNS = 3          # hostname, interval, timestamp


class SadfBlock:
    """Rows that share one header."""

    def __init__(self, columns, key_name):
        self.columns = columns          # value column names, after the fixed and key columns
        self.key_name = key_name        # None when there is no key column
        self.rows = []                  # (timestamp, interval, key, [floats])

    def column_index(self, name):
        try:
            return self.columns.index(name)
        except ValueError:
            return None


def _to_float(text):
    try:
        return float(text)
    except ValueError:
        return math.nan


def _normalise_key(name, key):
    if name == "CPU" and key in ("-1", "all"):
        return "all"
    return key


def parse_sadf_text(text):
    """List of SadfBlock from the text of one exported CSV."""
    blocks = []
    current = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("#"):
            stripped = line[1:].strip()
            if not stripped.startswith("hostname"):
                continue
            names = stripped.split(";")
            names = names[FIXED_COLUMNS:]
            key_name = None
            if names and names[0] in KEY_COLUMNS:
                key_name = names[0]
                names = names[1:]
            current = SadfBlock(names, key_name)
            blocks.append(current)
            continue
        if current is None:
            continue
        fields = line.split(";")
        if len(fields) < FIXED_COLUMNS:
            continue
        try:
            timestamp = int(float(fields[2]))
            interval = float(fields[1])
        except ValueError:
            continue
        rest = fields[FIXED_COLUMNS:]
        key = ""
        if current.key_name is not None:
            if not rest:
                continue
            key = _normalise_key(current.key_name, rest[0])
            rest = rest[1:]
        current.rows.append((timestamp, interval, key, [_to_float(item) for item in rest]))
    return blocks


def read_sadf_file(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return parse_sadf_text(handle.read())


def read_sadf_directory(directory):
    """Dict file stem -> [SadfBlock] for every *.csv in a sadf/ directory."""
    import os

    tables = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".csv"):
            continue
        blocks = read_sadf_file(os.path.join(directory, name))
        if any(block.rows for block in blocks):
            tables[name[:-4]] = blocks
    return tables
