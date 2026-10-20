#!/usr/bin/env bash
#
# collect-mysql-metrics.sh
# Samples SHOW GLOBAL STATUS counters on MySQL or MariaDB at a fixed interval
# for a fixed duration, matching the OS-level sadc/pidstat window.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="3.0.0"
COLLECTOR_TAG="mysql"
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
                      [--host host] [--port port] [--socket path]
                      [--user user] [--foreground] [-h] [-V]

  -d, --duration    Total run time: 30, 30s, 15m, 2h, 1d. A bare number is
                    seconds. Prompted for if omitted and a terminal is present
  -c, --count       Number of samples. Mutually exclusive with -d
  -i, --interval    Sample interval: 15, 15s, 1m. A bare number is seconds.
                    Prompted for if omitted and a terminal is present
  -o, --outdir      Base output directory. Default: /var/log/perf-diag, or
                    ~/perf-diag when that is not writable
      --host        MySQL/MariaDB host      (default: localhost). The client
                    reaches the host name localhost over the Unix socket
      --port        Port                    (default: 3306). With --host
                    localhost, an explicit --port makes the script use TCP
      --socket      Unix socket path. Cannot be combined with --host or --port
      --user        User                    (default: root)
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Authentication: uses the MYSQL_PWD environment variable or ~/.my.cnf. Set one
of these before running if the connection needs a password. A password is
never accepted as an option.

Local root login: on Debian and Ubuntu the MariaDB root account logs in through
the unix_socket plugin, which needs the operating system user root. Run
sudo ./${SCRIPT_NAME} for a local root login. This is the one database
collector where sudo is the normal way to run it. Under sudo, ~/.my.cnf is read
from root's home directory (or pass MYSQL_PWD with sudo -E).

Privileges: a login with PROCESS, REPLICATION CLIENT and SELECT on
performance_schema covers every query:
  GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'localhost';
  GRANT SELECT ON performance_schema.* TO 'monitor'@'localhost';
Without PROCESS, history_list_length is empty. Without the performance_schema
grant, or with performance_schema off (the MariaDB default), mysql_file_io.csv
is not written. Without REPLICATION CLIENT, replica lag is not recorded. On
MariaDB 10.5.9 and later that statement needs REPLICA MONITOR instead:
  GRANT REPLICA MONITOR ON *.* TO 'monitor'@'localhost';

Output goes to <outdir>/<host>_mysql_<timestamp>/ and holds mysql_stats.csv,
mysql_file_io.csv (performance_schema on), db_paths.txt (server on this host),
manifest.txt, errors.log and (detached runs) collector.pid and collector.log.
Stop a detached run with: kill \$(cat <outdir>/collector.pid)

Each sample is limited to a timeout just under the interval, so one hung query
cannot hold the schedule.

A detached run survives SSH logout. It does not survive systemd-logind
KillUserProcesses=yes, which is off by default on Fedora, RHEL, Debian and
Ubuntu.

Example:
  ${SCRIPT_NAME} -d 2h -i 30s --host db1.example.com --user monitor
USAGE
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
DURATION=""
COUNT=""
INTERVAL=""
OUTDIR_BASE=""
DB_HOST="localhost"
DB_PORT="3306"
DB_SOCKET=""
DB_USER="root"
HOST_GIVEN="no"
PORT_GIVEN="no"
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
        --host|--host=*)            take_value "$arg" "${2-}" "$has_next"; DB_HOST="$OPT_VALUE"; HOST_GIVEN="yes" ;;
        --port|--port=*)            take_value "$arg" "${2-}" "$has_next"; DB_PORT="$OPT_VALUE"; PORT_GIVEN="yes" ;;
        --socket|--socket=*)        take_value "$arg" "${2-}" "$has_next"; DB_SOCKET="$OPT_VALUE" ;;
        --user|--user=*)            take_value "$arg" "${2-}" "$has_next"; DB_USER="$OPT_VALUE" ;;
        --foreground) FOREGROUND="yes"; OPT_SHIFT=1 ;;
        -h|--help)
            # -h used to take a host. Refuse "-h <value>" instead of printing help.
            if [[ "$arg" == "-h" && "$has_next" == "yes" && "${2-}" != -* ]]; then
                usage_error "-h now means help; use --host to set the host"
            fi
            usage; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        # Options from earlier versions: name the replacement.
        -P) usage_error "-P was removed; use --port" ;;
        -u) usage_error "-u was removed; use --user" ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi
