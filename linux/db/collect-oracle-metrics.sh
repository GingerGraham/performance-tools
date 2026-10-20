#!/usr/bin/env bash
#
# collect-oracle-metrics.sh
# Samples system statistics, the time model, wait events and the active
# sessions of an Oracle instance at a fixed interval for a fixed duration,
# matching the OS-level sadc/pidstat window.
#
# Licensing: only v$sysstat, v$sys_time_model, v$system_event,
# v$system_wait_class, v$session, v$resource_limit, v$instance, v$database,
# v$version, v$parameter, v$datafile, v$tempfile, v$logfile and v$asm_disk_stat
# are read. v$active_session_history, DBA_HIST_* and AWR are never read: they
# need the Diagnostics Pack licence.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="3.0.0"
COLLECTOR_TAG="oracle"
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
                      [--connect connect_string] [--foreground] [-h] [-V]

  -d, --duration    Total run time: 30, 30s, 15m, 2h, 1d. A bare number is
                    seconds. Prompted for if omitted and a terminal is present
  -c, --count       Number of samples. Mutually exclusive with -d
  -i, --interval    Sample interval: 15, 15s, 1m. A bare number is seconds.
                    Prompted for if omitted and a terminal is present
  -o, --outdir      Base output directory. Default: /var/log/perf-diag, or
                    ~/perf-diag when that is not writable
      --connect     Connect string, e.g. user/password@//host:1521/service_name
                    Default: the ORACLE_CONNECT_STRING environment variable.
                    Prefer the environment variable: an option value is
                    visible in the process list and shell history
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

The connect string is sent to SQL*Plus on standard input, so it does not
appear in the process list.

Licensing: the collector reads only v\$sysstat, v\$sys_time_model,
v\$system_event, v\$system_wait_class, v\$session, v\$resource_limit,
v\$instance, v\$database, v\$version, v\$parameter, v\$datafile, v\$tempfile,
v\$logfile and v\$asm_disk_stat. It never reads v\$active_session_history,
DBA_HIST_* or AWR, which need the Diagnostics Pack licence.

Privileges: GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO <login> (or
SELECT ANY DICTIONARY). Each sample is one logon, so it adds to the logons
cumulative statistic and appears in the listener log; use an interval of 5
seconds or more.

Scope: connected to a pluggable database, the statistics may describe that
container only. Connect to CDB\$ROOT for instance-wide figures. On RAC the
figures are for the instance you connect to.

Output goes to <outdir>/<host>_oracle_<timestamp>/ and holds oracle_stats.csv,
oracle_waits.csv, oracle_active.csv, db_paths.txt (server on this host),
manifest.txt, errors.log and (detached runs) collector.pid and collector.log.
Stop a detached run with: kill \$(cat <outdir>/collector.pid)

Whether the server is on this host is taken from the host in the connect
string. A TNS alias hides the host, so then the instance's own host name is
compared with this host's name.

Each sample is limited to a timeout just under the interval, so one hung query
cannot hold the schedule.

A detached run survives SSH logout. It does not survive systemd-logind
KillUserProcesses=yes, which is off by default on Fedora, RHEL, Debian and
Ubuntu.

Example:
  ORACLE_CONNECT_STRING='monitor/secret@//db1:1521/ORCL' ${SCRIPT_NAME} -d 2h -i 30s
USAGE
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
DURATION=""
COUNT=""
INTERVAL=""
OUTDIR_BASE=""
CONNSTR="${ORACLE_CONNECT_STRING:-}"
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
        --connect|--connect=*)      take_value "$arg" "${2-}" "$has_next"; CONNSTR="$OPT_VALUE" ;;
        --foreground) FOREGROUND="yes"; OPT_SHIFT=1 ;;
        -h|--help)    usage; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi

