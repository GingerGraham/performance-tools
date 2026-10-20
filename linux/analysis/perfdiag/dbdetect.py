"""Finding database servers without a collector run (spec sections 7.3, 7.4, 8.1 and 8.6).

Two sources, both from the OS side of the bundle:

* pidstat rows (`pidstat -l` keeps the full command line): the signature table below.
* `dbproc.*` keys in hardware.txt, written by collect-hw-inventory.sh for each main
  database server process: engine, working directory (= the data directory for PostgreSQL
  and MySQL), the `-D` / `--datadir=` argument, and the Oracle SID.

Both produce DatabaseInstance objects with source "process". dbload merges them with the
instances of collector runs.

Short names of process-only instances (collector instances use the port or instance name,
see dbload.short_name): pg:proc, my:proc, ora:<SID>, mssql:proc. A second process instance
of the same engine gets a numeric suffix (pg:proc-2).
"""

import os
import re
from dataclasses import dataclass

from .attribution import process_key
from .model import DatabaseInstance
from .readers.pidstat import COMMAND_WIDTH

ENGINE_NAMES = {
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "oracle": "Oracle",
    "mssql": "SQL Server",
}

# Names for the roles in spec 8.6. Commands that match nothing special are client backends
# (PostgreSQL) or foreground processes (Oracle).
ROLE_CLIENT_BACKEND = "client backend"
ROLE_FOREGROUND = "foreground"
ROLE_SERVER = "server"

# Signature table (spec 7.4): the basename of the first command-line argument decides.
POSTGRESQL_PROGRAMS = ("postgres", "postmaster")
MYSQL_PROGRAMS = ("mysqld", "mariadbd")
MSSQL_PROGRAMS = ("sqlservr",)
ORACLE_BACKGROUND = re.compile(r"^ora_([a-z0-9]+)_(.+)$")
ASM_BACKGROUND = re.compile(r"^asm_([a-z0-9]+)_(.+)$")
ORACLE_FOREGROUND_PREFIX = "oracle"

DATA_DIRECTORY_ARGUMENTS = (
    re.compile(r"(?:^|\s)-D\s*(\S+)"),               # postgres -D /var/lib/pgsql/data
    re.compile(r"(?:^|\s)--datadir=(\S+)"),          # mysqld --datadir=/var/lib/mysql
)


@dataclass
class ProcessMatch:
    """What one command line says about the database it belongs to."""

    engine: str              # postgresql | mysql | mariadb | oracle | mssql | asm
    role: str
    instance_name: str = ""  # Oracle SID
    data_directory: str = ""


def _first_program(command):
    """Basename of the first argument without a trailing colon (retitled processes: 'postgres:')."""
    tokens = command.split()
    if not tokens:
        return ""
    first = tokens[0].rstrip(":")
    return first.rsplit("/", 1)[-1]


def data_directory_in(command):
    """Value of -D or --datadir= in a command line, or ''.

    pidstat cuts command lines at COMMAND_WIDTH. A value that runs to the end of a command of
    that width may be cut as well, and a wrong directory is worse than none, so it is dropped.
    """
    for pattern in DATA_DIRECTORY_ARGUMENTS:
        match = pattern.search(command)
        if match:
            if len(command) >= COMMAND_WIDTH and match.end() == len(command):
                return ""
            return match.group(1)
    return ""


def postgres_title(command):
    """Text after 'postgres: ' of a retitled process, without the cluster name and ' process'."""
    _, _, title = command.partition(":")
    title = title.strip()
    first, _, rest = title.partition(" ")
    # Debian-style cluster names: "postgres: 16/main: checkpointer". "bgworker:" is a role prefix.
    if first.endswith(":") and first.lower() != "bgworker:":
        title = rest.strip()
    if title.lower().endswith(" process"):
        title = title[:-len(" process")]
    return title.strip()


POSTGRES_ROLE_PREFIXES = (
    ("checkpointer", "checkpointer"),
    ("background writer", "background writer"),
    ("walwriter", "WAL writer"),
    ("wal writer", "WAL writer"),
    ("autovacuum", "autovacuum"),
    ("walsender", "replication"),
    ("walreceiver", "replication"),
    ("startup", "replication"),
    ("logical replication", "replication"),
    ("bgworker: logical replication", "replication"),
    # Not in the spec table: server-internal processes that are not client work.
    ("archiver", "background"),
    ("logger", "background"),
    ("stats collector", "background"),
    ("io worker", "background"),
    ("walsummarizer", "background"),
    ("bgworker", "background"),
)


def postgres_role(command):
    tokens = command.split()
    if not tokens:
        return ROLE_CLIENT_BACKEND
    if not tokens[0].endswith(":"):
        return "postmaster"                       # "postgres -D ..." or ".../postmaster -D ..."
    title = postgres_title(command).lower()
    for prefix, role in POSTGRES_ROLE_PREFIXES:
        if title.startswith(prefix):
            return role
    return ROLE_CLIENT_BACKEND


def oracle_background_role(process_name):
    """Role of ora_<name>_<SID> from the <name> part."""
    if process_name.startswith(("dbw", "bw")):
        return "DB writer"
    if process_name == "lgwr" or process_name.startswith("lg0"):
        return "log writer"
    if process_name == "ckpt":
        return "checkpoint"
    if process_name.startswith("arc"):
        return "archiver"
    return "background"


