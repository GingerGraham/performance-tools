#!/usr/bin/env bash
#
# collect-os-metrics.sh
# Cross-distro sysstat (sadc) + pidstat baseline collector.
# Supports: Debian, Ubuntu, RHEL, Rocky, AlmaLinux, Oracle Linux, CentOS,
# Amazon Linux, Fedora, Azure Linux, SLES.
#
# Runs sadc and pidstat detached (systemd transient units where systemd is
# init, setsid/nohup otherwise). Each run writes manifest.txt and readback.txt
# next to the data files.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="2.0.0"
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

# IANA zone name (Europe/London), or empty when it cannot be found. The
# timezone key above is a fixed offset; perfdiag needs the name to follow a
# daylight-saving change inside a multi-day capture.
detect_tz_name() {
    local value=""
    if [[ -n "${TZ:-}" && "${TZ}" != :* ]]; then
        echo "$TZ"
        return 0
    fi
    if command -v timedatectl >/dev/null 2>&1; then
        value="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    fi
    if [[ -z "$value" && -L /etc/localtime ]]; then
        value="$(readlink -f /etc/localtime)"
        value="${value#*/zoneinfo/}"
    fi
    if [[ -z "$value" && -r /etc/timezone ]]; then
        value="$(head -n 1 /etc/timezone)"
    fi
    echo "$value"
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
        echo "tz_name=$(detect_tz_name)"
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
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [-d duration | -c count] [-i interval] [-o outdir]
                      [-p process[,process...]] [--extended] [--threads]
                      [--foreground] [-h] [-V]

  -d, --duration    Total run time: 30, 30s, 15m, 2h, 1d. A bare number is
                    seconds. Prompted for if omitted and a terminal is present
  -c, --count       Number of samples. Mutually exclusive with -d
  -i, --interval    Sample interval: 15, 15s, 1m. A bare number is seconds.
                    Prompted for if omitted and a terminal is present
  -o, --outdir      Base output directory. Default: /var/log/perf-diag
  -p, --process     Comma-separated process names for the pidstat -C filter.
                    Omit to have pidstat report on all processes
      --extended    Collect every sadc activity group (sadc -S XALL), adding
                    per-IRQ and power counters. Output grows with CPU count
      --threads     Add per-thread rows to the pidstat log (pidstat -t)
      --foreground  Stay attached to the terminal; Ctrl-C stops collection.
                    Default is to detach and keep running after logout
  -h, --help        Show this help and exit
  -V, --version     Show the script name and version and exit

Output goes to <outdir>/<host>_os_<timestamp>/ and holds the sadc binary
file, the pidstat log, manifest.txt (run metadata) and readback.txt (commands
to read the data back).

Collection needs root. When not run as root, the script uses sudo.

Examples:
  ${SCRIPT_NAME} -d 2h -i 30s -p postgres,pgbouncer
  ${SCRIPT_NAME} -c 20 -i 5 --foreground
EOF
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
DURATION=""
COUNT=""
INTERVAL=""
OUTDIR_BASE="/var/log/perf-diag"
PROCESS_FILTER=""
EXTENDED="no"
THREADS="no"
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
        -p|--process|--process=*)   take_value "$arg" "${2-}" "$has_next"; PROCESS_FILTER="$OPT_VALUE" ;;
        --extended)   EXTENDED="yes"; OPT_SHIFT=1 ;;
        --threads)    THREADS="yes"; OPT_SHIFT=1 ;;
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
if [[ -n "$PROCESS_FILTER" && "$PROCESS_FILTER" =~ ^[,]*$ ]]; then
    usage_error "-p/--process needs at least one process name"
fi

resolve_timing

# ---------------------------------------------------------------------------
# Privileges
# ---------------------------------------------------------------------------
# All privileged commands go through ROOT_CMD, a command prefix that is empty
# when running as root (so the script works where sudo is not installed) and
# "sudo" otherwise.
#
# Background launches use the prefix directly instead of a function. A function
# would start a wrapper subshell, and $! would then be the wrapper's PID rather
# than the collector's (or sudo's), so the printed stop command would not work.
ROOT_CMD=()
STOP_PREFIX=""
if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || die "not running as root and sudo is not installed"
    echo "Checking sudo privileges..."
    sudo -v || die "this script needs root to run sadc and pidstat. Run it as root or as a user with sudo rights"
    ROOT_CMD=(sudo)
    STOP_PREFIX="sudo "
fi

as_root() {
    ${ROOT_CMD[@]+"${ROOT_CMD[@]}"} "$@"
}