# -c used to take a connect string. A connect string is not a positive
# integer, so it is refused here. The value is not echoed: it may hold a password.
if [[ -n "$COUNT" && ! "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
    usage_error "-c/--count must be a positive integer; connect strings now use --connect"
fi

resolve_timing
set_query_timeout

# ---------------------------------------------------------------------------
# Client and connection
# ---------------------------------------------------------------------------
if ! command -v sqlplus >/dev/null 2>&1; then
    echo "ERROR: 'sqlplus' not found." >&2
    echo "Oracle Instant Client isn't in standard distro repos; install it directly from Oracle:" >&2
    echo "  https://www.oracle.com/database/technologies/instant-client/downloads.html" >&2
    case "$(distro_family)" in
        rhel)   echo "  RHEL/Rocky/Alma/OL: Oracle publish an RPM (oracle-instantclient-sqlplus); Oracle Linux can also point at Oracle's public yum repo directly." >&2 ;;
        debian) echo "  Debian/Ubuntu: download the Instant Client Basic + SQL*Plus .zip packages, unzip, and set LD_LIBRARY_PATH / PATH (no native .deb from Oracle)." >&2 ;;
        suse)   echo "  SLES: use the generic Instant Client .zip (basic + sqlplus), same manual LD_LIBRARY_PATH setup as Debian." >&2 ;;
        *)      echo "  Use the generic Instant Client zip and set LD_LIBRARY_PATH accordingly." >&2 ;;
    esac
    exit 1
fi

if [[ -z "$CONNSTR" ]]; then
    die "no connect string supplied (--connect, or the ORACLE_CONNECT_STRING environment variable)"
fi

# Writes the SQL*Plus session: error handling, the logon, then the SQL that
# arrives on standard input. sqlplus is started with /nolog and reads all of
# this from standard input, so the connect string never appears in the process
# list. Both WHENEVER lines make sqlplus exit with a failure status.
sqlplus_session_script() {
    echo "WHENEVER SQLERROR EXIT FAILURE"
    echo "WHENEVER OSERROR EXIT FAILURE"
    printf 'CONNECT %s\n' "$CONNSTR"
    cat
}

# Runs the SQL on standard input as the configured user.
run_sqlplus() {
    sqlplus_session_script | sqlplus -s /nolog
}

# Plain output: no headings, no paging, lines wide enough for one whole record.
# Every number is converted in SQL with TO_CHAR(x, 'TM9') (text minimum: all
# digits, no exponent, no padding), so SQL*Plus never formats a number itself.
SQLPLUS_SETTINGS="SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF TRIMSPOOL ON TRIMOUT ON LINESIZE 32767 TAB OFF"

CONN_OUT_FILE="$(mktemp)"
trap 'rm -f "$CONN_OUT_FILE"' EXIT
if ! run_sqlplus > "$CONN_OUT_FILE" 2>&1 <<'SQL'
SELECT 1 FROM dual;
EXIT
SQL
then
    echo "ERROR: sqlplus could not connect." >&2
    cat "$CONN_OUT_FILE" >&2
    exit 1
fi
if grep -qE 'ORA-|SP2-' "$CONN_OUT_FILE"; then
    echo "ERROR: Oracle connection failed:" >&2
    cat "$CONN_OUT_FILE" >&2
    exit 1
fi
rm -f "$CONN_OUT_FILE"
trap - EXIT

# Setup queries print "key=value" lines (or "role=file name" lines for paths).
# Their output is kept in SETUP_OUTPUT. A line that starts with ORA- or SP2- is
# an error: they are counted and the first one goes into a NOTE, because a
# missing privilege only leaves some identity or settings values out.
SETUP_OUTPUT=""
SETUP_ERRORS=0
SETUP_FIRST_ERROR=""

# $1 = SQL statements (without the SET line and EXIT).
run_setup_sql() {
    local line
    SETUP_OUTPUT="$(printf '%s\n' "$SQLPLUS_SETTINGS" "$1" "EXIT" | run_sqlplus 2>&1 || true)"
    while IFS= read -r line; do
        case "$line" in
            ORA-*|SP2-*)
                SETUP_ERRORS=$(( SETUP_ERRORS + 1 ))
                SETUP_FIRST_ERROR="${SETUP_FIRST_ERROR:-${line}}" ;;
        esac
    done <<< "$SETUP_OUTPUT"
}

