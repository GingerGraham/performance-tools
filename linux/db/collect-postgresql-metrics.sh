#!/usr/bin/env bash
#
# collect-postgresql-metrics.sh
# Samples pg_stat_database, pg_stat_bgwriter (pg_stat_checkpointer on 17+) and
# pg_stat_activity at a fixed interval for a fixed duration, matching the
# OS-level sadc/pidstat window.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="2.0.0"
COLLECTOR_TAG="postgresql"
ORIGINAL_ARGS=("$@")

# ---------------------------------------------------------------------------
# Shared block A: messages and option parsing helpers.
# Identical in every script under linux/ (scripts are standalone by design).
# ---------------------------------------------------------------------------
NOTES_TEXT=""

die()         { echo "ERROR: $*" >&2; exit 1; }
usage_error() { echo "ERROR: $*" >&2; echo "Run '${SCRIPT_NAME} --help' for usage." >&2; exit 2; }
warn()        { echo "WARNING: $*" >&2; }

# NOTE: is for optional data that is unavailable on this host. Notes are kept
# for manifest.txt, so call this from the main shell, not from $( ).
note() {
    echo "NOTE: $*" >&2
    NOTES_TEXT="${NOTES_TEXT:+${NOTES_TEXT} | }$*"
}

# Reads the value for an option given as "--opt value", "-o value" or "--opt=value".
# Sets OPT_VALUE and OPT_SHIFT (how many arguments the caller must shift).
take_value() {
    local opt="$1" next="${2-}" has_next="$3"
    if [[ "$opt" == *=* ]]; then
        OPT_VALUE="${opt#*=}"
        OPT_SHIFT=1
    elif [[ "$has_next" == "yes" && "$next" != -* ]]; then
        OPT_VALUE="$next"
        OPT_SHIFT=2
    else
        usage_error "option '${opt%%=*}' needs a value"
    fi
}

# Bare integer = seconds. Hours and days only where allow_long_units is "yes".
parse_to_seconds() {
    local val="$1" allow_long_units="$2" num unit
    if [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
        return 0
    fi
    if [[ ! "$val" =~ ^([0-9]+)([a-zA-Z]+)$ ]]; then
        echo "ERROR: cannot parse '${val}' - expected e.g. 30, 30s, 15m, 2h, 1d" >&2
        return 1
    fi
    num="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    case "$unit" in
        s|sec|secs|second|seconds) echo $(( num )) ;;
        m|min|mins|minute|minutes) echo $(( num * 60 )) ;;
        h|hr|hrs|hour|hours)
            [[ "$allow_long_units" == "yes" ]] || { echo "ERROR: '${unit}' not valid for an interval" >&2; return 1; }
            echo $(( num * 3600 )) ;;
        d|day|days)
            [[ "$allow_long_units" == "yes" ]] || { echo "ERROR: '${unit}' not valid for an interval" >&2; return 1; }
            echo $(( num * 86400 )) ;;
        *) echo "ERROR: unrecognised unit '${unit}' in '${val}'" >&2; return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Shared block B: host identity and manifest.txt.
# Identical in every script under linux/.
# ---------------------------------------------------------------------------
MANIFEST_PATH=""

short_hostname() {
    local name
    name="$(uname -n)"
    echo "${name%%.*}"
}

os_pretty_name() {
    if [[ -r /etc/os-release ]]; then
        ( . /etc/os-release; echo "${PRETTY_NAME:-${NAME:-unknown}}" )
    else
        echo "unknown"
    fi
}

# Prints the arguments shell-quoted on one line. The value of --connect is
# replaced, because it can hold a password.
redacted_command_line() {
    local arg quoted line="" redact_next="no"
    for arg in "$@"; do
        if [[ "$redact_next" == "yes" ]]; then
            quoted="<redacted>"
            redact_next="no"
        elif [[ "$arg" == "--connect" ]]; then
            quoted="--connect"
            redact_next="yes"
        elif [[ "$arg" == --connect=* ]]; then
            quoted="--connect=<redacted>"
        else
            quoted="$(printf '%q' "$arg")"
        fi
        line="${line:+${line} }${quoted}"
    done
    echo "$line"
}

