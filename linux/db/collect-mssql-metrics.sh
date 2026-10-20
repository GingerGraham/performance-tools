#!/usr/bin/env bash
#
# collect-mssql-metrics.sh
# Samples SQL Server performance counters (sys.dm_os_performance_counters),
# wait statistics and per-file IO through sqlcmd at a fixed interval for a
# fixed duration, matching the OS-level sadc/pidstat window.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="3.0.0"
COLLECTOR_TAG="mssql"
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
    cat <<USAGE
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
      --host        Server, passed to sqlcmd -S: host, host,port or
                    host\\instance               (default: localhost)
      --port        Port, appended to --host as host,port
      --user        SQL login user. Omit to use trusted / AD authentication
      --database    Database name                (default: master). The
                    counters cover the whole server, whichever database this is
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Authentication: set the SQLCMDPASSWORD environment variable for SQL logins.
A password is never accepted as an option.

Encryption: when sqlcmd supports it (mssql-tools18, go-sqlcmd) the script adds
-C -N. The connection is encrypted, but the server certificate is trusted
without being checked (manifest: tls_trust_server_certificate=yes), so it does
not protect against a man in the middle. Older sqlcmd builds do not accept
these switches; the script then connects without them and records
tls_trust_server_certificate=no.

Privileges: SQL Server 2019 and earlier: GRANT VIEW SERVER STATE TO <login>.
SQL Server 2022 and later: GRANT VIEW SERVER PERFORMANCE STATE TO <login>
(VIEW SERVER STATE also works). The file IO record also needs
VIEW ANY DEFINITION, or sys.master_files shows no files and mssql_files.csv
stays empty.

Output goes to <outdir>/<host>_mssql_<timestamp>/ and holds mssql_stats.csv,
mssql_waits.csv, mssql_files.csv, db_paths.txt (server on this host),
manifest.txt, errors.log and (detached runs) collector.pid and collector.log.
Stop a detached run with: kill \$(cat <outdir>/collector.pid)

Columns ending _total are cumulative counters since server start; work out
rates from the difference between samples. manifest.txt records the server's
own counter type for each counter column (counter_type.<column>=<cntr_type>).

Each sample is limited to a timeout just under the interval (sqlcmd -t and
timeout(1)), so one hung query cannot hold the schedule.

A detached run survives SSH logout. It does not survive systemd-logind
KillUserProcesses=yes, which is off by default on Fedora, RHEL, Debian and
Ubuntu.

Example:
  SQLCMDPASSWORD='secret' ${SCRIPT_NAME} -d 2h -i 30s --host db1 --user monitor
USAGE
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
DURATION=""
COUNT=""
INTERVAL=""
OUTDIR_BASE=""
SQL_HOST="localhost"
SQL_PORT=""
SQL_USER=""
SQL_DATABASE="master"
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
        --host|--host=*)            take_value "$arg" "${2-}" "$has_next"; SQL_HOST="$OPT_VALUE" ;;
        --port|--port=*)            take_value "$arg" "${2-}" "$has_next"; SQL_PORT="$OPT_VALUE" ;;
        --user|--user=*)            take_value "$arg" "${2-}" "$has_next"; SQL_USER="$OPT_VALUE" ;;
        --database|--database=*)    take_value "$arg" "${2-}" "$has_next"; SQL_DATABASE="$OPT_VALUE" ;;
        --foreground) FOREGROUND="yes"; OPT_SHIFT=1 ;;
        -h|--help)    usage; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        # Options from earlier versions: name the replacement.
        -S) usage_error "-S was removed; use --host" ;;
        -U) usage_error "-U was removed; use --user" ;;
        -n) usage_error "-n was removed; use --database" ;;
        -P) usage_error "-P was removed: passwords are not accepted as options. Set the SQLCMDPASSWORD environment variable" ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi
if [[ -n "$SQL_PORT" ]]; then
    if [[ ! "$SQL_PORT" =~ ^[0-9]+$ ]] || (( SQL_PORT < 1 || SQL_PORT > 65535 )); then
        usage_error "--port must be a number from 1 to 65535"
    fi
    if [[ "$SQL_HOST" == *,* || "$SQL_HOST" == *\\* ]]; then
        usage_error "--port cannot be combined with a --host that already has a port or instance name"
    fi
    SQL_SERVER="${SQL_HOST},${SQL_PORT}"
else
    SQL_SERVER="$SQL_HOST"