if [[ ! "$DB_PORT" =~ ^[0-9]+$ ]] || (( DB_PORT < 1 || DB_PORT > 65535 )); then
    usage_error "--port must be a number from 1 to 65535"
fi
if [[ -n "$DB_SOCKET" && ( "$HOST_GIVEN" == "yes" || "$PORT_GIVEN" == "yes" ) ]]; then
    usage_error "--socket cannot be combined with --host or --port"
fi

resolve_timing
set_query_timeout

# ---------------------------------------------------------------------------
# Client and connection
# ---------------------------------------------------------------------------

# MariaDB 11 prints a deprecation warning on every call when its client runs
# under the name "mysql", so use the name "mariadb" when mysql is MariaDB's.
# Prints nothing when there is no client.
choose_client() {
    local mysql_path=""
    if command -v mysql >/dev/null 2>&1; then
        mysql_path="$(readlink -f "$(command -v mysql)")"
    fi
    if [[ -n "$mysql_path" && "${mysql_path##*/}" == "mariadb" ]]; then
        echo "mariadb"
    elif [[ -n "$mysql_path" ]]; then
        echo "mysql"
    elif command -v mariadb >/dev/null 2>&1; then
        echo "mariadb"
    fi
}

MYSQL_BIN="$(choose_client)"
if [[ -z "$MYSQL_BIN" ]]; then
    echo "ERROR: no MySQL/MariaDB client ('mysql' or 'mariadb') found." >&2
    echo "Install a client, then re-run:" >&2
    case "$(distro_family)" in
        debian) echo "  sudo apt-get update && sudo apt-get install -y mariadb-client   # or mysql-client-core-* if using Oracle's MySQL repo" >&2 ;;
        rhel)   echo "  sudo dnf install -y mysql   # MariaDB-based RHEL derivatives may ship this as 'mariadb'" >&2 ;;
        suse)   echo "  sudo zypper install -y mariadb-client" >&2 ;;
        *)      echo "  Install the mysql/mariadb client package for your distro." >&2 ;;
    esac
    exit 1
fi

# --batch prints plain tab-separated rows. The host name localhost makes the
# client use the Unix socket. MySQL ignores a port given with it, but the
# MariaDB client switches to TCP, where a root login through unix_socket fails.
# So localhost gets no -P unless the caller gave a port, which asks for TCP.
MYSQL_ARGS=(--batch --skip-column-names --connect-timeout=5 -u "$DB_USER")
if [[ -n "$DB_SOCKET" ]]; then
    MYSQL_ARGS+=(--socket "$DB_SOCKET")
    LOCALITY_HOST="$DB_SOCKET"
    DB_INSTANCE_TEXT="$DB_SOCKET"
elif [[ "$DB_HOST" == "localhost" && "$PORT_GIVEN" == "yes" ]]; then
    MYSQL_ARGS+=(-h "$DB_HOST" -P "$DB_PORT" --protocol=TCP)
    note "--host localhost with --port ${DB_PORT}: connecting over TCP, because the client would use the Unix socket and ignore the port"
    LOCALITY_HOST="$DB_HOST"
    DB_INSTANCE_TEXT="${DB_HOST}:${DB_PORT}"
else
    MYSQL_ARGS+=(-h "$DB_HOST")
    if [[ "$DB_HOST" != "localhost" ]]; then
        MYSQL_ARGS+=(-P "$DB_PORT")
    fi
    LOCALITY_HOST="$DB_HOST"
    DB_INSTANCE_TEXT="${DB_HOST}:${DB_PORT}"
fi

# Runs one SQL text and prints its rows. $1 = SQL.
mysql_query() {
    "$MYSQL_BIN" "${MYSQL_ARGS[@]}" -e "$1"
}

CONN_ERR_FILE="$(mktemp)"
trap 'rm -f "$CONN_ERR_FILE"' EXIT
if ! mysql_query 'SELECT 1' >/dev/null 2>"$CONN_ERR_FILE"; then
    echo "ERROR: cannot connect to MySQL/MariaDB at ${DB_INSTANCE_TEXT} as ${DB_USER}." >&2
    cat "$CONN_ERR_FILE" >&2
    echo "Check the host, port, socket and user, and that MYSQL_PWD or ~/.my.cnf is set correctly." >&2
    if [[ "$DB_USER" == "root" ]]; then
        echo "On Debian and Ubuntu the root login needs the operating system user root: run this script with sudo." >&2
    fi
    exit 1