# Writes the keys every script records. The caller adds its own keys with
# manifest_add, then finishes with manifest_add notes "$NOTES_TEXT".
# $1 = path, $2 = interval seconds, $3 = sample count or "unlimited",
# $4 = duration seconds or "unlimited"
manifest_start() {
    local path="$1" interval_s="$2" samples="$3" duration_s="$4"
    local cpu_count mem_total_kb psi="no"

    cpu_count="$(nproc 2>/dev/null || grep -c '^processor' /proc/cpuinfo)"
    mem_total_kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    if [[ -r /proc/pressure/cpu ]]; then
        psi="yes"
    fi

    MANIFEST_PATH="$path"
    {
        echo "script=${SCRIPT_NAME}"
        echo "script_version=${SCRIPT_VERSION}"
        echo "command_line=$(redacted_command_line ${ORIGINAL_ARGS[@]+"${ORIGINAL_ARGS[@]}"})"
        echo "hostname=$(short_hostname)"
        echo "fqdn=$(hostname -f 2>/dev/null || uname -n)"
        echo "distro=$(os_pretty_name)"
        echo "kernel=$(uname -r)"
        echo "arch=$(uname -m)"
        echo "cpus=${cpu_count}"
        echo "mem_total_kb=${mem_total_kb}"
        echo "timezone=$(date +'%Z %z')"
        echo "start=$(date -Iseconds)"
        echo "interval_s=${interval_s}"
        echo "samples=${samples}"
        echo "duration_s=${duration_s}"
        echo "psi=${psi}"
    } > "$MANIFEST_PATH"
}

manifest_add() {
    echo "$1=$2" >> "$MANIFEST_PATH"
}

# Appends end= once. Does nothing when no manifest was started.
MANIFEST_FINISHED="no"
manifest_finish() {
    if [[ -n "$MANIFEST_PATH" && "$MANIFEST_FINISHED" == "no" ]]; then
        MANIFEST_FINISHED="yes"
        manifest_add end "$(date -Iseconds)"
    fi
}