# Value of "key=value" line in SETUP_OUTPUT, or nothing.
# $1 = key. Later lines win, which does not matter for unique keys.
setup_value() {
    local line value=""
    while IFS= read -r line; do
        if [[ "$line" == "$1="* ]]; then
            value="${line#*=}"
        fi
    done <<< "$SETUP_OUTPUT"
    echo "$value"
}

# v$version.banner is the long text; v$instance.version decides which columns
# the later queries may use.
IFS= read -r -d '' BANNER_SQL <<SQL || true
SELECT 'banner=' || banner FROM v\$version WHERE ROWNUM = 1;
SELECT 'version=' || version FROM v\$instance;
SQL
run_setup_sql "$BANNER_SQL"
SERVER_VERSION="$(setup_value banner)"
INSTANCE_VERSION="$(setup_value version)"
CLIENT_VERSION="$(sqlplus -V 2>/dev/null | sed -n '/./{p;q;}' || true)"

# Turns a dotted Oracle version such as 19.21.0.0.0 into one integer:
# major * 10000 + second * 100 + third, the scheme MySQL uses (192100).
# Prints nothing when the text is not a version.
oracle_version_number() {
    local major minor patch
    if [[ ! "$1" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
        return
    fi
    IFS=. read -r major minor patch _ <<< "$1"
    echo $(( 10#${major} * 10000 + 10#${minor} * 100 + 10#${patch:-0} ))
}

SERVER_VERSION_NUM="$(oracle_version_number "$INSTANCE_VERSION")"
if [[ -z "$SERVER_VERSION_NUM" ]]; then
    die "could not read the version from v\$instance (is SELECT_CATALOG_ROLE granted to the login?). ${SETUP_FIRST_ERROR}"
fi
if (( SERVER_VERSION_NUM < 110200 )); then
    die "Oracle 11.2 or later is needed (server is ${INSTANCE_VERSION})"
fi
ORACLE_MAJOR_VERSION=$(( SERVER_VERSION_NUM / 10000 ))

# ---------------------------------------------------------------------------
# Sample queries. One sqlplus call per sample (one logon). Every row is one
# string built in SQL and starts with a record letter: S summary, E waits,
# A active sessions. Strings are quoted with their double quotes doubled.
# A missing value stays an empty field, because NULL || ',' is ','.
# ---------------------------------------------------------------------------

# A user session that is working: ACTIVE, not idle-waiting, and not this one.
ACTIVE_USER_SESSION_CONDITION="status = 'ACTIVE'
       AND NOT (state = 'WAITING' AND wait_class = 'Idle')
       AND sid <> TO_NUMBER(SYS_CONTEXT('USERENV', 'SID'))"

# Each inline view below is an aggregate without GROUP BY, so it returns exactly
# one row and the cross join stays one row. Column aliases stay under 30
# characters for Oracle 11.2 and 12.1. limit_value is text and can be
# UNLIMITED, which becomes an empty field.
IFS= read -r -d '' SUMMARY_QUERY <<SQL || true
SELECT 'S'
  || ',' || TO_CHAR(stat.reads, 'TM9')
  || ',' || TO_CHAR(stat.writes, 'TM9')
  || ',' || TO_CHAR(stat.logical_reads, 'TM9')
  || ',' || TO_CHAR(stat.redo_bytes, 'TM9')
  || ',' || TO_CHAR(stat.user_calls, 'TM9')
  || ',' || TO_CHAR(stat.block_gets, 'TM9')
  || ',' || TO_CHAR(stat.consistent_gets, 'TM9')
  || ',' || TO_CHAR(stat.parses, 'TM9')
  || ',' || TO_CHAR(stat.hard_parses, 'TM9')
  || ',' || TO_CHAR(sess.active_all, 'TM9')
  || ',' || TO_CHAR(stat.read_io_requests, 'TM9')
  || ',' || TO_CHAR(stat.write_io_requests, 'TM9')
  || ',' || TO_CHAR(stat.read_bytes, 'TM9')
  || ',' || TO_CHAR(stat.write_bytes, 'TM9')
  || ',' || TO_CHAR(stat.commits, 'TM9')
  || ',' || TO_CHAR(stat.rollbacks, 'TM9')
  || ',' || TO_CHAR(stat.executes, 'TM9')
  || ',' || TO_CHAR(stat.disk_sorts, 'TM9')
  || ',' || TO_CHAR(stat.memory_sorts, 'TM9')
  || ',' || TO_CHAR(stat.redo_space_requests, 'TM9')
  || ',' || TO_CHAR(stat.logons, 'TM9')
  || ',' || TO_CHAR(tmodel.db_time, 'TM9')
  || ',' || TO_CHAR(tmodel.db_cpu, 'TM9')
  || ',' || TO_CHAR(sess.users, 'TM9')
  || ',' || TO_CHAR(sess.users_active, 'TM9')
  || ',' || TO_CHAR(lim.processes_current, 'TM9')
  || ',' || CASE WHEN lim.processes_limit = 'UNLIMITED' THEN NULL ELSE TO_CHAR(TO_NUMBER(lim.processes_limit), 'TM9') END
  || ',' || TO_CHAR(lim.sessions_current, 'TM9')
  || ',' || CASE WHEN lim.sessions_limit = 'UNLIMITED' THEN NULL ELSE TO_CHAR(TO_NUMBER(lim.sessions_limit), 'TM9') END
  || ',' || TO_CHAR(inst.started, 'TM9')
FROM
  (SELECT MAX(DECODE(name, 'physical reads', value)) AS reads,
          MAX(DECODE(name, 'physical writes', value)) AS writes,
          MAX(DECODE(name, 'session logical reads', value)) AS logical_reads,
          MAX(DECODE(name, 'redo size', value)) AS redo_bytes,
          MAX(DECODE(name, 'user calls', value)) AS user_calls,
          MAX(DECODE(name, 'db block gets', value)) AS block_gets,
          MAX(DECODE(name, 'consistent gets', value)) AS consistent_gets,
          MAX(DECODE(name, 'parse count (total)', value)) AS parses,
          MAX(DECODE(name, 'parse count (hard)', value)) AS hard_parses,
          MAX(DECODE(name, 'physical read total IO requests', value)) AS read_io_requests,
          MAX(DECODE(name, 'physical write total IO requests', value)) AS write_io_requests,
          MAX(DECODE(name, 'physical read total bytes', value)) AS read_bytes,
          MAX(DECODE(name, 'physical write total bytes', value)) AS write_bytes,
          MAX(DECODE(name, 'user commits', value)) AS commits,
          MAX(DECODE(name, 'user rollbacks', value)) AS rollbacks,
          MAX(DECODE(name, 'execute count', value)) AS executes,
          MAX(DECODE(name, 'sorts (disk)', value)) AS disk_sorts,
          MAX(DECODE(name, 'sorts (memory)', value)) AS memory_sorts,
          MAX(DECODE(name, 'redo log space requests', value)) AS redo_space_requests,
          MAX(DECODE(name, 'logons current', value)) AS logons
   FROM v\$sysstat
   WHERE name IN ('physical reads', 'physical writes', 'session logical reads',
     'redo size', 'user calls', 'db block gets', 'consistent gets',
     'parse count (total)', 'parse count (hard)',
     'physical read total IO requests', 'physical write total IO requests',
     'physical read total bytes', 'physical write total bytes',
     'user commits', 'user rollbacks', 'execute count',
     'sorts (disk)', 'sorts (memory)', 'redo log space requests',
     'logons current')) stat,
  (SELECT MAX(DECODE(stat_name, 'DB time', value)) AS db_time,
          MAX(DECODE(stat_name, 'DB CPU', value)) AS db_cpu
   FROM v\$sys_time_model
   WHERE stat_name IN ('DB time', 'DB CPU')) tmodel,
  (SELECT NVL(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END), 0) AS active_all,
          NVL(SUM(CASE WHEN type = 'USER' THEN 1 ELSE 0 END), 0) AS users,
          NVL(SUM(CASE WHEN type = 'USER' AND ${ACTIVE_USER_SESSION_CONDITION} THEN 1 ELSE 0 END), 0) AS users_active
   FROM v\$session) sess,
  (SELECT MAX(DECODE(resource_name, 'processes', current_utilization)) AS processes_current,
          MAX(DECODE(resource_name, 'processes', TRIM(limit_value))) AS processes_limit,
          MAX(DECODE(resource_name, 'sessions', current_utilization)) AS sessions_current,
          MAX(DECODE(resource_name, 'sessions', TRIM(limit_value))) AS sessions_limit
   FROM v\$resource_limit
   WHERE resource_name IN ('processes', 'sessions')) lim,
  (SELECT ROUND((startup_time - DATE '1970-01-01') * 86400) AS started
   FROM v\$instance) inst;
SQL

# Wait classes (Idle left out) have times in centiseconds; the events use the
# microsecond columns. The _fg columns count foreground sessions only.
IFS= read -r -d '' WAITS_QUERY <<SQL || true
SELECT 'E,"class","' || REPLACE(wait_class, '"', '""') || '",'
  || TO_CHAR(total_waits, 'TM9')
  || ',' || TO_CHAR(time_waited * 10000, 'TM9')
  || ',' || TO_CHAR(total_waits_fg, 'TM9')
  || ',' || TO_CHAR(time_waited_fg * 10000, 'TM9')
FROM v\$system_wait_class
WHERE wait_class <> 'Idle'
UNION ALL
SELECT 'E,"event","' || REPLACE(event, '"', '""') || '",'
  || TO_CHAR(total_waits, 'TM9')
  || ',' || TO_CHAR(time_waited_micro, 'TM9')
  || ',' || TO_CHAR(total_waits_fg, 'TM9')
  || ',' || TO_CHAR(time_waited_micro_fg, 'TM9')
FROM v\$system_event
WHERE event IN (
  'db file sequential read', 'db file scattered read', 'db file parallel read',
  'direct path read', 'direct path read temp', 'direct path write temp',
  'log file sync', 'log file parallel write', 'log buffer space',
  'log file switch (checkpoint incomplete)', 'log file switch completion',
  'free buffer waits', 'buffer busy waits', 'read by other session',
  'enq: TX - row lock contention', 'enq: TX - index contention', 'enq: TM - contention',
  'latch: cache buffers chains', 'library cache: mutex X', 'cursor: pin S wait on X',
  'resmgr:cpu quantum', 'gc buffer busy acquire', 'gc cr block busy');
SQL

# What the working user sessions are doing right now: waiting in a class, or
# ON CPU (not waiting). A snapshot at the sample instant, not an average.
IFS= read -r -d '' ACTIVE_QUERY <<SQL || true
SELECT 'A,"' || REPLACE(state_class, '"', '""') || '",' || TO_CHAR(COUNT(*), 'TM9')
FROM (SELECT CASE WHEN state = 'WAITING' THEN wait_class ELSE 'ON CPU' END AS state_class
      FROM v\$session
      WHERE type = 'USER' AND ${ACTIVE_USER_SESSION_CONDITION})
GROUP BY state_class;
SQL

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
resolve_output_base
make_run_dir

SUMMARY_HEADER="ts,physical_reads,physical_writes,session_logical_reads,redo_size,user_calls,db_block_gets,consistent_gets,parse_count_total,parse_count_hard,active_sessions,physical_read_total_io_requests,physical_write_total_io_requests,physical_read_total_bytes,physical_write_total_bytes,user_commits,user_rollbacks,execute_count,sorts_disk,sorts_memory,redo_log_space_requests,logons_current,db_time_us,db_cpu_us,user_sessions,user_active_sessions,processes_current,processes_limit,sessions_current,sessions_limit,server_start_marker"
WAITS_HEADER="ts,kind,name,total_waits,time_waited_us,total_waits_fg,time_waited_us_fg"
ACTIVE_HEADER="ts,state_class,sessions"

register_record S oracle_stats.csv "$SUMMARY_HEADER"
register_record E oracle_waits.csv "$WAITS_HEADER"
register_record A oracle_active.csv "$ACTIVE_HEADER"

SAMPLE_SQL_FILE="${OUTDIR}/sample.sql"
printf '%s\n' "$SQLPLUS_SETTINGS" "$SUMMARY_QUERY" "$WAITS_QUERY" "$ACTIVE_QUERY" "EXIT" > "$SAMPLE_SQL_FILE"
OUTFILE="${OUTDIR}/oracle_stats.csv"

# ---------------------------------------------------------------------------
# Identity, settings and file locations. Each statement runs on its own with
# WHENEVER SQLERROR CONTINUE, so one view the login cannot read does not stop
# the others. Columns that older releases lack are only asked for on releases
# that have them.
# ---------------------------------------------------------------------------
PARAMETER_NAMES=(sga_target sga_max_size memory_target pga_aggregate_target db_cache_size processes sessions filesystemio_options disk_asynch_io db_writer_processes resource_manager_plan cpu_count db_block_size cluster_database)
PARAMETER_LIST=""
for parameter_name in "${PARAMETER_NAMES[@]}"; do
    PARAMETER_LIST="${PARAMETER_LIST:+${PARAMETER_LIST}, }'${parameter_name}'"
done

# version_full (18 and later), v$database.cdb and the CON_NAME context (12 and later).
VERSION_FULL_STATEMENT=""
CDB_STATEMENT=""
CON_NAME_STATEMENT=""
if (( ORACLE_MAJOR_VERSION >= 18 )); then
    VERSION_FULL_STATEMENT="SELECT 'version_full=' || version_full FROM v\$instance;"
fi
if (( ORACLE_MAJOR_VERSION >= 12 )); then
    CDB_STATEMENT="SELECT 'cdb=' || cdb FROM v\$database;"
    CON_NAME_STATEMENT="SELECT 'con_name=' || SYS_CONTEXT('USERENV', 'CON_NAME') FROM dual;"
fi

IFS= read -r -d '' IDENTITY_SQL <<SQL || true
WHENEVER SQLERROR CONTINUE
SELECT 'instance_name=' || instance_name FROM v\$instance;
SELECT 'host_name=' || host_name FROM v\$instance;
SELECT 'startup_time=' || TO_CHAR(startup_time, 'YYYY-MM-DD HH24:MI:SS') FROM v\$instance;
SELECT 'database_role=' || database_role FROM v\$database;
SELECT 'log_mode=' || log_mode FROM v\$database;
SELECT 'param.' || name || '=' || value FROM v\$parameter WHERE name IN (${PARAMETER_LIST});
${VERSION_FULL_STATEMENT}
${CDB_STATEMENT}
${CON_NAME_STATEMENT}
SQL
run_setup_sql "$IDENTITY_SQL"

INSTANCE_NAME="$(setup_value instance_name)"
SERVER_HOSTNAME="$(setup_value host_name)"
SERVER_START_TEXT="$(setup_value startup_time)"
CONTAINER_NAME="$(setup_value con_name)"
VERSION_FULL="$(setup_value version_full)"
if [[ -n "$(oracle_version_number "$VERSION_FULL")" ]]; then
    SERVER_VERSION_NUM="$(oracle_version_number "$VERSION_FULL")"
fi

ORACLE_INSTANCE_TEXT="${INSTANCE_NAME:-oracle}"
if [[ -n "$CONTAINER_NAME" ]]; then
    ORACLE_INSTANCE_TEXT="${ORACLE_INSTANCE_TEXT}/${CONTAINER_NAME}"
fi

# Sets CONNECT_HOST to the host in the connect string and CONNECT_HOST_KNOWN to
# yes when it really is a host. A string with no @ goes to a local instance
# (bequeath), which gives an empty host. A TNS alias hides the host, so the
# alias is returned with CONNECT_HOST_KNOWN=no. Nothing before the last @ is
# looked at, so the password is never part of the result.
CONNECT_HOST=""
CONNECT_HOST_KNOWN="yes"
connect_target_host() {
    local target descriptor_host_pattern descriptor_ipc_pattern
    descriptor_host_pattern='[Hh][Oo][Ss][Tt][[:space:]]*=[[:space:]]*([^)[:space:]]+)'
    descriptor_ipc_pattern='[Pp][Rr][Oo][Tt][Oo][Cc][Oo][Ll][[:space:]]*=[[:space:]]*[Ii][Pp][Cc]'
    CONNECT_HOST=""
    CONNECT_HOST_KNOWN="yes"

    if [[ "$CONNSTR" != *@* ]]; then
        if [[ -n "${TWO_TASK:-${LOCAL:-}}" ]]; then
            CONNECT_HOST="${TWO_TASK:-${LOCAL}}"
            CONNECT_HOST_KNOWN="no"
        fi
        return
    fi
    target="${CONNSTR##*@}"

    # A full descriptor: (DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=db1)(PORT=1521))...)
    if [[ "$target" == *\(* ]]; then
        if [[ "$target" =~ $descriptor_host_pattern ]]; then
            CONNECT_HOST="${BASH_REMATCH[1]}"
        elif [[ "$target" =~ $descriptor_ipc_pattern ]]; then
            CONNECT_HOST=""
        else
            CONNECT_HOST="descriptor"
            CONNECT_HOST_KNOWN="no"
        fi
        return
    fi

    # Easy Connect: [//]host[:port][/service], also tcp:// and tcps://, with an
    # optional "as sysdba" after it.
    target="${target%%[[:space:]]*}"
    target="${target//\"/}"
    if [[ "$target" != */* && "$target" != *:* ]]; then
        CONNECT_HOST="$target"
        CONNECT_HOST_KNOWN="no"
        return
    fi
    target="${target#*://}"
    target="${target#//}"
    if [[ "$target" == \[* ]]; then
        target="${target#\[}"
        CONNECT_HOST="${target%%\]*}"
    else
        CONNECT_HOST="${target%%[:/?]*}"
    fi
}

connect_target_host
resolve_target_locality "$CONNECT_HOST" "$SERVER_HOSTNAME"
if [[ "$CONNECT_HOST_KNOWN" == "no" && "$TARGET_LOCAL" == "no" ]]; then
    TARGET_LOCAL="unknown"
fi

# No target= key: the connect string can hold a password.
manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
manifest_add client_version "${CLIENT_VERSION:-unknown}"
manifest_add server_version "${SERVER_VERSION:-unknown}"
manifest_add_db_identity oracle "$ORACLE_INSTANCE_TEXT" "$SERVER_HOSTNAME" "$SERVER_START_TEXT" "$SERVER_VERSION_NUM"

for setting_name in database_role log_mode cdb con_name; do
    setting_value="$(setup_value "$setting_name")"
    if [[ -n "$setting_value" ]]; then
        record_setting "$setting_name" "$setting_value"
    fi
done
for parameter_name in "${PARAMETER_NAMES[@]}"; do
    setting_value="$(setup_value "param.${parameter_name}")"
    if [[ -n "$setting_value" ]]; then
        record_setting "$parameter_name" "$setting_value"
    fi
done

if [[ "$(setup_value param.cluster_database)" == "TRUE" ]]; then
    note "cluster_database is TRUE: the figures are for the instance ${INSTANCE_NAME:-you connected to} only, not for the whole RAC database"
fi
if [[ -n "$CONTAINER_NAME" && "$(setup_value cdb)" == "YES" && "$CONTAINER_NAME" != "CDB\$ROOT" ]]; then
    note "connected to pluggable database ${CONTAINER_NAME}: instance statistics may describe this container only. Connect to CDB\$ROOT for instance-wide figures"
fi

# Data, temp and redo files. Names starting with + are in ASM: the disk group
# is recorded as the directory and its disks are added afterwards. The disk
# views are only read when an ASM name was seen.
add_file_paths() {
    local line role file_name uses_asm="no" disk_path files_sql disks_sql
    IFS= read -r -d '' files_sql <<SQL || true
WHENEVER SQLERROR CONTINUE
SELECT 'data=' || name FROM v\$datafile;
SELECT 'temp=' || name FROM v\$tempfile;
SELECT 'redo=' || member FROM v\$logfile;
SQL
    run_setup_sql "$files_sql"
    while IFS= read -r line; do
        case "$line" in
            data=*|temp=*|redo=*) ;;
            *) continue ;;
        esac
        role="${line%%=*}"
        file_name="${line#*=}"
        if [[ "$file_name" == +* ]]; then
            add_db_path "$role" "${file_name%%/*}"
            uses_asm="yes"
        elif [[ "$file_name" == */* ]]; then
            add_db_path "$role" "${file_name%/*}"
        fi
    done <<< "$SETUP_OUTPUT"

    if [[ "$uses_asm" == "no" ]]; then
        return
    fi
    IFS= read -r -d '' disks_sql <<SQL || true
WHENEVER SQLERROR CONTINUE
SELECT 'asm_disk=' || path FROM v\$asm_disk_stat WHERE group_number > 0 AND path IS NOT NULL;
SQL
    run_setup_sql "$disks_sql"
    while IFS= read -r line; do
        if [[ "$line" != asm_disk=* ]]; then
            continue
        fi
        disk_path="${line#*=}"
        case "$disk_path" in
            AFD:*|ORCL:*)
                add_db_path asm_disk "$disk_path" "ASM Filter Driver or ASMLib label; it cannot be mapped to a block device here" ;;
            *)
                add_db_path asm_disk "$disk_path" ;;
        esac
    done <<< "$SETUP_OUTPUT"
    if [[ -z "${DB_PATH_COUNT[asm_disk]-}" ]]; then
        note "the database files are in ASM but the ASM disks could not be read from v\$asm_disk_stat, so they cannot be placed on devices"
    elif grep -qE '^path\.asm_disk\.[0-9]+\.note=' "${OUTDIR}/db_paths.txt"; then
        note "some ASM disks are AFD or ASMLib labels and cannot be mapped to block devices"
    fi
}

if [[ "$TARGET_LOCAL" == "yes" ]]; then
    add_file_paths
fi

if (( SETUP_ERRORS > 0 )); then
    note "${SETUP_ERRORS} setup query error(s), the first was: ${SETUP_FIRST_ERROR}. Some identity, settings or file locations are missing; grant SELECT_CATALOG_ROLE to the login"
fi
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: ${SERVER_VERSION:-unknown} (${ORACLE_INSTANCE_TEXT}, on this host: ${TARGET_LOCAL})"

# The timestamp is the collector's clock, not the server's. sqlplus reports
# errors on standard output, so error text is routed like any other row and
# ends up in errors.log, not in a CSV.
collect_one_sample() {
    local ts output status=0
    ts="$(date -Iseconds)"
    output="$(sqlplus_session_script < "$SAMPLE_SQL_FILE" | run_with_timeout sqlplus -s /nolog 2>>"$ERROR_LOG")" || status=$?
    route_sample_output "$ts" "$output"
    note_sample_failure "$ts" "$status"
}

run_collection