fi

resolve_timing
set_query_timeout

# ---------------------------------------------------------------------------
# Client and connection
# ---------------------------------------------------------------------------
SQLCMD_BIN=""
for candidate in sqlcmd /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd; do
    if command -v "$candidate" >/dev/null 2>&1; then
        SQLCMD_BIN="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$SQLCMD_BIN" ]]; then
    echo "ERROR: 'sqlcmd' not found (checked PATH, /opt/mssql-tools18/bin, /opt/mssql-tools/bin)." >&2
    echo "Microsoft's SQL tools aren't in the standard distro repos; install them from Microsoft's repo:" >&2
    echo "  https://learn.microsoft.com/sql/linux/sql-server-linux-setup-tools" >&2
    echo "Typical flow: add Microsoft's package repo for your distro, then:" >&2
    echo "  sudo ACCEPT_EULA=Y dnf install -y mssql-tools18 unixODBC-devel     # RHEL family" >&2
    echo "  sudo ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev   # Debian/Ubuntu" >&2
    echo "SLES: Microsoft does not officially publish mssql-tools for SLES; use remote collection against the SQL Server host from a supported distro instead." >&2
    exit 1
fi

# The legacy mssql-tools sqlcmd rejects -C (trust server certificate) and -N
# (encrypt). mssql-tools18 and go-sqlcmd accept them. Decide by the install
# path first, then by what the help text lists.
SQLCMD_HELP_TEXT="$("$SQLCMD_BIN" '-?' 2>&1 || true)"