# ---------------------------------------------------------------------------
# Shared block C: duration, count and interval resolution for collectors.
# Identical in every collector. psar.sh has its own (count 0 = until stopped).
# Needs DURATION, COUNT, INTERVAL (possibly empty). Sets INTERVAL_SECS,
# SAMPLE_COUNT and DURATION_SECS.
# ---------------------------------------------------------------------------
resolve_timing() {
    if [[ -n "$DURATION" && -n "$COUNT" ]]; then
        usage_error "-d/--duration and -c/--count are mutually exclusive"
    fi
    if [[ -n "$COUNT" && ! "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
        usage_error "-c/--count must be a positive integer"
    fi
    if [[ -z "$DURATION" && -z "$COUNT" ]]; then
        [[ -t 0 ]] || usage_error "no -d/--duration or -c/--count given and no terminal to prompt on"
        read -rp "Collection duration (e.g. 30m, 2h, 1d): " DURATION
    fi
    if [[ -z "$INTERVAL" ]]; then
        [[ -t 0 ]] || usage_error "no -i/--interval given and no terminal to prompt on"
        read -rp "Sample interval (e.g. 15s, 1m): " INTERVAL
    fi

    INTERVAL_SECS=$(parse_to_seconds "$INTERVAL" no) || exit 2
    if (( INTERVAL_SECS < 1 )); then
        usage_error "interval must be at least 1 second"
    fi
    if [[ -n "$COUNT" ]]; then
        SAMPLE_COUNT="$COUNT"
        DURATION_SECS=$(( SAMPLE_COUNT * INTERVAL_SECS ))
    else
        DURATION_SECS=$(parse_to_seconds "$DURATION" yes) || exit 2
        if (( DURATION_SECS < INTERVAL_SECS )); then
            usage_error "duration must be >= interval"
        fi
        SAMPLE_COUNT=$(( DURATION_SECS / INTERVAL_SECS ))
    fi
}

# ---------------------------------------------------------------------------
# Shared block D: output location, sample timing and detaching for the DB
# collectors. Identical in every DB collector.
#
# The script supplies: COLLECTOR_TAG, OUTDIR_BASE (empty = default), FOREGROUND,
# INTERVAL_SECS, SAMPLE_COUNT, and a collect_one_sample function that appends
# one row to its CSV and sends query errors to "${OUTDIR}/errors.log".
# ---------------------------------------------------------------------------
DEFAULT_OUTDIR_BASE="/var/log/perf-diag"

# Centiseconds since boot. /proc/uptime always has two decimal places, so
# removing the point gives an integer: "12345.67" -> "1234567".
read_uptime_cs() {
    local up
    read -r up _ < /proc/uptime
    echo "${up/./}"
}

# Sleep until start_cs + sample_number * interval. Time spent taking a sample
# does not delay later samples. The sleep runs in the background so that a
# stop request is handled straight away instead of after the interval.
SLEEP_PID=""
sleep_until_sample() {
    local start_cs="$1" sample_number="$2" interval_secs="$3"
    local target_cs now_cs wait_cs
    target_cs=$(( start_cs + sample_number * interval_secs * 100 ))
    now_cs="$(read_uptime_cs)"
    wait_cs=$(( target_cs - now_cs ))
    if (( wait_cs > 0 )); then
        sleep "$(( wait_cs / 100 )).$(printf '%02d' $(( wait_cs % 100 )))" &
        SLEEP_PID=$!
        wait "$SLEEP_PID" || true
        SLEEP_PID=""
    fi
}

base_is_writable() {
    mkdir -p "$1" 2>/dev/null && [[ -w "$1" ]]
}

# Sets OUTDIR_BASE. An explicit -o must be writable. With no -o, use
# /var/log/perf-diag, or ~/perf-diag when the first is not writable (running
# under sudo would change $USER and where ~/.pgpass and ~/.my.cnf are read).
resolve_output_base() {
    local home_base="${HOME:-}/perf-diag"
    if [[ -n "$OUTDIR_BASE" ]]; then
        base_is_writable "$OUTDIR_BASE" \
            || die "${OUTDIR_BASE} is not writable; run with sudo or pass a different -o <dir>"
    elif base_is_writable "$DEFAULT_OUTDIR_BASE"; then
        OUTDIR_BASE="$DEFAULT_OUTDIR_BASE"
    elif [[ -n "${HOME:-}" ]] && base_is_writable "$home_base"; then
        note "${DEFAULT_OUTDIR_BASE} is not writable; using ${home_base}. Run with sudo or pass -o <dir> to choose another location"
        OUTDIR_BASE="$home_base"
    else
        die "${DEFAULT_OUTDIR_BASE} is not writable; run with sudo or pass -o <dir>"
    fi
}

# Creates "<base>/<host>_<tag>_<timestamp>" and sets OUTDIR and ERROR_LOG.
make_run_dir() {
    OUTDIR="${OUTDIR_BASE}/$(short_hostname)_${COLLECTOR_TAG}_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$OUTDIR" || die "cannot create ${OUTDIR}"
    ERROR_LOG="${OUTDIR}/errors.log"
    : > "$ERROR_LOG"
}

# Called after the first sample so a broken query is seen on the terminal.
warn_if_queries_failing() {
    local first_error
    if [[ -s "$ERROR_LOG" ]]; then
        read -r first_error < "$ERROR_LOG" || true
        warn "queries are failing (see ${ERROR_LOG}): ${first_error}"
    fi
}

stop_collection() {
    if [[ -n "$SLEEP_PID" ]]; then
        kill "$SLEEP_PID" 2>/dev/null || true
    fi
    manifest_finish
    exit 0
}

# Takes samples first_sample..SAMPLE_COUNT-1, each at its scheduled time.
collection_loop() {
    local first_sample="$1" sample
    trap stop_collection TERM INT
    for (( sample = first_sample; sample < SAMPLE_COUNT; sample = sample + 1 )); do
        sleep_until_sample "$COLLECTION_START_CS" "$sample" "$INTERVAL_SECS"
        collect_one_sample
    done
    manifest_finish
}

# Sample 0 is taken attached to the terminal. The rest run in the foreground
# (--foreground) or detached from the terminal (default).
run_collection() {
    COLLECTION_START_CS="$(read_uptime_cs)"
    trap stop_collection TERM INT
    collect_one_sample
    warn_if_queries_failing

    if [[ "$FOREGROUND" == "yes" ]]; then
        collection_loop 1
        echo "Done. Output is in ${OUTDIR}"
    else
        ( trap '' HUP; collection_loop 1 ) < /dev/null > "${OUTDIR}/collector.log" 2>&1 &
        echo "$!" > "${OUTDIR}/collector.pid"
        disown
        echo "Collecting in the background. Stop with: kill \$(cat ${OUTDIR}/collector.pid)"
        echo "Output: ${OUTDIR}"
    fi
}

# Distro family, used only to choose the client install hint.
distro_family() {
    local id="" id_like=""
    if [[ -r /etc/os-release ]]; then
        id="$(. /etc/os-release; echo "${ID:-}")"
        id_like="$(. /etc/os-release; echo "${ID_LIKE:-}")"
    fi
    case "$id" in
        ubuntu|debian) echo "debian"; return ;;
        rhel|rocky|almalinux|ol|centos|fedora|amzn|mariner|azurelinux) echo "rhel"; return ;;
        sles|sled|opensuse*|suse) echo "suse"; return ;;
    esac
    case "$id_like" in
        *debian*)        echo "debian" ;;
        *rhel*|*fedora*) echo "rhel" ;;
        *suse*)          echo "suse" ;;
        *)               echo "unknown" ;;
    esac
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [-d duration | -c count] [-i interval] [-o outdir]
                      [--host host] [--port port] [--user user]
                      [--database dbname] [--foreground] [-h] [-V]

  -d, --duration    Total run time: 30, 30s, 15m, 2h, 1d. A bare number is
                    seconds. Prompted for if omitted and a terminal is present
  -c, --count       Number of samples. Mutually exclusive with -d
  -i, --interval    Sample interval: 15, 15s, 1m. A bare number is seconds.
                    Prompted for if omitted and a terminal is present
  -o, --outdir      Base output directory. Default: /var/log/perf-diag, or
                    ~/perf-diag when that is not writable
      --host        PostgreSQL host         (default: localhost)
      --port        PostgreSQL port         (default: 5432)
      --user        PostgreSQL user         (default: current user)
      --database    Database name           (default: postgres)
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Authentication: uses the standard libpq environment variables and ~/.pgpass.
Set PGPASSWORD or configure ~/.pgpass before running if the connection needs
a password. A password is never accepted as an option.

