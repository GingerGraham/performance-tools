#!/usr/bin/env bash
#
# collect-postgresql-metrics.sh
# Samples pg_stat_database, pg_stat_bgwriter (pg_stat_checkpointer on 17+) and
# pg_stat_activity at a fixed interval for a fixed duration, matching the
# OS-level sadc/pidstat window.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="3.0.0"
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
# INTERVAL_SECS, SAMPLE_COUNT, and a collect_one_sample function that makes one
# client call per sample, hands the output to route_sample_output and sends
# query errors to "${OUTDIR}/errors.log".
# ---------------------------------------------------------------------------
shopt -s extglob

DEFAULT_OUTDIR_BASE="/var/log/perf-diag"
COLLECTOR_SCHEMA="3"

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
        note "${DEFAULT_OUTDIR_BASE} is not writable; using ${home_base}. Run with sudo or pass -o <dir> to choose another location. gather-perf-diag.sh finds runs under ~/perf-diag of the invoking user, SUDO_USER and the database service accounts"
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

# A sample may take at most this long, so one hung query cannot hold the
# schedule. Call set_query_timeout after resolve_timing.
QUERY_TIMEOUT_S=1

set_query_timeout() {
    if (( INTERVAL_SECS > 6 )); then
        QUERY_TIMEOUT_S=$(( INTERVAL_SECS - 2 ))
    elif (( INTERVAL_SECS > 2 )); then
        QUERY_TIMEOUT_S=$(( INTERVAL_SECS - 1 ))
    else
        QUERY_TIMEOUT_S=1
    fi
}

run_with_timeout() {
    timeout --kill-after=2 "${QUERY_TIMEOUT_S}" "$@"
}

# Logs a failed client call. $1 = timestamp, $2 = exit status of the call.
# Status 124 is timeout(1) ending the call; 137 is the kill that follows when
# the client ignored the first signal.
note_sample_failure() {
    local ts="$1" status="$2"
    if (( status == 124 || status == 137 )); then
        echo "${ts} sample timed out after ${QUERY_TIMEOUT_S} s" >> "$ERROR_LOG"
    elif (( status != 0 )); then
        echo "${ts} client exited with status ${status}" >> "$ERROR_LOG"
    fi
}