# ---------------------------------------------------------------------------
# Distro, package manager
# ---------------------------------------------------------------------------
if [[ ! -r /etc/os-release ]]; then
    die "cannot read /etc/os-release. Hosts without it (RHEL/CentOS 6 and older) are not supported"
fi
# shellcheck disable=SC1091
source /etc/os-release

FAMILY="unknown"
case "${ID:-}" in
    ubuntu|debian)                                              FAMILY="debian" ;;
    rhel|rocky|almalinux|ol|centos|fedora|amzn|mariner|azurelinux) FAMILY="rhel" ;;
    sles|sled|opensuse*|suse)                                   FAMILY="suse" ;;
    *)
        case "${ID_LIKE:-}" in
            *debian*)        FAMILY="debian" ;;
            *rhel*|*fedora*) FAMILY="rhel" ;;
            *suse*)          FAMILY="suse" ;;
        esac
        ;;
esac

if [[ "$FAMILY" == "unknown" ]]; then
    warn "unrecognised distro (ID=${ID:-unset}, ID_LIKE=${ID_LIKE:-unset}). Continuing, but package and path detection may be wrong"
fi

echo "Detected distro: ${PRETTY_NAME:-${ID:-unknown}} (family: ${FAMILY})"

# The install command depends on the package manager present, not the distro
# family: Amazon Linux 2 and RHEL 7 use yum, Azure Linux uses tdnf.
install_hint() {
    local sudo_prefix="$STOP_PREFIX"
    if [[ "${ID:-}" == "mariner" || "${ID:-}" == "azurelinux" ]] && command -v tdnf >/dev/null 2>&1; then
        echo "${sudo_prefix}tdnf install -y sysstat"
    elif command -v dnf >/dev/null 2>&1; then
        echo "${sudo_prefix}dnf install -y sysstat"
    elif command -v yum >/dev/null 2>&1; then
        echo "${sudo_prefix}yum install -y sysstat"
    elif command -v tdnf >/dev/null 2>&1; then
        echo "${sudo_prefix}tdnf install -y sysstat"
    elif command -v apt-get >/dev/null 2>&1; then
        echo "${sudo_prefix}apt-get update && ${sudo_prefix}apt-get install -y sysstat"
    elif command -v zypper >/dev/null 2>&1; then
        echo "${sudo_prefix}zypper install -y sysstat"
    else
        echo "Install the 'sysstat' package using your distro's package manager."
    fi
}

# ---------------------------------------------------------------------------
# sysstat presence
# ---------------------------------------------------------------------------
find_sadc() {
    local candidates=(
        /usr/lib64/sa/sadc          # RHEL family, Fedora, SLES
        /usr/lib/sa/sadc            # Arch, upstream default builds
        /usr/libexec/sysstat/sadc   # current Debian / Ubuntu
        /usr/lib/sysstat/sadc       # older Debian / Ubuntu
        /usr/libexec/sa/sadc        # possible future RHEL/Fedora FHS move
    )
    local path=""

    for path in "${candidates[@]}"; do
        if [[ -x "$path" ]]; then
            echo "$path"
            return 0
        fi
    done

    # Fall back to asking the package manager where sadc was installed.
    path=""
    if command -v dpkg-query >/dev/null 2>&1; then
        path="$(dpkg-query -L sysstat 2>/dev/null | grep -E '/sadc$' | head -n 1 || true)"
    fi
    if [[ -z "$path" ]] && command -v rpm >/dev/null 2>&1; then
        path="$(rpm -ql sysstat 2>/dev/null | grep -E '/sadc$' | head -n 1 || true)"
    fi
    if [[ -n "$path" && -x "$path" ]]; then
        echo "$path"
        return 0
    fi

    if command -v sadc >/dev/null 2>&1; then
        command -v sadc
        return 0
    fi

    return 1
}

sysstat_pkg_installed() {
    if command -v dpkg-query >/dev/null 2>&1; then
        dpkg-query -W -f='${Status}' sysstat 2>/dev/null | grep -q 'install ok installed'
    elif command -v rpm >/dev/null 2>&1; then
        rpm -q sysstat >/dev/null 2>&1
    else
        return 1
    fi
}