Output goes to <outdir>/<host>_postgresql_<timestamp>/ and holds pg_stats.csv,
manifest.txt, errors.log and (detached runs) collector.pid and collector.log.
Stop a detached run with: kill \$(cat <outdir>/collector.pid)

A detached run survives SSH logout. It does not survive systemd-logind
KillUserProcesses=yes, which is off by default on Fedora, RHEL, Debian and
Ubuntu.

Example:
  ${SCRIPT_NAME} -d 2h -i 30s --host db1.example.com --user monitor
EOF
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
DURATION=""
COUNT=""
INTERVAL=""
OUTDIR_BASE=""
PG_HOST="localhost"
PG_PORT="5432"
PG_USER="$(id -un)"
PG_DATABASE="postgres"
FOREGROUND="no"
OPT_SHIFT=1

while [[ $# -gt 0 ]]; do
    arg="$1"
    has_next="no"
    if [[ $# -ge 2 ]]; then
        has_next="yes"
    fi
    case "$arg" in
        -d|--duration|--duration=*) take_value "$arg" "${2-}" "$has_next"; DURATION="$OPT_VALUE" ;;
        -c|--count|--count=*)       take_value "$arg" "${2-}" "$has_next"; COUNT="$OPT_VALUE" ;;
        -i|--interval|--interval=*) take_value "$arg" "${2-}" "$has_next"; INTERVAL="$OPT_VALUE" ;;
        -o|--outdir|--outdir=*)     take_value "$arg" "${2-}" "$has_next"; OUTDIR_BASE="$OPT_VALUE" ;;
        --host|--host=*)            take_value "$arg" "${2-}" "$has_next"; PG_HOST="$OPT_VALUE" ;;
        --port|--port=*)            take_value "$arg" "${2-}" "$has_next"; PG_PORT="$OPT_VALUE" ;;
        --user|--user=*)            take_value "$arg" "${2-}" "$has_next"; PG_USER="$OPT_VALUE" ;;
        --database|--database=*)    take_value "$arg" "${2-}" "$has_next"; PG_DATABASE="$OPT_VALUE" ;;
        --foreground) FOREGROUND="yes"; OPT_SHIFT=1 ;;
        -h|--help)
            # -h used to take a host. Refuse "-h <value>" instead of printing help.
            if [[ "$arg" == "-h" && "$has_next" == "yes" && "${2-}" != -* ]]; then
                usage_error "-h now means help; use --host to set the host"
            fi
            usage; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        # Options from earlier versions: name the replacement.
        -p) usage_error "-p was removed; use --port" ;;
        -U) usage_error "-U was removed; use --user" ;;
        -w) usage_error "-w was removed; use --database" ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi
if [[ ! "$PG_PORT" =~ ^[0-9]+$ ]] || (( PG_PORT < 1 || PG_PORT > 65535 )); then
    usage_error "--port must be a number from 1 to 65535"