# Counts the fields in one CSV row. Double-quoted fields may hold commas, and a
# doubled quote inside one is a literal quote. Removing the doubled quotes and
# then each quoted string leaves only the commas that separate fields.
csv_field_count() {
    local row="$1" separators
    row="${row//\"\"/}"
    row="${row//\"*([^\"])\"/}"
    separators="${row//[^,]/}"
    echo $(( ${#separators} + 1 ))
}

# One output file per record letter. RECORD_FILES maps the letter to a CSV
# path. RECORD_FIELDS holds the field count of that file without the leading
# timestamp. DATA_FILES lists the file names for the manifest.
declare -A RECORD_FILES=()
declare -A RECORD_FIELDS=()
DATA_FILES=()

# Creates a data file with its header line and registers it for a record
# letter. $1 = letter, $2 = file name, $3 = header line (starts with ts).
register_record() {
    local letter="$1" name="$2" header="$3" commas
    commas="${header//[^,]/}"
    RECORD_FILES[$letter]="${OUTDIR}/${name}"
    RECORD_FIELDS[$letter]="${#commas}"
    echo "$header" > "${OUTDIR}/${name}"
    DATA_FILES+=("$name")
}

# Splits one sample's client output by its first field and appends each row,
# with the sample timestamp in front, to the file registered for that record
# letter. A row with an unknown letter or the wrong number of fields goes to
# errors.log instead of the CSV.
route_sample_output() {
    local ts="$1" output="$2" line record rest field_count
    while IFS= read -r line; do
        line="${line%$'\r'}"
        if [[ -z "$line" ]]; then
            continue
        fi
        record="${line%%,*}"
        rest=""
        if [[ "$line" == *,* ]]; then
            rest="${line#*,}"
        fi
        if [[ -z "${RECORD_FILES[$record]+set}" ]]; then
            echo "${ts} unexpected output: ${line}" >> "$ERROR_LOG"
            continue
        fi
        field_count="$(csv_field_count "$rest")"
        if [[ "$field_count" != "${RECORD_FIELDS[$record]}" ]]; then
            echo "${ts} ${record} row has ${field_count} fields, expected ${RECORD_FIELDS[$record]}: ${line}" >> "$ERROR_LOG"
            continue
        fi
        echo "${ts},${rest}" >> "${RECORD_FILES[$record]}"
    done <<< "$output"
}

# Sets TARGET_LOCAL (yes|no|unknown) and TARGET_LOCAL_BASIS.
# $1 = host as given (may be empty or a socket directory), $2 = host name the
# server reports about itself (may be empty).
TARGET_LOCAL="unknown"
TARGET_LOCAL_BASIS="none"

local_addresses() {
    ip -o addr show 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4}'
}

resolve_target_locality() {
    local host="$1" server_host="$2" address
    TARGET_LOCAL="no"
    TARGET_LOCAL_BASIS="none"
    if [[ -z "$host" || "$host" == /* ]]; then
        TARGET_LOCAL="yes"
        TARGET_LOCAL_BASIS="socket"
        return
    fi
    case "$host" in
        localhost|localhost.*|127.*|::1|"[::1]")
            TARGET_LOCAL="yes"
            TARGET_LOCAL_BASIS="loopback"
            return ;;
    esac
    if [[ -n "$server_host" && "${server_host%%.*}" == "$(short_hostname)" ]]; then
        TARGET_LOCAL="yes"
        TARGET_LOCAL_BASIS="server hostname"
        return
    fi
    for address in $(getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
        if local_addresses | grep -qxF "$address"; then
            TARGET_LOCAL="yes"
            TARGET_LOCAL_BASIS="local address"
            return
        fi
    done
    if [[ -z "$server_host" ]]; then
        TARGET_LOCAL="unknown"
    fi
}

# Writes the manifest keys every DB collector records after manifest_start.
# $1 = engine (postgresql|mysql|mariadb|oracle|mssql), $2 = display id without
# secrets, $3 = host name the server reports, $4 = server start time as text,
# $5 = server version as an integer.
manifest_add_db_identity() {
    local engine="$1" instance="$2" server_host="$3" server_start="$4" version_num="$5"
    manifest_add collector_schema "$COLLECTOR_SCHEMA"
    manifest_add db_engine "$engine"
    manifest_add db_instance "$instance"
    manifest_add server_version_num "$version_num"
    manifest_add server_hostname "$server_host"
    manifest_add target_local "$TARGET_LOCAL"
    manifest_add target_local_basis "$TARGET_LOCAL_BASIS"
    manifest_add server_start "$server_start"
    manifest_add query_timeout_s "$QUERY_TIMEOUT_S"
    manifest_add files "${DATA_FILES[*]}"
}

# Records one server setting in the manifest. Newlines are removed; settings
# are not secrets.
record_setting() {
    local name="$1" value="$2"
    value="${value//$'\n'/ }"
    manifest_add "setting.${name}" "$value"
}

# db_paths.txt holds where the database keeps its files. It is only written
# when the server is on this host, because a remote server's paths mean
# nothing here. Roles: data, wal, redo, log, temp, tablespace, asm_disk.
MAX_PATHS_PER_ROLE=50
declare -A DB_PATH_COUNT=()
declare -A DB_PATH_SEEN=()

# $1 = role, $2 = path, $3 = optional note about how the path was found.
add_db_path() {
    local role="$1" path="$2" path_note="${3-}" count path_key
    if [[ "$TARGET_LOCAL" != "yes" || -z "$path" ]]; then
        return
    fi
    path_key="${role}|${path}"
    count="${DB_PATH_COUNT[$role]-0}"
    if (( count >= MAX_PATHS_PER_ROLE )) || [[ -n "${DB_PATH_SEEN[$path_key]+set}" ]]; then
        return
    fi
    count=$(( count + 1 ))
    DB_PATH_COUNT[$role]="$count"
    DB_PATH_SEEN[$path_key]="yes"
    echo "path.${role}.${count}=${path}" >> "${OUTDIR}/db_paths.txt"
    if [[ -n "$path_note" ]]; then
        echo "path.${role}.${count}.note=${path_note}" >> "${OUTDIR}/db_paths.txt"
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
      --host        PostgreSQL host or socket directory
                    (default: the local Unix socket; pass --host localhost
                    for TCP)
      --port        PostgreSQL port         (default: 5432)
      --user        PostgreSQL user         (default: current user)
      --database    Database to connect to  (default: postgres). Counters
                    cover every database in the cluster, whichever database
                    this is
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Authentication: uses the standard libpq environment variables and ~/.pgpass.
Set PGPASSWORD or configure ~/.pgpass before running if the connection needs
a password. A password is never accepted as an option.

Privileges: on PostgreSQL 10 and later, GRANT pg_monitor TO <login> covers
every query. On 9.2 to 9.6 use a superuser, or accept that other users'
state and wait columns are empty.

Output goes to <outdir>/<host>_postgresql_<timestamp>/ and holds pg_stats.csv,
pg_database.csv, pg_waits.csv (9.6 and later), db_paths.txt (server on this
host), manifest.txt, errors.log and (detached runs) collector.pid and
collector.log. Stop a detached run with: kill \$(cat <outdir>/collector.pid)

Each sample is limited to a timeout just under the interval, so one hung query
cannot hold the schedule.

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
PG_HOST=""
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
set_query_timeout

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

# -X ignores ~/.psqlrc, whose \timing or \pset settings would change the
# output. With no -h, libpq uses the local Unix socket.
PSQL_ARGS=(-X -q -A -t -F',' -v ON_ERROR_STOP=1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE")
if [[ -n "$PG_HOST" ]]; then
    PSQL_ARGS+=(-h "$PG_HOST")
fi
export PGCONNECT_TIMEOUT=5
export PGOPTIONS="-c statement_timeout=$(( QUERY_TIMEOUT_S * 1000 ))"
export PGAPPNAME="perf-diag-collector"

# Runs one statement and prints its rows. $1 = SQL.
pg_query() {
    psql "${PSQL_ARGS[@]}" -c "$1"
}

# Same, with a tab between fields, for values that can hold commas.
pg_query_tabs() {
    psql "${PSQL_ARGS[@]}" -F $'\t' -c "$1"
}

PG_TARGET_TEXT="${PG_HOST:-local socket}:${PG_PORT}/${PG_DATABASE}"
CONN_ERR_FILE="$(mktemp)"
trap 'rm -f "$CONN_ERR_FILE"' EXIT
if ! psql "${PSQL_ARGS[@]}" -c '\q' 2>"$CONN_ERR_FILE"; then
    echo "ERROR: cannot connect to PostgreSQL at ${PG_TARGET_TEXT} as ${PG_USER}." >&2
    cat "$CONN_ERR_FILE" >&2
    echo "Check the host, port, user and database, and that PGPASSWORD or ~/.pgpass is set correctly." >&2
    exit 1
fi
rm -f "$CONN_ERR_FILE"
trap - EXIT

SERVER_VERSION_NUM="$(pg_query 'SHOW server_version_num' 2>/dev/null || true)"
SERVER_VERSION="$(pg_query 'SHOW server_version' 2>/dev/null || true)"
CLIENT_VERSION="$(psql --version)"
if [[ ! "$SERVER_VERSION_NUM" =~ ^[0-9]+$ ]]; then
    die "could not read server_version_num from ${PG_TARGET_TEXT}"
fi
if (( SERVER_VERSION_NUM < 90200 )); then
    die "PostgreSQL 9.2 or later is needed (server is ${SERVER_VERSION:-unknown})"
fi

# ---------------------------------------------------------------------------
# Version gates. Each one sets SQL fragments named for what they are, so the
# sample queries below read the same on every version.
# ---------------------------------------------------------------------------

# Checkpoint counters moved from pg_stat_bgwriter to pg_stat_checkpointer in 17.
# The CSV column names stay the same on every version.
if (( SERVER_VERSION_NUM >= 170000 )); then
    CHECKPOINT_COLUMNS="c.num_timed, c.num_requested, c.buffers_written, b.buffers_clean"
    CHECKPOINT_TIME_COLUMNS="c.write_time, c.sync_time"
    CHECKPOINT_TABLES="pg_stat_bgwriter b, pg_stat_checkpointer c"
else
    CHECKPOINT_COLUMNS="b.checkpoints_timed, b.checkpoints_req, b.buffers_checkpoint, b.buffers_clean"
    CHECKPOINT_TIME_COLUMNS="b.checkpoint_write_time, b.checkpoint_sync_time"
    CHECKPOINT_TABLES="pg_stat_bgwriter b"
fi

# Backend buffer writes come from pg_stat_io from 16.
if (( SERVER_VERSION_NUM >= 160000 )); then
    BACKEND_WRITE_COLUMNS="(SELECT coalesce(sum(writes), 0) FROM pg_stat_io WHERE backend_type = 'client backend' AND object = 'relation'), (SELECT coalesce(sum(fsyncs), 0) FROM pg_stat_io WHERE backend_type = 'client backend' AND object = 'relation')"
else
    BACKEND_WRITE_COLUMNS="b.buffers_backend, b.buffers_backend_fsync"
fi

# pg_stat_wal exists from 14.
if (( SERVER_VERSION_NUM >= 140000 )); then
    WAL_STATS_COLUMNS="(SELECT wal_records FROM pg_stat_wal), (SELECT wal_fpi FROM pg_stat_wal), (SELECT wal_bytes FROM pg_stat_wal), (SELECT wal_buffers_full FROM pg_stat_wal)"
else
    WAL_STATS_COLUMNS="NULL, NULL, NULL, NULL"
fi

# backend_type, the WAL function names and pg_stat_replication.replay_lag are 10 and later.
if (( SERVER_VERSION_NUM >= 100000 )); then
    CLIENT_BACKEND_FILTER="backend_type = 'client backend'"
    AUTOVACUUM_COUNT_EXPR="(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'autovacuum worker')"
    WAL_LSN_EXPR="CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') END"
    REPLICATION_LAG_EXPR="CASE WHEN pg_is_in_recovery() THEN extract(epoch from now() - pg_last_xact_replay_timestamp()) ELSE (SELECT max(extract(epoch from replay_lag)) FROM pg_stat_replication) END"
else
    CLIENT_BACKEND_FILTER="query NOT LIKE 'autovacuum:%'"
    AUTOVACUUM_COUNT_EXPR="(SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'autovacuum:%')"
    WAL_LSN_EXPR="CASE WHEN pg_is_in_recovery() THEN NULL ELSE pg_xlog_location_diff(pg_current_xlog_location(), '0/0') END"
    REPLICATION_LAG_EXPR="CASE WHEN pg_is_in_recovery() THEN extract(epoch from now() - pg_last_xact_replay_timestamp()) ELSE NULL END"
fi
# Leave out the collector's own sessions, however they were opened.
CLIENT_BACKEND_FILTER="${CLIENT_BACKEND_FILTER} AND pid <> pg_backend_pid() AND coalesce(application_name, '') <> '${PGAPPNAME}'"

# Wait events exist from 9.6. Before that, pg_stat_activity has a waiting flag.
if (( SERVER_VERSION_NUM >= 90600 )); then
    WAITING_EXPR="wait_event IS NOT NULL"
else
    WAITING_EXPR="waiting"
fi

# ---------------------------------------------------------------------------
# Sample queries. Statements run one after another in one psql call. Every row
# starts with a record letter: S summary, W waits, D databases.
# ---------------------------------------------------------------------------
SUMMARY_QUERY="
SELECT 'S',
  d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit,
  d.tup_returned, d.tup_fetched, d.tup_inserted, d.tup_updated, d.tup_deleted,
  d.temp_files, d.temp_bytes, d.deadlocks,
  ${CHECKPOINT_COLUMNS},
  a.active, a.idle, a.waiting,
  d.blk_read_time_ms, d.blk_write_time_ms,
  ${CHECKPOINT_TIME_COLUMNS},
  ${BACKEND_WRITE_COLUMNS},
  b.maxwritten_clean, b.buffers_alloc,
  a.idle_in_xact, a.total, current_setting('max_connections')::int,
  a.oldest_xact_s, a.oldest_query_s,
  (SELECT max(age(datfrozenxid)) FROM pg_database),
  ${AUTOVACUUM_COUNT_EXPR},
  ${WAL_STATS_COLUMNS},
  ${WAL_LSN_EXPR},
  pg_is_in_recovery()::int,
  ${REPLICATION_LAG_EXPR},
  extract(epoch from pg_postmaster_start_time())::bigint,
  d.stats_reset_marker
FROM
  (SELECT coalesce(sum(xact_commit), 0) AS xact_commit, coalesce(sum(xact_rollback), 0) AS xact_rollback,
          coalesce(sum(blks_read), 0) AS blks_read, coalesce(sum(blks_hit), 0) AS blks_hit,
          coalesce(sum(tup_returned), 0) AS tup_returned, coalesce(sum(tup_fetched), 0) AS tup_fetched,
          coalesce(sum(tup_inserted), 0) AS tup_inserted, coalesce(sum(tup_updated), 0) AS tup_updated,
          coalesce(sum(tup_deleted), 0) AS tup_deleted, coalesce(sum(temp_files), 0) AS temp_files,
          coalesce(sum(temp_bytes), 0) AS temp_bytes, coalesce(sum(deadlocks), 0) AS deadlocks,
          sum(blk_read_time) AS blk_read_time_ms, sum(blk_write_time) AS blk_write_time_ms,
          extract(epoch from max(stats_reset))::bigint AS stats_reset_marker
   FROM pg_stat_database) d,
  (SELECT coalesce(sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END), 0) AS active,
          coalesce(sum(CASE WHEN state = 'idle' THEN 1 ELSE 0 END), 0) AS idle,
          coalesce(sum(CASE WHEN state = 'active' AND ${WAITING_EXPR} THEN 1 ELSE 0 END), 0) AS waiting,
          coalesce(sum(CASE WHEN state IN ('idle in transaction', 'idle in transaction (aborted)') THEN 1 ELSE 0 END), 0) AS idle_in_xact,
          count(*) AS total,
          round(max(extract(epoch from now() - xact_start))::numeric, 1) AS oldest_xact_s,
          round(max(CASE WHEN state = 'active' THEN extract(epoch from now() - query_start) END)::numeric, 1) AS oldest_query_s
   FROM pg_stat_activity
   WHERE ${CLIENT_BACKEND_FILTER}) a,
  ${CHECKPOINT_TABLES};
"

# CPU means active with no wait event: on CPU, or in code that is not
# instrumented. These are snapshots at the sample instant, not averages.
WAITS_QUERY="
SELECT 'W',
  '\"' || replace(coalesce(wait_event_type, 'CPU'), '\"', '\"\"') || '\"',
  '\"' || replace(coalesce(wait_event, 'CPU'), '\"', '\"\"') || '\"',
  count(*)
FROM pg_stat_activity
WHERE state = 'active' AND ${CLIENT_BACKEND_FILTER}
GROUP BY 2, 3;
"

DATABASE_QUERY="
SELECT 'D', '\"' || replace(datname, '\"', '\"\"') || '\"', numbackends,
  xact_commit, xact_rollback, blks_read, blks_hit,
  tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
  temp_files, temp_bytes, deadlocks, blk_read_time, blk_write_time
FROM pg_stat_database
WHERE datname IS NOT NULL;
"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
resolve_output_base
make_run_dir

SUMMARY_HEADER="ts,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,bgwriter_checkpoints_timed,bgwriter_checkpoints_req,bgwriter_buffers_checkpoint,bgwriter_buffers_clean,active_connections,idle_connections,waiting_connections,blk_read_time_ms,blk_write_time_ms,checkpoint_write_time_ms,checkpoint_sync_time_ms,backend_buffer_writes,backend_buffer_fsyncs,maxwritten_clean,buffers_alloc,idle_in_transaction_connections,total_connections,max_connections,oldest_xact_age_s,oldest_active_query_age_s,xid_age_max,autovacuum_workers,wal_records,wal_fpi,wal_bytes,wal_buffers_full,wal_lsn_bytes,in_recovery,replication_lag_s,server_start_marker,stats_reset_marker"
DATABASE_HEADER="ts,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,blk_read_time_ms,blk_write_time_ms"
WAITS_HEADER="ts,wait_event_type,wait_event,sessions"

register_record S pg_stats.csv "$SUMMARY_HEADER"
register_record D pg_database.csv "$DATABASE_HEADER"
SAMPLE_SQL="${SUMMARY_QUERY}${DATABASE_QUERY}"
if (( SERVER_VERSION_NUM >= 90600 )); then
    register_record W pg_waits.csv "$WAITS_HEADER"
    SAMPLE_SQL="${SAMPLE_SQL}${WAITS_QUERY}"
else
    note "PostgreSQL ${SERVER_VERSION:-unknown} has no wait events (9.6 and later), so pg_waits.csv is not written"
fi
SAMPLE_SQL_FILE="${OUTDIR}/sample.sql"
printf '%s' "$SAMPLE_SQL" > "$SAMPLE_SQL_FILE"
OUTFILE="${OUTDIR}/pg_stats.csv"

# ---------------------------------------------------------------------------
# Identity, settings and file locations
# ---------------------------------------------------------------------------
manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
manifest_add target "${PG_USER}@${PG_TARGET_TEXT}"
manifest_add client_version "$CLIENT_VERSION"
manifest_add server_version "${SERVER_VERSION:-unknown}"

# inet_server_addr() is NULL on a socket connection. PostgreSQL has no SQL
# function for its host name, so locality rests on the socket, loopback and
# local-address checks.
SERVER_ADDRESS="$(pg_query "SELECT coalesce(host(inet_server_addr()), '')" 2>/dev/null || true)"
SERVER_START_TEXT="$(pg_query 'SELECT pg_postmaster_start_time()' 2>/dev/null || true)"
resolve_target_locality "$PG_HOST" ""

if [[ -z "$PG_HOST" || "$PG_HOST" == /* ]]; then
    SOCKET_DIRECTORY="$PG_HOST"
    if [[ -z "$SOCKET_DIRECTORY" ]]; then
        SOCKET_DIRECTORY="$(pg_query "SELECT split_part(setting, ',', 1) FROM pg_settings WHERE name = 'unix_socket_directories'" 2>/dev/null || true)"
    fi
    PG_INSTANCE_TEXT="${SOCKET_DIRECTORY:-socket}:${PG_PORT}"
else
    PG_INSTANCE_TEXT="${PG_HOST}:${PG_PORT}"
fi
manifest_add_db_identity postgresql "$PG_INSTANCE_TEXT" "$SERVER_ADDRESS" "$SERVER_START_TEXT" "$SERVER_VERSION_NUM"

# Memory settings are shown as sizes (128 MB) instead of a count of pages.
SETTING_NAMES="'block_size','shared_buffers','max_connections','track_io_timing','checkpoint_timeout','max_wal_size','checkpoint_segments','checkpoint_completion_target','synchronous_commit','synchronous_standby_names','wal_sync_method','full_page_writes','effective_io_concurrency','work_mem','temp_tablespaces','data_directory'"
SETTINGS_QUERY="
SELECT name,
  CASE WHEN setting IS NULL THEN NULL
       WHEN unit = '8kB' THEN pg_size_pretty(setting::bigint * 8192)
       WHEN unit = 'kB'  THEN pg_size_pretty(setting::bigint * 1024)
       WHEN unit = 'MB'  THEN pg_size_pretty(setting::bigint * 1048576)
       WHEN unit = 'B'   THEN pg_size_pretty(setting::bigint)
       WHEN unit IS NULL THEN setting
       ELSE setting || unit END
FROM pg_settings WHERE name IN (${SETTING_NAMES});
"
declare -A PG_SETTING=()
while IFS=$'\t' read -r setting_name setting_value; do
    if [[ -n "$setting_name" ]]; then
        PG_SETTING[$setting_name]="$setting_value"
        if [[ -n "$setting_value" ]]; then
            record_setting "$setting_name" "$setting_value"
        fi
    fi
done < <(pg_query_tabs "$SETTINGS_QUERY" 2>>"$ERROR_LOG" || true)

DATA_DIRECTORY="${PG_SETTING[data_directory]-}"
if [[ -z "$DATA_DIRECTORY" ]]; then
    note "data_directory is not readable for this login (grant pg_monitor); database files cannot be placed on devices"
fi
if [[ "${PG_SETTING[track_io_timing]-}" == "off" ]]; then
    note "track_io_timing is off, so read and write latency is not measured. Enable with ALTER SYSTEM SET track_io_timing = on; SELECT pg_reload_conf(); after checking timer overhead with pg_test_timing"
fi
MONITOR_ROLE_CHECK="false"
if (( SERVER_VERSION_NUM >= 100000 )); then
    MONITOR_ROLE_CHECK="pg_has_role(current_user, 'pg_monitor', 'member')"
fi
LOGIN_CAN_MONITOR="$(pg_query "SELECT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) OR ${MONITOR_ROLE_CHECK}" 2>/dev/null || true)"
if [[ "$LOGIN_CAN_MONITOR" == "f" ]]; then
    note "login ${PG_USER} is not a superuser and lacks pg_monitor, so other users' sessions are not visible and connection and wait figures under-report"
fi

if [[ "$TARGET_LOCAL" == "yes" && -n "$DATA_DIRECTORY" ]]; then
    add_db_path data "$DATA_DIRECTORY"
    if (( SERVER_VERSION_NUM >= 100000 )); then
        WAL_DIRECTORY_NAME="pg_wal"
    else
        WAL_DIRECTORY_NAME="pg_xlog"
    fi
    # The data directory is mode 0700, so a monitoring login usually cannot
    # resolve the symlink. Then the unresolved path is recorded with a note.
    if RESOLVED_WAL="$(readlink -f "${DATA_DIRECTORY}/${WAL_DIRECTORY_NAME}" 2>/dev/null)" && [[ -d "$RESOLVED_WAL" ]]; then
        add_db_path wal "$RESOLVED_WAL"
    else
        add_db_path wal "${DATA_DIRECTORY}/${WAL_DIRECTORY_NAME}" "assumed inside the data directory; the ${WAL_DIRECTORY_NAME} symlink could not be read"
    fi
    while IFS= read -r tablespace_path; do
        add_db_path tablespace "$tablespace_path"
    done < <(pg_query "SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE pg_tablespace_location(oid) <> ''" 2>/dev/null || true)
    TEMP_TABLESPACE_NAME="${PG_SETTING[temp_tablespaces]-}"
    TEMP_TABLESPACE_NAME="${TEMP_TABLESPACE_NAME%%,*}"
    TEMP_PATH=""
    if [[ -n "$TEMP_TABLESPACE_NAME" ]]; then
        TEMP_PATH="$(pg_query "SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = '${TEMP_TABLESPACE_NAME//\'/\'\'}'" 2>/dev/null || true)"
    fi
    add_db_path temp "${TEMP_PATH:-${DATA_DIRECTORY}/base/pgsql_tmp}"
fi
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: PostgreSQL ${SERVER_VERSION:-unknown} (${PG_INSTANCE_TEXT}, on this host: ${TARGET_LOCAL})"

# The timestamp is the collector's clock, not the server's.
collect_one_sample() {
    local ts output status=0
    ts="$(date -Iseconds)"
    output="$(run_with_timeout psql "${PSQL_ARGS[@]}" -f "$SAMPLE_SQL_FILE" 2>>"$ERROR_LOG")" || status=$?
    route_sample_output "$ts" "$output"
    note_sample_failure "$ts" "$status"
}

run_collection