if ! SADC_PATH="$(find_sadc)"; then
    echo "" >&2
    if sysstat_pkg_installed; then
        echo "sysstat is installed, but the sadc binary could not be located." >&2
        echo "Find it with one of:" >&2
        echo "  dpkg-query -L sysstat | grep '/sadc\$'   # Debian/Ubuntu" >&2
        echo "  rpm -ql sysstat | grep '/sadc\$'         # RHEL/Fedora/SLES" >&2
        echo "Then add that path to the candidates list in find_sadc()." >&2
    else
        echo "sysstat is not installed (sadc binary not found)." >&2
        echo "Run the following, then re-run this script:" >&2
        echo "  $(install_hint)" >&2
    fi
    exit 1
fi

if ! command -v sar >/dev/null 2>&1 || ! command -v pidstat >/dev/null 2>&1; then
    echo "" >&2
    echo "sysstat appears partially installed (sadc found at ${SADC_PATH}, but 'sar' or 'pidstat' is missing from PATH)." >&2
    echo "Reinstall with:" >&2
    echo "  $(install_hint)" >&2
    exit 1
fi

SYSSTAT_VERSION_LINE="$(sar -V 2>&1 | head -n 1 || true)"
SYSSTAT_VERSION="${SYSSTAT_VERSION_LINE##* }"
echo "sysstat OK (${SYSSTAT_VERSION_LINE}; sadc: ${SADC_PATH})"

# ---------------------------------------------------------------------------
# Feature probes. Each optional feature is tested on this host, not assumed
# from the sysstat version.
# ---------------------------------------------------------------------------

# With no outfile, sadc writes one record to stdout and exits; an unknown
# keyword prints usage and exits non-zero. Do not pass "-" as the outfile: that
# means the standard daily file (saDD), which the probe would write to.
sadc_accepts() {
    "$SADC_PATH" -S "$1" 1 1 >/dev/null 2>&1
}

pidstat_accepts() {
    LC_ALL=C pidstat "$@" 1 1 >/dev/null 2>&1
}

# sar -F (filesystem report) needs sysstat 10.1.6.
sar_supports_filesystems() {
    LC_ALL=C sar -F 1 1 >/dev/null 2>&1
}

if [[ "$EXTENDED" == "yes" ]]; then
    WANTED_KEYWORDS="XALL"
else
    WANTED_KEYWORDS="XDISK SNMP IPV6"
fi

SADC_KEYWORDS=""
for keyword in $WANTED_KEYWORDS; do
    if sadc_accepts "$keyword"; then
        SADC_KEYWORDS="${SADC_KEYWORDS:+${SADC_KEYWORDS},}${keyword}"
    else
        note "sadc on this host does not accept -S ${keyword}; skipped"
    fi
done

if [[ -n "$SADC_KEYWORDS" ]] && ! sadc_accepts "$SADC_KEYWORDS"; then
    note "sadc does not accept a comma-separated -S list; falling back to -S DISK"
    SADC_KEYWORDS="DISK"
fi
if [[ -z "$SADC_KEYWORDS" ]]; then
    SADC_KEYWORDS="DISK"
fi

# -h: one line per task per interval with all metric groups.
# -l: full command line (last column).
PIDSTAT_FLAGS=(-u -r -d -w -h -l)
for optional_flag in -v -H; do
    if pidstat_accepts "$optional_flag"; then
        PIDSTAT_FLAGS+=("$optional_flag")
    else
        note "pidstat on this host does not support ${optional_flag}; skipped"
    fi
done
if [[ "$THREADS" == "yes" ]]; then
    PIDSTAT_FLAGS+=(-t)
fi
if [[ -n "$PROCESS_FILTER" ]]; then
    PIDSTAT_FLAGS+=(-C "${PROCESS_FILTER//,/|}")
fi