fi

resolve_timing

# ---------------------------------------------------------------------------
# Client and connection
# ---------------------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: 'psql' not found." >&2
    echo "Install the PostgreSQL client, then re-run this script:" >&2
    case "$(distro_family)" in
        debian) echo "  sudo apt-get update && sudo apt-get install -y postgresql-client" >&2 ;;
        rhel)   echo "  sudo dnf install -y postgresql   # package may be postgresqlNN on RHEL/Rocky/Alma, e.g. postgresql16" >&2 ;;
        suse)   echo "  sudo zypper install -y postgresql" >&2 ;;
        *)      echo "  Install the postgresql client package for your distro." >&2 ;;
    esac
    exit 1
fi

PSQL_ARGS=(-h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE")

CONN_ERR_FILE="$(mktemp)"
trap 'rm -f "$CONN_ERR_FILE"' EXIT
if ! PGCONNECT_TIMEOUT=5 psql "${PSQL_ARGS[@]}" -c '\q' 2>"$CONN_ERR_FILE"; then
    echo "ERROR: cannot connect to PostgreSQL at ${PG_HOST}:${PG_PORT}/${PG_DATABASE} as ${PG_USER}." >&2
    cat "$CONN_ERR_FILE" >&2
    echo "Check the host, port, user and database, and that PGPASSWORD or ~/.pgpass is set correctly." >&2
    exit 1
fi
rm -f "$CONN_ERR_FILE"
trap - EXIT

SERVER_VERSION_NUM="$(psql "${PSQL_ARGS[@]}" -At -c 'SHOW server_version_num' 2>/dev/null || true)"
SERVER_VERSION="$(psql "${PSQL_ARGS[@]}" -At -c 'SHOW server_version' 2>/dev/null || true)"
CLIENT_VERSION="$(psql --version)"
if [[ ! "$SERVER_VERSION_NUM" =~ ^[0-9]+$ ]]; then
    die "could not read server_version_num from ${PG_HOST}:${PG_PORT}"
fi

# PostgreSQL 17 moved the checkpoint counters from pg_stat_bgwriter to
# pg_stat_checkpointer. The CSV column names stay the same on every version.
if (( SERVER_VERSION_NUM >= 170000 )); then
    CHECKPOINT_COLUMNS="c.num_timed, c.num_requested, c.buffers_written, b.buffers_clean"
    CHECKPOINT_TABLES="pg_stat_bgwriter b, pg_stat_checkpointer c"
else
    CHECKPOINT_COLUMNS="b.checkpoints_timed, b.checkpoints_req, b.buffers_checkpoint, b.buffers_clean"
    CHECKPOINT_TABLES="pg_stat_bgwriter b"
fi

# Idle sessions sit in the ClientRead wait event, so waiting is counted for
# active sessions only.
QUERY="
SELECT
  d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit,
  d.tup_returned, d.tup_fetched, d.tup_inserted, d.tup_updated, d.tup_deleted,
  d.temp_files, d.temp_bytes, d.deadlocks,
  ${CHECKPOINT_COLUMNS},
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'active'),
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle'),
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND wait_event IS NOT NULL)
FROM pg_stat_database d, ${CHECKPOINT_TABLES}
WHERE d.datname = current_database();
"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
resolve_output_base
make_run_dir
OUTFILE="${OUTDIR}/pg_stats.csv"

manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
manifest_add target "${PG_USER}@${PG_HOST}:${PG_PORT}/${PG_DATABASE}"
manifest_add client_version "$CLIENT_VERSION"
manifest_add server_version "${SERVER_VERSION:-unknown}"
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: PostgreSQL ${SERVER_VERSION:-unknown}"

echo "ts,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,bgwriter_checkpoints_timed,bgwriter_checkpoints_req,bgwriter_buffers_checkpoint,bgwriter_buffers_clean,active_connections,idle_connections,waiting_connections" > "$OUTFILE"

# The timestamp is the collector's clock, not the server's.
collect_one_sample() {
    local ts row
    ts="$(date -Iseconds)"
    row="$(PGCONNECT_TIMEOUT=5 psql "${PSQL_ARGS[@]}" -At -F',' -c "$QUERY" 2>>"$ERROR_LOG" || true)"
    if [[ -n "$row" ]]; then
        echo "${ts},${row}" >> "$OUTFILE"
    fi
}

run_collection