fi
rm -f "$CONN_ERR_FILE"
trap - EXIT

SERVER_VERSION="$(mysql_query 'SELECT VERSION()' 2>/dev/null || true)"
CLIENT_VERSION="$("$MYSQL_BIN" --version)"

# 8.0.36 -> 80036. Some MariaDB versions report 5.5.5-10.x.y-MariaDB; the
# 5.5.5- is a replication compatibility prefix, not the version.
VERSION_TEXT="$SERVER_VERSION"
if [[ "$SERVER_VERSION" == 5.5.5-*MariaDB* ]]; then
    VERSION_TEXT="${SERVER_VERSION#5.5.5-}"
fi
if [[ ! "$VERSION_TEXT" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+) ]]; then
    die "could not read the server version from ${DB_INSTANCE_TEXT}"
fi
SERVER_VERSION_NUM=$(( 10#${BASH_REMATCH[1]} * 10000 + 10#${BASH_REMATCH[2]} * 100 + 10#${BASH_REMATCH[3]} ))

# The run tag stays "mysql" for MariaDB. db_engine tells them apart.
if [[ "$SERVER_VERSION" == *MariaDB* ]]; then
    DB_ENGINE="mariadb"
    SERVER_LABEL="MariaDB"
else
    DB_ENGINE="mysql"
    SERVER_LABEL="MySQL"
fi

# ---------------------------------------------------------------------------
# Server settings, read once. They decide which optional queries are used and
# are recorded in the manifest further down.
# ---------------------------------------------------------------------------
SETTING_NAMES="hostname version_comment max_connections innodb_buffer_pool_size innodb_flush_log_at_trx_commit sync_binlog innodb_flush_method innodb_io_capacity innodb_io_capacity_max innodb_log_file_size innodb_log_files_in_group innodb_redo_log_capacity innodb_doublewrite performance_schema datadir tmpdir innodb_data_home_dir innodb_log_group_home_dir innodb_tmpdir log_bin thread_cache_size long_query_time"

setting_list=""
for setting_name in $SETTING_NAMES; do
    setting_list="${setting_list:+${setting_list},}'${setting_name}'"
done

declare -A MYSQL_SETTING=()
while IFS=$'\t' read -r setting_name setting_value; do
    if [[ -n "$setting_name" ]]; then
        MYSQL_SETTING[$setting_name]="$setting_value"
    fi
done < <(mysql_query "SHOW GLOBAL VARIABLES WHERE Variable_name IN (${setting_list})" 2>/dev/null || true)

SERVER_HOSTNAME="${MYSQL_SETTING[hostname]-}"

# ---------------------------------------------------------------------------
# Optional data. Each source is used only when this login can read it; a
# missing one is a NOTE, not an error.
# ---------------------------------------------------------------------------

# History list length: INNODB_METRICS exists from MySQL 5.6 and MariaDB 10.0
# and needs the PROCESS privilege.
HISTORY_QUERY=""
if [[ "$DB_ENGINE" == "mysql" ]] && (( SERVER_VERSION_NUM < 50600 )); then
    note "MySQL ${SERVER_VERSION} has no information_schema.INNODB_METRICS (5.6 and later), so history_list_length is empty"
elif mysql_query "SELECT COUNT FROM information_schema.INNODB_METRICS WHERE NAME = 'trx_rseg_history_len'" >/dev/null 2>&1; then
    HISTORY_QUERY="SELECT 'H', COUNT FROM information_schema.INNODB_METRICS WHERE NAME = 'trx_rseg_history_len';"
else
    note "information_schema.INNODB_METRICS is not readable for this login (grant PROCESS), so history_list_length is empty"
fi

# InnoDB file IO timers come from performance_schema, which is off by default
# on MariaDB.
FILE_IO_QUERY=""
if [[ "${MYSQL_SETTING[performance_schema]-}" != "ON" && "${MYSQL_SETTING[performance_schema]-}" != "1" ]]; then
    note "performance_schema is off, so InnoDB file IO latency is not measured and mysql_file_io.csv is not written. Set performance_schema=ON in the server configuration and restart to enable it"
elif mysql_query "SELECT COUNT(*) FROM performance_schema.file_summary_by_event_name" >/dev/null 2>&1; then
    FILE_IO_QUERY="SELECT 'P', CONCAT('\"', EVENT_NAME, '\"'), COUNT_READ, SUM_TIMER_READ, COUNT_WRITE, SUM_TIMER_WRITE, COUNT_MISC, SUM_TIMER_MISC FROM performance_schema.file_summary_by_event_name WHERE EVENT_NAME LIKE 'wait/io/file/innodb/%' AND COUNT_READ + COUNT_WRITE + COUNT_MISC > 0;"
else
    note "performance_schema.file_summary_by_event_name is not readable for this login (grant SELECT on performance_schema), so mysql_file_io.csv is not written"
fi

# Replica status. The statement was renamed in MySQL 8.0.22 and MariaDB 10.5.1.
# The sample only asks for it when the server was a replica at the start. The
# client prints column names for it (--column-names), because the columns are
# found by name and their order differs between versions.
if [[ "$DB_ENGINE" == "mysql" ]] && (( SERVER_VERSION_NUM >= 80022 )); then
    REPLICA_STATUS_SQL='SHOW REPLICA STATUS\G'
elif [[ "$DB_ENGINE" == "mariadb" ]] && (( SERVER_VERSION_NUM >= 100501 )); then
    REPLICA_STATUS_SQL='SHOW REPLICA STATUS\G'
else
    REPLICA_STATUS_SQL='SHOW SLAVE STATUS\G'
fi

if [[ "$DB_ENGINE" == "mariadb" ]] && (( SERVER_VERSION_NUM >= 100509 )); then
    REPLICA_PRIVILEGE="REPLICA MONITOR"
else
    REPLICA_PRIVILEGE="REPLICATION CLIENT"
fi

IS_REPLICA="no"
REPLICA_STATUS_FAILED="no"
REPLICA_STATUS_AT_START="$("$MYSQL_BIN" "${MYSQL_ARGS[@]}" --column-names -e "$REPLICA_STATUS_SQL" 2>/dev/null)" || REPLICA_STATUS_FAILED="yes"
if [[ "$REPLICA_STATUS_FAILED" == "yes" ]]; then
    note "${REPLICA_STATUS_SQL%\\G} failed for this login (grant ${REPLICA_PRIVILEGE}), so replica lag is not recorded even if this server is a replica"
elif [[ -n "$REPLICA_STATUS_AT_START" ]]; then
    IS_REPLICA="yes"
    if [[ "$DB_ENGINE" == "mariadb" ]]; then
        # SHOW SLAVE STATUS shows only the default connection.
        CONNECTION_COUNT="$("$MYSQL_BIN" "${MYSQL_ARGS[@]}" --column-names -e 'SHOW ALL SLAVES STATUS\G' 2>/dev/null | grep -c 'Connection_name:' || true)"
        if (( CONNECTION_COUNT > 1 )); then
            note "MariaDB multi-source replication (${CONNECTION_COUNT} connections): replica_lag_s covers the default connection only"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Sample query and output files
# ---------------------------------------------------------------------------

# The first 19 names are the columns of earlier versions and keep their
# meaning. The rest were added in 3.0.0.
BASE_STATUS_VARIABLES="Threads_connected Threads_running Questions Slow_queries Com_select Com_insert Com_update Com_delete Innodb_buffer_pool_reads Innodb_buffer_pool_read_requests Innodb_row_lock_waits Innodb_row_lock_time Innodb_row_lock_time_avg Created_tmp_disk_tables Created_tmp_tables Open_files Open_tables Aborted_connects Aborted_clients"
ADDED_STATUS_VARIABLES="Uptime Threads_created Connections Max_used_connections Com_commit Com_rollback Select_full_join Select_scan Sort_merge_passes Table_locks_waited Innodb_buffer_pool_wait_free Innodb_buffer_pool_pages_total Innodb_buffer_pool_pages_dirty Innodb_buffer_pool_pages_free Innodb_buffer_pool_pages_flushed Innodb_data_reads Innodb_data_writes Innodb_data_read Innodb_data_written Innodb_data_fsyncs Innodb_os_log_written Innodb_os_log_fsyncs Innodb_log_waits Innodb_row_lock_current_waits Innodb_rows_read Innodb_rows_inserted Innodb_rows_updated Innodb_rows_deleted Innodb_checkpoint_age Innodb_checkpoint_max_age Innodb_history_list_length Bytes_received Bytes_sent"
STATUS_VARIABLES="${BASE_STATUS_VARIABLES} ${ADDED_STATUS_VARIABLES}"

SUMMARY_HEADER="ts,${STATUS_VARIABLES// /,},history_list_length,replica_lag_s,replica_io_running,replica_sql_running"
FILE_IO_HEADER="ts,event_name,count_read,sum_timer_read_ps,count_write,sum_timer_write_ps,count_misc,sum_timer_misc_ps"

# One client call per sample. SHOW GLOBAL STATUS prints name<TAB>value rows
# with no record letter; the H and P queries start every row with their letter.
SAMPLE_SQL="SHOW GLOBAL STATUS;
${HISTORY_QUERY}
${FILE_IO_QUERY}"

# Turns the client output of one sample into rows for route_sample_output.
# Reads the output on standard input and takes the replica columns as
# variables. Prints one S row in the order of the header, with an empty field
# for a status variable the server does not report, and passes P rows on with
# commas in place of tabs. The H row only supplies history_list_length. No S
# row is printed when there is no status output, because the sample failed.
build_sample_rows() {
    awk -F'\t' \
        -v variables="$STATUS_VARIABLES" \
        -v replica_lag="$REPLICA_LAG_S" \
        -v replica_io_running="$REPLICA_IO_RUNNING" \
        -v replica_sql_running="$REPLICA_SQL_RUNNING" '
        function row_with_commas(   i, line) {
            line = $1
            for (i = 2; i <= NF; i++) {
                line = line "," $i
            }
            return line
        }
        BEGIN {
            variable_count = split(variables, variable_names, " ")
            status_rows = 0
            history_list_length = ""
        }
        $1 == "H" {
            if ($2 != "NULL") {
                history_list_length = $2
            }
            next
        }
        $1 == "P" {
            print row_with_commas()
            next
        }
        NF == 2 {
            status[$1] = $2
            status_rows++
            next
        }
        {
            # Anything else is unexpected. Pass it on so it is logged.
            print row_with_commas()
        }
        END {
            if (status_rows == 0) {
                exit
            }
            row = "S"
            for (i = 1; i <= variable_count; i++) {
                row = row "," status[variable_names[i]]
            }
            print row "," history_list_length "," replica_lag "," replica_io_running "," replica_sql_running
        }
    '
}

# Reads the replica columns out of vertical SHOW ... STATUS output on standard
# input and prints "lag,io_running,sql_running". Fields are found by name. The
# lag is the largest of all channels; a thread counts as running (1) only when
# it runs on every channel.
summarise_replica_status() {
    awk '
        {
            name = $1
            sub(/:$/, "", name)
            value = $2
        }
        name == "Seconds_Behind_Source" || name == "Seconds_Behind_Master" {
            if (value ~ /^[0-9]+$/ && (lag == "" || value + 0 > lag + 0)) {
                lag = value
            }
        }
        name == "Replica_IO_Running" || name == "Slave_IO_Running" {
            if (value != "Yes") { io_running = 0 } else if (io_running == "") { io_running = 1 }
        }
        name == "Replica_SQL_Running" || name == "Slave_SQL_Running" {
            if (value != "Yes") { sql_running = 0 } else if (sql_running == "") { sql_running = 1 }
        }
        END {
            print lag "," io_running "," sql_running
        }
    '
}

resolve_output_base
make_run_dir

register_record S mysql_stats.csv "$SUMMARY_HEADER"
if [[ -n "$FILE_IO_QUERY" ]]; then
    register_record P mysql_file_io.csv "$FILE_IO_HEADER"
fi
OUTFILE="${OUTDIR}/mysql_stats.csv"

# ---------------------------------------------------------------------------
# Identity, settings and file locations
# ---------------------------------------------------------------------------
manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
manifest_add target "${DB_USER}@${DB_INSTANCE_TEXT}"
manifest_add client_version "$CLIENT_VERSION"
manifest_add server_version "${SERVER_VERSION:-unknown}"

# The server start time is its current time minus its uptime.
SERVER_UPTIME_S="$(mysql_query "SHOW GLOBAL STATUS LIKE 'Uptime'" 2>/dev/null | awk '{print $2}' || true)"
SERVER_START_TEXT=""
if [[ "$SERVER_UPTIME_S" =~ ^[0-9]+$ ]]; then
    SERVER_START_TEXT="$(mysql_query "SELECT DATE_SUB(NOW(), INTERVAL ${SERVER_UPTIME_S} SECOND)" 2>/dev/null || true)"
fi

resolve_target_locality "$LOCALITY_HOST" "$SERVER_HOSTNAME"
manifest_add_db_identity "$DB_ENGINE" "$DB_INSTANCE_TEXT" "$SERVER_HOSTNAME" "$SERVER_START_TEXT" "$SERVER_VERSION_NUM"

for setting_name in $SETTING_NAMES; do
    if [[ -n "${MYSQL_SETTING[$setting_name]-}" ]]; then
        record_setting "$setting_name" "${MYSQL_SETTING[$setting_name]}"
    fi
done

# "/var/lib/mysql/" -> "/var/lib/mysql". A lone "/" stays.
strip_trailing_slash() {
    local path="$1"
    while [[ "$path" == */ && "$path" != "/" ]]; do
        path="${path%/}"
    done
    echo "$path"
}

# A relative directory setting is relative to the data directory; "./" and an
# empty value both mean the data directory itself.
resolve_against_data_directory() {
    local directory="$1" relative
    if [[ "$directory" == /* ]]; then
        strip_trailing_slash "$directory"
        return
    fi
    relative="$(strip_trailing_slash "${directory#./}")"
    if [[ -z "$relative" || "$relative" == "." ]]; then
        echo "$DATA_DIRECTORY"
    else
        echo "${DATA_DIRECTORY}/${relative}"
    fi
}

DATA_DIRECTORY="$(strip_trailing_slash "${MYSQL_SETTING[datadir]-}")"
if [[ "$TARGET_LOCAL" == "yes" && -n "$DATA_DIRECTORY" ]]; then
    add_db_path data "$DATA_DIRECTORY"

    REDO_DIRECTORY="$(resolve_against_data_directory "${MYSQL_SETTING[innodb_log_group_home_dir]-}")"
    add_db_path redo "$REDO_DIRECTORY"
    # MySQL 8.0.30 and later keep the redo log files in a #innodb_redo
    # directory below the log directory.
    if [[ "$DB_ENGINE" == "mysql" ]] && (( SERVER_VERSION_NUM >= 80030 )); then
        add_db_path redo "${REDO_DIRECTORY}/#innodb_redo"
    fi

    # tmpdir can list several directories separated by colons.
    IFS=: read -r -a TEMP_DIRECTORIES <<< "${MYSQL_SETTING[tmpdir]-}"
    for temp_directory in ${TEMP_DIRECTORIES[@]+"${TEMP_DIRECTORIES[@]}"}; do
        add_db_path temp "$(strip_trailing_slash "$temp_directory")"
    done
    if [[ -n "${MYSQL_SETTING[innodb_tmpdir]-}" ]]; then
        add_db_path temp "$(strip_trailing_slash "${MYSQL_SETTING[innodb_tmpdir]}")"
    fi
fi
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: ${SERVER_LABEL} ${SERVER_VERSION:-unknown} (${DB_INSTANCE_TEXT}, on this host: ${TARGET_LOCAL})"

# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------
REPLICA_LAG_S=""
REPLICA_IO_RUNNING=""
REPLICA_SQL_RUNNING=""

# Sets REPLICA_LAG_S, REPLICA_IO_RUNNING and REPLICA_SQL_RUNNING for one
# sample. They stay empty on a server that was not a replica at the start,
# and when the call fails. $1 = timestamp for the error log.
read_replica_columns() {
    local ts="$1" replica_output status=0 columns
    REPLICA_LAG_S=""
    REPLICA_IO_RUNNING=""
    REPLICA_SQL_RUNNING=""
    if [[ "$IS_REPLICA" != "yes" ]]; then
        return
    fi
    replica_output="$(run_with_timeout "$MYSQL_BIN" "${MYSQL_ARGS[@]}" --column-names -e "$REPLICA_STATUS_SQL" 2>>"$ERROR_LOG")" || status=$?
    if (( status != 0 )); then
        note_sample_failure "$ts" "$status"
        return
    fi
    columns="$(printf '%s\n' "$replica_output" | summarise_replica_status)"
    IFS=, read -r REPLICA_LAG_S REPLICA_IO_RUNNING REPLICA_SQL_RUNNING <<< "$columns"
}

# The timestamp is the collector's clock, not the server's.
collect_one_sample() {
    local ts client_output sample_rows status=0
    ts="$(date -Iseconds)"
    read_replica_columns "$ts"
    client_output="$(run_with_timeout "$MYSQL_BIN" "${MYSQL_ARGS[@]}" -e "$SAMPLE_SQL" 2>>"$ERROR_LOG")" || status=$?
    sample_rows="$(printf '%s\n' "$client_output" | build_sample_rows)"
    route_sample_output "$ts" "$sample_rows"
    note_sample_failure "$ts" "$status"
}

run_collection