def match_command(command):
    """ProcessMatch for a pidstat command line, or None when it is not a database server process."""
    text = command.strip()
    if text.startswith("|__") or not text:
        return None
    program = _first_program(text)
    if program in POSTGRESQL_PROGRAMS:
        return ProcessMatch("postgresql", postgres_role(text), data_directory=data_directory_in(text))
    if program in MYSQL_PROGRAMS:
        engine = "mariadb" if program == "mariadbd" else "mysql"
        return ProcessMatch(engine, ROLE_SERVER, data_directory=data_directory_in(text))
    if program in MSSQL_PROGRAMS:
        return ProcessMatch("mssql", ROLE_SERVER)
    asm = ASM_BACKGROUND.match(program)
    if asm:
        return ProcessMatch("asm", "background", instance_name=asm.group(2))
    background = ORACLE_BACKGROUND.match(program)
    if background:
        return ProcessMatch("oracle", oracle_background_role(background.group(1)), instance_name=background.group(2))
    if program.startswith(ORACLE_FOREGROUND_PREFIX) and len(program) > len(ORACLE_FOREGROUND_PREFIX):
        sid = program[len(ORACLE_FOREGROUND_PREFIX):]
        if sid.startswith("asm"):
            return None                           # the oracleasm driver tools, not a server process
        if sid.startswith("+"):
            return ProcessMatch("asm", "foreground", instance_name=sid)
        return ProcessMatch("oracle", ROLE_FOREGROUND, instance_name=sid)
    return None


def process_role(engine, command):
    """Role of a command line for a known engine. Unknown commands count as the engine's default role."""
    found = match_command(command)
    if found is not None and found.engine != "asm":
        return found.role
    if engine == "postgresql":
        return ROLE_CLIENT_BACKEND
    if engine == "oracle":
        return ROLE_FOREGROUND
    return ROLE_SERVER


def engine_family(engine):
    """MariaDB and MySQL are one family for matching a process to a collector run."""
    return "mysql" if engine == "mariadb" else engine


def same_directory(left, right):
    """True when two data directory paths name the same place (normalised, no filesystem access)."""
    if not left or not right:
        return False
    return os.path.normpath(left) == os.path.normpath(right)


def new_process_instance(engine, instance_name="", data_directory=""):
    """A DatabaseInstance with source 'process' and no data except what the caller adds."""
    display = ENGINE_NAMES.get(engine, engine)
    label = "%s %s (process data only)" % (display, instance_name) if instance_name else "%s (process data only)" % display
    instance = DatabaseInstance(
        id="%s:process%s" % (engine, ":" + instance_name if instance_name else ""),
        short=process_short_name(engine, instance_name),
        engine=engine, label=label, source="process", local=True, instance_name=instance_name,
        locality_basis="process")
    if data_directory:
        instance.paths["data"] = [data_directory]
    return instance


def process_short_name(engine, instance_name=""):
    if engine == "postgresql":
        return "pg:proc"
    if engine in ("mysql", "mariadb"):
        return "my:proc"
    if engine == "oracle":
        return "ora:%s" % instance_name if instance_name else "ora:proc"
    return "mssql:proc"


def _add_data_directory(instance, directory):
    if directory and directory not in instance.paths.setdefault("data", []):
        instance.paths["data"].append(directory)


def instances_from_processes(host):
    """Process instances from the host's pidstat rows: one per engine, one per SID for Oracle.

    Backends of several PostgreSQL clusters are all titled 'postgres: ...', so their processes
    cannot be split by cluster here. Data directories seen in postmaster command lines are
    listed in paths['data'] for dbload to match against collector runs.
    """
    found = {}
    matches = {}          # pidstat repeats the same command lines in every sample
    for row in host.process_rows:
        if row.is_thread:
            continue
        if row.command not in matches:
            matches[row.command] = match_command(row.command)
        match = matches[row.command]
        if match is None or match.engine == "asm":
            continue
        key = (match.engine, match.instance_name)
        instance = found.get(key)
        if instance is None:
            instance = new_process_instance(match.engine, match.instance_name)
            found[key] = instance
        key_text = process_key(row.command)
        if key_text not in instance.process_keys:
            instance.process_keys.append(key_text)
        instance.process_roles.setdefault(row.command, match.role)
        if match.data_directory:
            _add_data_directory(instance, match.data_directory)
    return [found[key] for key in sorted(found)]


def instances_from_inventory(hardware):
    """Process instances from the dbproc.N.* keys of hardware.txt (spec 7.3). [] without them."""
    if hardware is None:
        return []
    found = []
    index = 1
    while ("dbproc.%d.engine" % index) in hardware.raw:
        prefix = "dbproc.%d." % index
        engine = hardware.get(prefix + "engine")
        index += 1
        if engine not in ENGINE_NAMES:
            continue
        sid = hardware.get(prefix + "instance")
        data_directory = hardware.get(prefix + "args_datadir") or hardware.get(prefix + "cwd")
        if engine == "oracle":
            data_directory = ""                   # the Oracle cwd is not the data location
        instance = new_process_instance(engine, sid, data_directory)
        cwd_note = hardware.get(prefix + "cwd_note")
        if cwd_note:
            instance.note("%s process working directory: %s, so its data location is unknown" % (
                ENGINE_NAMES[engine], cwd_note))
        found.append(instance)
    return found


def process_seen_share(host, process_keys):
    """Share (0-1) of pidstat sample times in which any of `process_keys` appears, or None."""
    if not process_keys or not host.process_rows:
        return None
    keys = set(process_keys)
    all_times = set()
    seen_times = set()
    for row in host.process_rows:
        if row.is_thread:
            continue
        all_times.add(row.time)
        if process_key(row.command) in keys:
            seen_times.add(row.time)
    if not all_times:
        return None
    return len(seen_times) / len(all_times)
