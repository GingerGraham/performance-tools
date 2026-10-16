#!/usr/bin/env bash
#
# collect-mssql-metrics.sh
# Samples SQL Server performance counters (sys.dm_os_performance_counters)
# through sqlcmd at a fixed interval for a fixed duration, matching the
# OS-level sadc/pidstat window.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="2.0.0"
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
      --database    Database name                (default: master)
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to take the first sample, then detach
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Authentication: set the SQLCMDPASSWORD environment variable for SQL logins.
A password is never accepted as an option.

Output goes to <outdir>/<host>_mssql_<timestamp>/ and holds mssql_stats.csv,
manifest.txt, errors.log and (detached runs) collector.pid and collector.log.
Stop a detached run with: kill \$(cat <outdir>/collector.pid)

Columns ending _total are cumulative counters since server start; work out
rates from the difference between samples.

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

SQLCMD_ARGS=(-S "$SQL_SERVER" -d "$SQL_DATABASE")
if sqlcmd_supports_tls_flags; then
    SQLCMD_ARGS+=(-C -N)
else
    note "sqlcmd at ${SQLCMD_BIN} does not list -C/-N; connecting without them"
fi
if [[ -n "$SQL_USER" ]]; then
    # The password comes from SQLCMDPASSWORD, which sqlcmd reads itself.
    SQLCMD_ARGS+=(-U "$SQL_USER")
fi

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

SERVER_VERSION="$("$SQLCMD_BIN" "${SQLCMD_ARGS[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT CAST(SERVERPROPERTY('ProductVersion') AS varchar(50));" 2>/dev/null | sed -n '/./{p;q;}' || true)"
CLIENT_VERSION="$(printf '%s\n' "$SQLCMD_HELP_TEXT" | sed -n '/./p' | head -n 2 | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
resolve_output_base
make_run_dir
OUTFILE="${OUTDIR}/mssql_stats.csv"

manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
if [[ -n "$SQL_USER" ]]; then
    manifest_add target "${SQL_USER}@${SQL_SERVER}/${SQL_DATABASE}"
else
    manifest_add target "${SQL_SERVER}/${SQL_DATABASE}"
fi
manifest_add client_version "${CLIENT_VERSION:-unknown}"
manifest_add server_version "${SERVER_VERSION:-unknown}"
manifest_add notes "$NOTES_TEXT"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION_SECS}s  Interval: ${INTERVAL_SECS}s  Samples: ${SAMPLE_COUNT}"
echo "Server: SQL Server ${SERVER_VERSION:-unknown}"

# Buffer cache hit ratio is only meaningful as value / base, as a percentage.
# The per-second counters in this view are cumulative since server start, so
# those columns are named *_total; work out rates from the difference.
QUERY="SET NOCOUNT ON;
SELECT
  CAST(100.0 * MAX(CASE WHEN counter_name = 'Buffer cache hit ratio' THEN cntr_value END)
       / NULLIF(MAX(CASE WHEN counter_name = 'Buffer cache hit ratio base' THEN cntr_value END), 0)
       AS DECIMAL(6,2)) AS buffer_cache_hit_ratio,
  MAX(CASE WHEN counter_name = 'Page life expectancy' THEN cntr_value END) AS page_life_expectancy,
  MAX(CASE WHEN counter_name = 'Checkpoint pages/sec' THEN cntr_value END) AS checkpoint_pages_total,
  MAX(CASE WHEN counter_name = 'Lazy writes/sec' THEN cntr_value END) AS lazy_writes_total,
  MAX(CASE WHEN counter_name = 'Memory Grants Pending' THEN cntr_value END) AS memory_grants_pending,
  MAX(CASE WHEN counter_name = 'Target Server Memory (KB)' THEN cntr_value END) AS target_server_memory_kb,
  MAX(CASE WHEN counter_name = 'Total Server Memory (KB)' THEN cntr_value END) AS total_server_memory_kb,
  MAX(CASE WHEN counter_name = 'Batch Requests/sec' THEN cntr_value END) AS batch_requests_total,
  MAX(CASE WHEN counter_name = 'SQL Compilations/sec' THEN cntr_value END) AS sql_compilations_total,
  MAX(CASE WHEN counter_name = 'SQL Re-Compilations/sec' THEN cntr_value END) AS sql_recompilations_total
FROM sys.dm_os_performance_counters
WHERE object_name LIKE '%Buffer Manager%'
   OR object_name LIKE '%Memory Manager%'
   OR object_name LIKE '%SQL Statistics%';"

echo "ts,buffer_cache_hit_ratio,page_life_expectancy,checkpoint_pages_total,lazy_writes_total,memory_grants_pending,target_server_memory_kb,total_server_memory_kb,batch_requests_total,sql_compilations_total,sql_recompilations_total" > "$OUTFILE"

# The timestamp is the collector's clock, not the server's. sqlcmd reports
# errors on stdout, so anything that is not a row of numbers goes to
# errors.log instead of the CSV.
collect_one_sample() {
    local ts output
    ts="$(date -Iseconds)"
    output="$("$SQLCMD_BIN" "${SQLCMD_ARGS[@]}" -h -1 -W -s',' -Q "$QUERY" 2>>"$ERROR_LOG" | sed '/^$/d;/rows affected/d' || true)"
    # A counter that is missing from the view prints as NULL.
    if [[ "$output" =~ ^(NULL|[0-9.-]*)(,(NULL|[0-9.-]*))*$ && -n "$output" ]]; then
        echo "${ts},${output}" >> "$OUTFILE"
    elif [[ -n "$output" ]]; then
        echo "$output" >> "$ERROR_LOG"
    fi
}

run_collection