# ---------------------------------------------------------------------------
# Run directory and metadata
# ---------------------------------------------------------------------------
# Transient systemd units start in /, so a relative -o would put the data
# files there instead of in the run directory. Make the base absolute.
case "$OUTDIR_BASE" in
    /*) ;;
    *)  OUTDIR_BASE="${PWD}/${OUTDIR_BASE}" ;;
esac

TS="$(date +%Y%m%d_%H%M%S)"
HOST_SHORT="$(short_hostname)"
OUTDIR="${OUTDIR_BASE}/${HOST_SHORT}_os_${TS}"

as_root mkdir -p "$OUTDIR"
# Only the run directory changes owner, so the invoking user can write the
# PID files and read the results. The base directory stays as it was.
as_root chown "$(id -u):$(id -g)" "$OUTDIR"

SADC_OUT="${OUTDIR}/sa_${TS}"
PIDSTAT_OUT="${OUTDIR}/pidstat_${TS}.log"
SADC_COUNT=$(( SAMPLE_COUNT + 1 ))   # sadc's first record is a baseline; N+1 records give N intervals

if [[ "$FOREGROUND" == "yes" ]]; then
    LAUNCHER="foreground"
elif [[ -d /run/systemd/system ]]; then
    LAUNCHER="systemd"
else
    LAUNCHER="nohup"
    note "systemd is not running as init; using setsid/nohup. Stop with the commands printed below"
fi

# True when the sadc keyword (or XALL, which includes every group) was collected.
keyword_collected() {
    case ",${SADC_KEYWORDS}," in
        *",$1,"*|*",XALL,"*) return 0 ;;
    esac
    return 1
}

write_readback() {
    local sar_prefix="LC_ALL=C sar -f ${SADC_OUT}"
    {
        echo "# Read back on this host, or on any host with sysstat >= ${SYSSTAT_VERSION}."
        echo "${sar_prefix} -u ALL -P ALL      # CPU, per CPU"
        echo "${sar_prefix} -q                 # run queue, load (-q ALL adds pressure stall on sysstat 12.3.3+)"
        echo "${sar_prefix} -w                 # context switches"
        echo "${sar_prefix} -r ALL             # memory"
        echo "${sar_prefix} -B                 # paging"
        echo "${sar_prefix} -S -W              # swap space and swap rate"
        echo "${sar_prefix} -d -p              # block devices"
        if keyword_collected XDISK && sar_supports_filesystems; then
            echo "${sar_prefix} -F                 # filesystems"
        fi
        echo "${sar_prefix} -n DEV,EDEV        # interfaces"
        if keyword_collected SNMP; then
            echo "${sar_prefix} -n TCP,ETCP,UDP,IP,EIP,SOCK   # TCP, UDP, IP"
        fi
        if keyword_collected IPV6; then
            echo "${sar_prefix} -n IP6,EIP6,UDP6,SOCK6   # IPv6"
        fi
        if keyword_collected INT; then
            echo "${sar_prefix} -I ALL             # interrupts"
        fi
        echo "less ${PIDSTAT_OUT}"
        echo ""
        echo "# CSV for analysis scripts (semicolon-separated, one file per activity group):"
        echo "LC_ALL=C sadf -d ${SADC_OUT} -- -u ALL -P ALL > cpu.csv"
        echo "LC_ALL=C sadf -d ${SADC_OUT} -- -d -p        > disk.csv"
        echo ""
        echo "# Off-host analysis: gather-perf-diag.sh bundles this run with CSV exports for perfdiag."
    } > "${OUTDIR}/readback.txt"
}

manifest_start "${OUTDIR}/manifest.txt" "$INTERVAL_SECS" "$SAMPLE_COUNT" "$DURATION_SECS"
manifest_add sysstat_version "$SYSSTAT_VERSION_LINE"
manifest_add sadc_keywords "$SADC_KEYWORDS"
manifest_add pidstat_flags "${PIDSTAT_FLAGS[*]}"
manifest_add launcher "$LAUNCHER"
manifest_add notes "$NOTES_TEXT"

write_readback

echo ""
echo "Duration:  ${DURATION_SECS}s"
echo "Interval:  ${INTERVAL_SECS}s"
echo "Samples:   ${SAMPLE_COUNT}"
echo ""
echo "Output directory: ${OUTDIR}"
echo "  sadc binary log:  ${SADC_OUT}"
echo "  pidstat log:      ${PIDSTAT_OUT}"
echo "  manifest:         ${OUTDIR}/manifest.txt"
echo "  read-back hints:  ${OUTDIR}/readback.txt"

# ---------------------------------------------------------------------------
# Launchers
#
# Values reach bash -c as positional parameters, never by string
# interpolation, so a quote in -p cannot break the command. $0 is the output
# file and "$@" is the pidstat argument list.
# ---------------------------------------------------------------------------
# Single quotes are intended: $@ and $0 are expanded by the inner bash.
# shellcheck disable=SC2016
PIDSTAT_RUNNER='exec pidstat "$@" > "$0"'

SADC_UNIT="perf-diag-sadc-${TS}"
PIDSTAT_UNIT="perf-diag-pidstat-${TS}"

launch_systemd() {
    echo ""
    echo "Starting sadc collection (unit: ${SADC_UNIT})..."
    as_root systemd-run --unit="$SADC_UNIT" --description="perf-diag sadc ${TS}" \
        "$SADC_PATH" -S "$SADC_KEYWORDS" "$INTERVAL_SECS" "$SADC_COUNT" "$SADC_OUT"

    echo "Starting pidstat collection (unit: ${PIDSTAT_UNIT})..."
    as_root systemd-run --unit="$PIDSTAT_UNIT" --description="perf-diag pidstat ${TS}" \
        --setenv=LC_ALL=C --setenv=S_TIME_FORMAT=ISO \
        bash -c "$PIDSTAT_RUNNER" \
        "$PIDSTAT_OUT" "${PIDSTAT_FLAGS[@]}" "$INTERVAL_SECS" "$SAMPLE_COUNT"

    echo ""
    echo "Collection running. It stops by itself after ${SAMPLE_COUNT} samples."
    echo ""
    echo "Check status:"
    echo "  systemctl status ${SADC_UNIT} ${PIDSTAT_UNIT}"
    echo "Stop early:"
    echo "  ${STOP_PREFIX}systemctl stop ${SADC_UNIT} ${PIDSTAT_UNIT}"
}

launch_nohup() {
    # The redirections and PID files are written by this (invoking) shell. That
    # works because the run directory belongs to the invoking user.
    echo ""
    echo "Starting sadc and pidstat with setsid/nohup..."
    ${ROOT_CMD[@]+"${ROOT_CMD[@]}"} setsid nohup "$SADC_PATH" -S "$SADC_KEYWORDS" "$INTERVAL_SECS" "$SADC_COUNT" "$SADC_OUT" \
        > "${OUTDIR}/sadc.stderr" 2>&1 < /dev/null &
    echo "$!" > "${OUTDIR}/sadc.pid"

    ${ROOT_CMD[@]+"${ROOT_CMD[@]}"} setsid nohup env LC_ALL=C S_TIME_FORMAT=ISO \
        bash -c "$PIDSTAT_RUNNER" \
        "$PIDSTAT_OUT" "${PIDSTAT_FLAGS[@]}" "$INTERVAL_SECS" "$SAMPLE_COUNT" \
        > "${OUTDIR}/pidstat.stderr" 2>&1 < /dev/null &
    echo "$!" > "${OUTDIR}/pidstat.pid"

    # When as_root is sudo, the PID is sudo's. Killing sudo relays the signal
    # to the command it started.
    echo ""
    echo "Collection running. It stops by itself after ${SAMPLE_COUNT} samples."
    echo ""
    echo "Check status:"
    echo "  ps -p \$(cat ${OUTDIR}/sadc.pid),\$(cat ${OUTDIR}/pidstat.pid)"
    echo "Stop early:"
    echo "  ${STOP_PREFIX}kill \$(cat ${OUTDIR}/sadc.pid) \$(cat ${OUTDIR}/pidstat.pid)"
}

FOREGROUND_PIDS=""

stop_foreground() {
    echo "" >&2
    echo "Stopping collection..." >&2
    # shellcheck disable=SC2086
    as_root kill $FOREGROUND_PIDS 2>/dev/null || true
}

launch_foreground() {
    local sadc_pid pidstat_pid
    echo ""
    echo "Collecting in the foreground. Press Ctrl-C to stop."

    ${ROOT_CMD[@]+"${ROOT_CMD[@]}"} "$SADC_PATH" -S "$SADC_KEYWORDS" "$INTERVAL_SECS" "$SADC_COUNT" "$SADC_OUT" &
    sadc_pid=$!
    ${ROOT_CMD[@]+"${ROOT_CMD[@]}"} env LC_ALL=C S_TIME_FORMAT=ISO \
        bash -c "$PIDSTAT_RUNNER" \
        "$PIDSTAT_OUT" "${PIDSTAT_FLAGS[@]}" "$INTERVAL_SECS" "$SAMPLE_COUNT" &
    pidstat_pid=$!
    FOREGROUND_PIDS="${sadc_pid} ${pidstat_pid}"

    trap stop_foreground INT TERM
    # A trapped signal ends the first wait early, so wait again to reap both.
    wait "$sadc_pid" || true
    wait "$pidstat_pid" || true
    wait "$sadc_pid" 2>/dev/null || true
    wait "$pidstat_pid" 2>/dev/null || true
    trap - INT TERM

    manifest_finish
    echo "Done. Output is in ${OUTDIR}"
}

case "$LAUNCHER" in
    foreground) launch_foreground ;;
    systemd)    launch_systemd ;;
    nohup)      launch_nohup ;;
esac

echo ""
echo "Read back with the commands in ${OUTDIR}/readback.txt"