sqlcmd_supports_tls_flags() {
    local real_path
    real_path="$(readlink -f "$SQLCMD_BIN" 2>/dev/null || echo "$SQLCMD_BIN")"
    case "$real_path" in
        */mssql-tools18/*) return 0 ;;
    esac
    case "$SQLCMD_HELP_TEXT" in
        *go-sqlcmd*) return 0 ;;
    esac
    # Both options listed as their own switch, e.g. "[-C ...]" and "[-N ...]".
    printf '%s\n' "$SQLCMD_HELP_TEXT" | grep -Eq '(^|[[:space:]]|\[)-C([[:space:]]|\]|$)' \
        && printf '%s\n' "$SQLCMD_HELP_TEXT" | grep -Eq '(^|[[:space:]]|\[)-N([[:space:]]|\[|\]|$)'
}

# -l 5: give up on a login after 5 s. -b: exit with a non-zero status when a
# statement fails. The password comes from SQLCMDPASSWORD, which sqlcmd reads
# itself, so it is never on the command line.
SQLCMD_ARGS=(-S "$SQL_SERVER" -d "$SQL_DATABASE" -l 5 -b)
# -C is always added when supported (S5): the connection is encrypted but the
# server certificate is not validated. The manifest says so.
TLS_TRUST_SERVER_CERTIFICATE="no"
if sqlcmd_supports_tls_flags; then
    SQLCMD_ARGS+=(-C -N)
    TLS_TRUST_SERVER_CERTIFICATE="yes"
else
    note "sqlcmd at ${SQLCMD_BIN} does not list -C/-N; connecting without them"
fi
if [[ -n "$SQL_USER" ]]; then
    SQLCMD_ARGS+=(-U "$SQL_USER")
fi

# Output format: no column headers, no padding, a comma between columns, and
# rows that are not wrapped at the default screen width of 80 characters.
SQLCMD_OUTPUT_ARGS=(-h -1 -W -w 65535 "-s,")

# Runs one batch outside the sample loop and prints its rows. $1 = SQL.
# ANSI_WARNINGS OFF stops the "Null value is eliminated by an aggregate"
# warning, which sqlcmd prints among the rows. The 30 s limit is not the
# sample timeout: that can be 1 s, too short for a start-up query.
sql_query() {
    "$SQLCMD_BIN" "${SQLCMD_ARGS[@]}" "${SQLCMD_OUTPUT_ARGS[@]}" -t 30 \
        -Q "SET NOCOUNT ON; SET ANSI_WARNINGS OFF; $1"
}

CONN_ERR_FILE="$(mktemp)"
trap 'rm -f "$CONN_ERR_FILE"' EXIT
if ! "$SQLCMD_BIN" "${SQLCMD_ARGS[@]}" -Q 'SELECT 1' >/dev/null 2>"$CONN_ERR_FILE"; then
    echo "ERROR: cannot connect to SQL Server at '${SQL_SERVER}'." >&2
    cat "$CONN_ERR_FILE" >&2
    if [[ -n "$SQL_USER" && -z "${SQLCMDPASSWORD:-}" ]]; then
        echo "A user was given but SQLCMDPASSWORD is not set." >&2
    fi
    exit 1
fi
rm -f "$CONN_ERR_FILE"
trap - EXIT

# "ProductVersion|major". ProductMajorVersion is NULL on older builds, so the
# first part of ProductVersion (15.0.4345.5 -> 15) is the fallback.
VERSION_QUERY="SELECT CONCAT(CONVERT(nvarchar(50), SERVERPROPERTY('ProductVersion')), N'|',
  COALESCE(CONVERT(int, SERVERPROPERTY('ProductMajorVersion')),
           CONVERT(int, PARSENAME(CONVERT(nvarchar(50), SERVERPROPERTY('ProductVersion')), 4))));"
VERSION_ROW="$(sql_query "$VERSION_QUERY" 2>/dev/null | sed -n '/./{p;q;}' || true)"
SERVER_VERSION="${VERSION_ROW%%|*}"
SERVER_MAJOR="${VERSION_ROW#*|}"
CLIENT_VERSION="$(printf '%s\n' "$SQLCMD_HELP_TEXT" | sed -n '/./p' | head -n 2 | tr '\n' ' ')"
if [[ ! "$SERVER_MAJOR" =~ ^[0-9]+$ ]]; then
    die "could not read the SQL Server version from ${SQL_SERVER}"
fi
# CONCAT, used by every query below, needs SQL Server 2012 (major version 11).
if (( SERVER_MAJOR < 11 )); then
    die "SQL Server 2012 or later is needed (server is ${SERVER_VERSION})"
fi
# Major version times 10000, like PostgreSQL's server_version_num:
# 15 -> 150000 (SQL Server 2019), 16 -> 160000 (2022).
SERVER_VERSION_NUM=$(( SERVER_MAJOR * 10000 ))

# SQL Server 2022 split VIEW SERVER STATE; the new permission is enough for
# the DMVs used here.
if (( SERVER_MAJOR >= 16 )); then
    SERVER_STATE_PERMISSION="VIEW SERVER PERFORMANCE STATE"
else
    SERVER_STATE_PERMISSION="VIEW SERVER STATE"
fi

# ---------------------------------------------------------------------------
# Performance counter columns. Each entry is
#   column | object | counter | instance
# "object" is the part after "SQLServer:", or after "MSSQL$NAME:" on a named
# instance, so it is matched with LIKE '%:object'. An instance of "-" means
# the counter has none; the Locks and Databases counters are read from their
# _Total instance. The order here is the column order in mssql_stats.csv,
# after buffer_cache_hit_ratio, which is a ratio of two counters and is
# built separately.
# ---------------------------------------------------------------------------
COUNTER_COLUMNS=(
    "page_life_expectancy|Buffer Manager|Page life expectancy|-"
    "checkpoint_pages_total|Buffer Manager|Checkpoint pages/sec|-"
    "lazy_writes_total|Buffer Manager|Lazy writes/sec|-"
    "memory_grants_pending|Memory Manager|Memory Grants Pending|-"
    "target_server_memory_kb|Memory Manager|Target Server Memory (KB)|-"
    "total_server_memory_kb|Memory Manager|Total Server Memory (KB)|-"
    "batch_requests_total|SQL Statistics|Batch Requests/sec|-"
    "sql_compilations_total|SQL Statistics|SQL Compilations/sec|-"
    "sql_recompilations_total|SQL Statistics|SQL Re-Compilations/sec|-"
    "user_connections|General Statistics|User Connections|-"
    "processes_blocked|General Statistics|Processes blocked|-"
    "lock_waits_total|Locks|Lock Waits/sec|_Total"
    "lock_wait_time_ms_total|Locks|Lock Wait Time (ms)|_Total"
    "deadlocks_total|Locks|Number of Deadlocks/sec|_Total"
    "log_flushes_total|Databases|Log Flushes/sec|_Total"
    "log_flush_wait_time_ms_total|Databases|Log Flush Wait Time|_Total"
    "log_bytes_flushed_total|Databases|Log Bytes Flushed/sec|_Total"
    "page_reads_total|Buffer Manager|Page reads/sec|-"
    "page_writes_total|Buffer Manager|Page writes/sec|-"
    "free_list_stalls_total|Buffer Manager|Free list stalls/sec|-"
    "full_scans_total|Access Methods|Full Scans/sec|-"
    "page_splits_total|Access Methods|Page Splits/sec|-"
    "workfiles_created_total|Access Methods|Workfiles Created/sec|-"
    "worktables_created_total|Access Methods|Worktables Created/sec|-"
    "memory_grants_outstanding|Memory Manager|Memory Grants Outstanding|-"
)

# object_name, counter_name and instance_name are padded with spaces to 128
# characters. "=" ignores the padding, LIKE does not, hence the RTRIM.
counter_match() {
    local object="$1" counter="$2" instance="$3" match
    match="RTRIM(object_name) LIKE N'%:${object}' AND counter_name = N'${counter}'"
    if [[ "$instance" != "-" ]]; then
        match="${match} AND RTRIM(instance_name) = N'${instance}'"
    fi
    echo "$match"
}

# Builds three things from the table above:
#   COUNTER_VALUE_LIST  the MAX(CASE ...) columns of the summary query. A counter
#                       the server does not have gives NULL.
#   COUNTER_FIELD_LIST  the same columns as pieces of the output row
#   COUNTER_TYPE_QUERY  one SELECT per column that prints "column|cntr_type"
SUMMARY_HEADER="ts,buffer_cache_hit_ratio"
COUNTER_VALUE_LIST=""
COUNTER_FIELD_LIST=""
COUNTER_TYPE_QUERY=""
for entry in "${COUNTER_COLUMNS[@]}"; do
    IFS='|' read -r column object counter instance <<< "$entry"
    match="$(counter_match "$object" "$counter" "$instance")"
    SUMMARY_HEADER="${SUMMARY_HEADER},${column}"
    COUNTER_VALUE_LIST="${COUNTER_VALUE_LIST},
     MAX(CASE WHEN ${match} THEN cntr_value END) AS ${column}"
    COUNTER_FIELD_LIST="${COUNTER_FIELD_LIST}, N',', c.${column}"
    COUNTER_TYPE_QUERY="${COUNTER_TYPE_QUERY}
SELECT CONCAT(N'${column}|', MAX(CASE WHEN ${match} THEN cntr_type END)) FROM sys.dm_os_performance_counters;"
done
SUMMARY_HEADER="${SUMMARY_HEADER},active_requests,runnable_tasks,work_queue_count,active_workers,busy_schedulers,server_start_marker"

# ---------------------------------------------------------------------------
# Sample queries. Statements run one after another in one sqlcmd call. Every
# row starts with a record letter: S summary, W waits, F files. The row is one
# string built with CONCAT, which treats NULL as empty, so a missing value is
# an empty field and not the word NULL. Text values are quoted here.
# ---------------------------------------------------------------------------
SAMPLE_PREFIX="SET NOCOUNT ON;
SET ANSI_WARNINGS OFF;"

BUFFER_RATIO_MATCH="$(counter_match "Buffer Manager" "Buffer cache hit ratio" "-")"
BUFFER_RATIO_BASE_MATCH="$(counter_match "Buffer Manager" "Buffer cache hit ratio base" "-")"

# The seconds since 1970 are built from days and the seconds within the day,
# because DATEDIFF(SECOND, ...) overflows its int in 2038 and DATEDIFF_BIG
# needs SQL Server 2016. This runs on 2012 and later and does not overflow.
# The marker uses the server's clock; it is only compared with itself.
SUMMARY_QUERY="
SELECT CONCAT(N'S,', c.buffer_cache_hit_ratio${COUNTER_FIELD_LIST},
  N',', r.active_requests,
  N',', s.runnable_tasks, N',', s.work_queue_count, N',', s.active_workers, N',', s.busy_schedulers,
  N',', i.server_start_marker)
FROM
  (SELECT CAST(100.0 * MAX(CASE WHEN ${BUFFER_RATIO_MATCH} THEN cntr_value END)
               / NULLIF(MAX(CASE WHEN ${BUFFER_RATIO_BASE_MATCH} THEN cntr_value END), 0)
               AS DECIMAL(6,2)) AS buffer_cache_hit_ratio${COUNTER_VALUE_LIST}
   FROM sys.dm_os_performance_counters) AS c
CROSS JOIN
  (SELECT COUNT(*) AS active_requests
   FROM sys.dm_exec_requests AS er
   JOIN sys.dm_exec_sessions AS es ON es.session_id = er.session_id
   WHERE es.is_user_process = 1 AND er.session_id <> @@SPID) AS r
CROSS JOIN
  (SELECT SUM(runnable_tasks_count) AS runnable_tasks, SUM(work_queue_count) AS work_queue_count,
          SUM(active_workers_count) AS active_workers, SUM(CASE WHEN is_idle = 0 THEN 1 ELSE 0 END) AS busy_schedulers
   FROM sys.dm_os_schedulers
   WHERE status = N'VISIBLE ONLINE') AS s
CROSS JOIN
  (SELECT CAST(DATEDIFF(DAY, '19700101', sqlserver_start_time) AS bigint) * 86400
          + DATEDIFF(SECOND, CAST(sqlserver_start_time AS date), sqlserver_start_time) AS server_start_marker
   FROM sys.dm_os_sys_info) AS i;
"

# Only the waits the analyser sorts into categories, not all 900 wait types.
# The counts and times are cumulative since server start.
WAITS_QUERY="$(cat <<'EOF'
SELECT CONCAT(N'W,"', REPLACE(wait_type, N'"', N'""'), N'",',
              waiting_tasks_count, N',', wait_time_ms, N',', signal_wait_time_ms)
FROM sys.dm_os_wait_stats
WHERE wait_type IN (N'WRITELOG', N'LOGBUFFER', N'IO_COMPLETION', N'ASYNC_IO_COMPLETION', N'WRITE_COMPLETION',
                    N'BACKUPIO', N'SOS_SCHEDULER_YIELD', N'CXPACKET', N'CXCONSUMER', N'RESOURCE_SEMAPHORE',
                    N'RESOURCE_SEMAPHORE_QUERY_COMPILE', N'THREADPOOL', N'ASYNC_NETWORK_IO', N'HADR_SYNC_COMMIT',
                    N'CMEMTHREAD')
   OR wait_type LIKE N'PAGEIOLATCH[_]%'
   OR wait_type LIKE N'PAGELATCH[_]%'
   OR wait_type LIKE N'LCK[_]M[_]%';
EOF
)"

# sys.master_files supplies the file type and path. A login without
# VIEW ANY DEFINITION sees no rows in it, and then this record is empty.
FILES_QUERY="$(cat <<'EOF'
SELECT CONCAT(N'F,"', REPLACE(DB_NAME(vfs.database_id), N'"', N'""'), N'","', mf.type_desc, N'","',
              REPLACE(CAST(mf.physical_name AS nvarchar(400)), N'"', N'""'), N'",',
              vfs.num_of_reads, N',', vfs.io_stall_read_ms, N',', vfs.num_of_bytes_read, N',',
              vfs.num_of_writes, N',', vfs.io_stall_write_ms, N',', vfs.num_of_bytes_written)
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
JOIN sys.master_files AS mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id;
EOF
)"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
resolve_output_base
make_run_dir

WAITS_HEADER="ts,wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms"
FILES_HEADER="ts,database,file_type,physical_name,num_of_reads,io_stall_read_ms,num_of_bytes_read,num_of_writes,io_stall_write_ms,num_of_bytes_written"

register_record S mssql_stats.csv "$SUMMARY_HEADER"
register_record W mssql_waits.csv "$WAITS_HEADER"
register_record F mssql_files.csv "$FILES_HEADER"
OUTFILE="${OUTDIR}/mssql_stats.csv"

SAMPLE_SQL_FILE="${OUTDIR}/sample.sql"
printf '%s\n%s\n%s\n%s\n' "$SAMPLE_PREFIX" "$SUMMARY_QUERY" "$WAITS_QUERY" "$FILES_QUERY" > "$SAMPLE_SQL_FILE"

# ---------------------------------------------------------------------------
# Identity, settings and file locations
# ---------------------------------------------------------------------------
manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
if [[ -n "$SQL_USER" ]]; then
    manifest_add target "${SQL_USER}@${SQL_SERVER}/${SQL_DATABASE}"
else
    manifest_add target "${SQL_SERVER}/${SQL_DATABASE}"
fi
manifest_add client_version "${CLIENT_VERSION:-unknown}"
manifest_add server_version "${SERVER_VERSION:-unknown}"
manifest_add tls_trust_server_certificate "$TLS_TRUST_SERVER_CERTIFICATE"

# One "name|value" line per fact. A value may contain "|" but a name does not.
# Settings from sys.configurations are named like the setting, in lower case
# with underscores: "max server memory (MB)" -> max_server_memory_mb.
INFO_QUERY="$(cat <<'EOF'
SELECT CONCAT(N'machine_name|', CONVERT(nvarchar(256), SERVERPROPERTY('MachineName')));
SELECT CONCAT(N'instance_name|', ISNULL(CONVERT(nvarchar(128), SERVERPROPERTY('InstanceName')), N'MSSQLSERVER'));
SELECT CONCAT(N'product_version|', CONVERT(nvarchar(50), SERVERPROPERTY('ProductVersion')));
SELECT CONCAT(N'edition|', CONVERT(nvarchar(128), SERVERPROPERTY('Edition')));
SELECT CONCAT(N'is_hadr_enabled|', CONVERT(nvarchar(10), SERVERPROPERTY('IsHadrEnabled')));
SELECT CONCAT(LOWER(REPLACE(REPLACE(REPLACE(name, N' ', N'_'), N'(', N''), N')', N'')), N'|', CONVERT(nvarchar(50), value_in_use))
FROM sys.configurations
WHERE name IN (N'max server memory (MB)', N'max degree of parallelism', N'cost threshold for parallelism', N'max worker threads');
SELECT CONCAT(v.setting_name, N'|', v.setting_value)
FROM sys.dm_os_sys_info AS i
CROSS APPLY (VALUES
  (N'server_start', CONVERT(nvarchar(30), i.sqlserver_start_time, 120)),
  (N'cpu_count', CONVERT(nvarchar(30), i.cpu_count)),
  (N'scheduler_count', CONVERT(nvarchar(30), i.scheduler_count)),
  (N'max_workers_count', CONVERT(nvarchar(30), i.max_workers_count)),
  (N'physical_memory_kb', CONVERT(nvarchar(30), i.physical_memory_kb)),
  (N'committed_target_kb', CONVERT(nvarchar(30), i.committed_target_kb))) AS v(setting_name, setting_value);
EOF
)"
INFO_TEXT="$(sql_query "$INFO_QUERY" 2>>"$ERROR_LOG" || true)"

declare -A SERVER_INFO=()
while IFS= read -r info_line; do
    info_line="${info_line%$'\r'}"
    if [[ "$info_line" == *"|"* ]]; then
        SERVER_INFO[${info_line%%|*}]="${info_line#*|}"
    elif [[ -n "$info_line" ]]; then
        echo "$(date -Iseconds) start-up query output: ${info_line}" >> "$ERROR_LOG"
    fi
done <<< "$INFO_TEXT"

MACHINE_NAME="${SERVER_INFO[machine_name]-}"
INSTANCE_NAME="${SERVER_INFO[instance_name]:-MSSQLSERVER}"

# sqlcmd -S can carry a protocol, a port or an instance name; only the host
# name says where the server is.
TARGET_HOST="${SQL_SERVER#tcp:}"
TARGET_HOST="${TARGET_HOST%%,*}"
TARGET_HOST="${TARGET_HOST%%\\*}"
resolve_target_locality "$TARGET_HOST" "$MACHINE_NAME"

manifest_add_db_identity mssql "$INSTANCE_NAME" "$MACHINE_NAME" "${SERVER_INFO[server_start]-}" "$SERVER_VERSION_NUM"

# Everything in the info query that is not identity is recorded as a setting.
for identity_key in machine_name instance_name server_start; do
    unset "SERVER_INFO[$identity_key]"
done
while IFS= read -r info_line; do
    info_name="${info_line%%|*}"
    if [[ "$info_line" == *"|"* && -n "${SERVER_INFO[$info_name]-}" ]]; then
        record_setting "$info_name" "${SERVER_INFO[$info_name]}"
    fi
done <<< "$INFO_TEXT"
if [[ -z "${SERVER_INFO[cpu_count]-}" ]]; then
    note "sys.dm_os_sys_info is not readable for this login (grant ${SERVER_STATE_PERMISSION}); CPU, worker and memory settings are not recorded"
fi

# The server's own type for each counter (65792 gauge, 272696576 cumulative,
# 537003264 ratio), so the analyser does not have to guess from the name.
# buffer_cache_hit_ratio is left out: the column holds the finished
# percentage, not the raw counter.
COUNTER_TYPE_TEXT="$(sql_query "$COUNTER_TYPE_QUERY" 2>>"$ERROR_LOG" || true)"
COUNTER_TYPES_FOUND=0
while IFS='|' read -r type_column counter_type; do
    if [[ "$counter_type" =~ ^[0-9]+$ ]]; then
        manifest_add "counter_type.${type_column}" "$counter_type"
        COUNTER_TYPES_FOUND=$(( COUNTER_TYPES_FOUND + 1 ))
    fi
done <<< "$COUNTER_TYPE_TEXT"
if (( COUNTER_TYPES_FOUND == 0 )); then
    note "sys.dm_os_performance_counters returned no counter types (grant ${SERVER_STATE_PERMISSION}); the analyser falls back to the column names"
fi

# Compare the files that have IO statistics with the files sys.master_files
# shows. Fewer means the login lacks VIEW ANY DEFINITION.
FILE_PROBE_QUERY="SELECT CONCAT(
  (SELECT COUNT(*) FROM sys.dm_io_virtual_file_stats(NULL, NULL)), N'|',
  (SELECT COUNT(*) FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
   JOIN sys.master_files AS mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id));"
FILE_PROBE="$(sql_query "$FILE_PROBE_QUERY" 2>>"$ERROR_LOG" | sed -n '/./{p;q;}' || true)"
STATS_FILE_COUNT="${FILE_PROBE%%|*}"
LISTED_FILE_COUNT="${FILE_PROBE#*|}"
if [[ ! "$STATS_FILE_COUNT" =~ ^[0-9]+$ || ! "$LISTED_FILE_COUNT" =~ ^[0-9]+$ ]]; then
    note "file IO statistics are not readable for this login (grant ${SERVER_STATE_PERMISSION}), so mssql_files.csv will be empty"
elif (( LISTED_FILE_COUNT < STATS_FILE_COUNT )); then
    note "sys.master_files lists ${LISTED_FILE_COUNT} of ${STATS_FILE_COUNT} database files (grant VIEW ANY DEFINITION), so mssql_files.csv is empty or incomplete"
fi

# Directories of the database files: data (ROWS), log (LOG) and tempdb (both
# kinds) as temp. Written only for a server on this host. When the server
# reports another host name but was reached over loopback it runs in a
# container, and its paths are inside the container's mount namespace.
if [[ "$TARGET_LOCAL" == "yes" ]]; then
    LOCAL_NAME="$(short_hostname)"
    MACHINE_SHORT_NAME="${MACHINE_NAME%%.*}"
    if [[ "$TARGET_LOCAL_BASIS" == "loopback" && -n "$MACHINE_NAME" && "${MACHINE_SHORT_NAME,,}" != "${LOCAL_NAME,,}" ]]; then
        echo "path_namespace=container" >> "${OUTDIR}/db_paths.txt"
        manifest_add path_namespace container
        note "SQL Server reports host name ${MACHINE_NAME}, not ${LOCAL_NAME}; it runs in a container and its file paths are not paths on this host"
    fi
    FILE_PATHS_QUERY="SELECT DISTINCT CONCAT(
  CASE WHEN database_id = 2 THEN N'temp' WHEN type_desc = N'LOG' THEN N'log' ELSE N'data' END, N'|', physical_name)
FROM sys.master_files
WHERE type_desc IN (N'ROWS', N'LOG');"
    while IFS='|' read -r file_role physical_name; do
        if [[ -n "$file_role" && "$physical_name" == /* ]]; then
            add_db_path "$file_role" "${physical_name%/*}"
        fi
    done < <(sql_query "$FILE_PATHS_QUERY" 2>>"$ERROR_LOG" || true)
fi
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: SQL Server ${SERVER_VERSION:-unknown} (${INSTANCE_NAME}, on this host: ${TARGET_LOCAL})"

# The timestamp is the collector's clock, not the server's. sqlcmd reports
# errors on stdout, so error text arrives as rows with an unknown record
# letter and is routed to errors.log.
collect_one_sample() {
    local ts output status=0
    ts="$(date -Iseconds)"
    output="$(run_with_timeout "$SQLCMD_BIN" "${SQLCMD_ARGS[@]}" "${SQLCMD_OUTPUT_ARGS[@]}" \
        -t "$QUERY_TIMEOUT_S" -i "$SAMPLE_SQL_FILE" 2>>"$ERROR_LOG")" || status=$?
    route_sample_output "$ts" "$output"
    note_sample_failure "$ts" "$status"
}

run_collection
