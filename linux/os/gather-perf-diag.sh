#!/usr/bin/env bash
#
# gather-perf-diag.sh
# Bundles recent perf-diag run directories into one .tar.gz (or .zip) for
# analysis off the host with perfdiag. Every sadc binary file is converted to
# CSV here with the sadf that matches the sadc that wrote it, so the
# workstation never needs sysstat. pidstat logs are redacted by default.
#
# This script samples nothing and makes no network calls.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="2.0.0"
ORIGINAL_ARGS=("$@")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
# Shared block E: database server signatures (spec section 7.4).
# Identical in gather-perf-diag.sh, collect-os-metrics.sh and
# collect-hw-inventory.sh. perfdiag uses the same table.
# ---------------------------------------------------------------------------
# Sets DB_ENGINE (postgresql, mysql, oracle, mssql, or empty) and DB_INSTANCE
# for a command line, the args column of ps. The match is on the basename of
# the first argument, never on comm: comm is cut at 15 characters, so
# ora_pmon_ORCLCDB would read ora_pmon_ORCLCD and the SID would be wrong.
# Not matched on purpose: mysqld_safe, mysqld_exporter, postgres_exporter and
# asm_pmon_+ASM (an ASM instance is not a database).
classify_db_process() {
    local first="${1%% *}" name
    name="${first##*/}"
    DB_ENGINE=""
    DB_INSTANCE=""
    case "$name" in
        postgres|postmaster|postgres:) DB_ENGINE="postgresql" ;;   # "postgres:" is a retitled backend
        mysqld|mariadbd)               DB_ENGINE="mysql" ;;
        ora_pmon_?*)                   DB_ENGINE="oracle"; DB_INSTANCE="${name#ora_pmon_}" ;;   # background process
        oracle[A-Za-z0-9_]*)           DB_ENGINE="oracle"; DB_INSTANCE="${name#oracle}" ;;      # foreground process
        sqlservr)                      DB_ENGINE="mssql" ;;
    esac
}

# Reads "pid ppid user args" lines on stdin (ps -eo pid=,ppid=,user=,args=).
# Prints engine|pid|ppid|user|instance|args for each database server process.
db_scan() {
    local pid ppid user args
    while read -r pid ppid user args; do
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        classify_db_process "$args"
        [[ -n "$DB_ENGINE" ]] || continue
        printf '%s|%s|%s|%s|%s|%s\n' "$DB_ENGINE" "$pid" "$ppid" "$user" "$DB_INSTANCE" "$args"
    done
}

# Reads db_scan output on stdin. Prints the engines found, comma separated, in
# a fixed order: postgresql,mysql,oracle,mssql. Prints an empty line for none.
db_engines_detected() {
    local line engine found=" " result=""
    while IFS= read -r line; do
        found+="${line%%|*} "
    done
    for engine in postgresql mysql oracle mssql; do
        case "$found" in
            *" ${engine} "*) result="${result:+${result},}${engine}" ;;
        esac
    done
    echo "$result"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [--since duration] [--run dir]... [--base dir]... [-o outdir]
                      [--format tar.gz|zip] [--no-kernel-log] [--no-redact]
                      [--no-inventory] [-h] [-V]

  --since        Include run directories whose name timestamp is within this
                 period: 2h, 3d, or a bare number of seconds. Default: 7d
  --run          Include this run directory. Repeatable. When given, --since
                 is ignored
  --base         Where run directories are found. Repeatable. When given,
                 only these directories are searched. Default: /var/log/perf-diag
                 and ~/perf-diag of the invoking user, of SUDO_USER and of the
                 postgres, mysql, oracle and mssql accounts, where they exist
  -o, --outdir   Where the bundle is written. Default: the current directory
  --format       tar.gz (default) or zip. zip needs the zip command, which
                 minimal server images often lack; tar.gz needs nothing extra
  --no-kernel-log  Skip the kernel log excerpt
  --no-redact    Copy pidstat logs without command-line redaction
  --no-inventory   Do not run or require a hardware inventory
  -h, --help     Show this help and exit
  -V, --version  Show the script name and version and exit

Writes <outdir>/<host>_gather_<timestamp>.tar.gz holding the selected run
directories, a CSV export of every sadc file (sadf/), kernel_events.log and
gather_manifest.txt with a SHA-256 for every file.

Redaction: pidstat -l records full command lines, which can hold passwords.
The copies in the bundle have password/secret/token/api-key arguments,
-p<value> arguments and *PASSWORD*/*SECRET*/*TOKEN* environment assignments
replaced with <redacted>. The originals are not changed. The -p rule also
rewrites harmless values such as -p5432. Host names, IP addresses and paths
are NOT redacted: read the pidstat log before sending a bundle outside your
organisation.

Root is needed only for the kernel log on hosts with kernel.dmesg_restrict=1
and for run directories created by other users. Without root those parts are
skipped with a NOTE:. A search directory that exists but cannot be read is
named in a NOTE: too.

Database collectors (linux/db/collect-*-metrics.sh) write their run
directories next to the OS runs, or under ~/perf-diag of the user who ran them,
so the default search covers both. When a database server is running on this
host and the selection holds no run of its collector, a NOTE: says so.

Examples:
  ${SCRIPT_NAME}
  ${SCRIPT_NAME} --since 6h -o /tmp
  ${SCRIPT_NAME} --run /var/log/perf-diag/db01_os_20261016_093000 --no-kernel-log
EOF
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
SINCE="7d"
SINCE_GIVEN="no"
RUN_ARGS=()
BASE_ARGS=()
OUT_DIR="."
FORMAT="tar.gz"
KERNEL_LOG="yes"
REDACT="yes"
INVENTORY="yes"
OPT_SHIFT=1

while [[ $# -gt 0 ]]; do
    arg="$1"
    has_next="no"
    if [[ $# -ge 2 ]]; then
        has_next="yes"
    fi
    case "$arg" in
        --since|--since=*)  take_value "$arg" "${2-}" "$has_next"; SINCE="$OPT_VALUE"; SINCE_GIVEN="yes" ;;
        --run|--run=*)      take_value "$arg" "${2-}" "$has_next"; RUN_ARGS+=("$OPT_VALUE") ;;
        --base|--base=*)    take_value "$arg" "${2-}" "$has_next"; BASE_ARGS+=("$OPT_VALUE") ;;
        -o|--outdir|--outdir=*) take_value "$arg" "${2-}" "$has_next"; OUT_DIR="$OPT_VALUE" ;;
        --format|--format=*) take_value "$arg" "${2-}" "$has_next"; FORMAT="$OPT_VALUE" ;;
        --no-kernel-log) KERNEL_LOG="no"; OPT_SHIFT=1 ;;
        --no-redact)     REDACT="no"; OPT_SHIFT=1 ;;
        --no-inventory)  INVENTORY="no"; OPT_SHIFT=1 ;;
        -h|--help)    usage; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        -d|--duration|-c|--count|-i|--interval)
            usage_error "'${arg}' does not apply: this script does not sample" ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi

case "$FORMAT" in
    tar.gz|zip) ;;
    *) usage_error "--format must be tar.gz or zip" ;;
esac

SINCE_SECS=$(parse_to_seconds "$SINCE" yes) || exit 2

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
install_hint() {
    local package="$1" sudo_prefix=""
    if (( EUID != 0 )); then
        sudo_prefix="sudo "
    fi
    if command -v dnf >/dev/null 2>&1; then
        echo "${sudo_prefix}dnf install -y ${package}"
    elif command -v yum >/dev/null 2>&1; then
        echo "${sudo_prefix}yum install -y ${package}"
    elif command -v tdnf >/dev/null 2>&1; then
        echo "${sudo_prefix}tdnf install -y ${package}"
    elif command -v apt-get >/dev/null 2>&1; then
        echo "${sudo_prefix}apt-get install -y ${package}"
    elif command -v zypper >/dev/null 2>&1; then
        echo "${sudo_prefix}zypper install -y ${package}"
    else
        echo "install the '${package}' package with your package manager"
    fi
}

command -v tar >/dev/null 2>&1 || die "tar is required"
if [[ "$FORMAT" == "zip" ]] && ! command -v zip >/dev/null 2>&1; then
    echo "ERROR: --format zip needs the zip command, which is not installed." >&2
    echo "  Install it with: $(install_hint zip)" >&2
    echo "  Or use the default --format tar.gz, which needs nothing extra." >&2
    exit 1
fi

IS_ROOT="no"
if (( EUID == 0 )); then
    IS_ROOT="yes"
else
    note "not root: the kernel log may be unreadable and other users' run directories may be skipped"
fi

# ---------------------------------------------------------------------------
# Search bases
# ---------------------------------------------------------------------------
# Database collectors are told not to run under sudo, so their runs usually sit
# in ~/perf-diag of whoever ran them, not in /var/log/perf-diag. The default
# search therefore covers the home directories that can hold runs. With --base,
# only the named directories are searched.
SEARCH_BASES=()

# Home directory of an account, empty when the account does not exist.
home_of() {
    local account="$1"
    if [[ -n "$account" ]]; then
        getent passwd "$account" 2>/dev/null | cut -d: -f6 || true
    fi
}

# Prints the default bases, one per line, in search order.
default_bases() {
    local account home
    echo "/var/log/perf-diag"
    if [[ -n "${HOME:-}" ]]; then
        echo "${HOME}/perf-diag"
    fi
    for account in "${SUDO_USER:-}" postgres mysql oracle mssql; do
        home="$(home_of "$account")"
        if [[ -n "$home" ]]; then
            echo "${home}/perf-diag"
        fi
    done
}

# Adds a directory to SEARCH_BASES unless it is the same directory as one
# already there (HOME and the SUDO_USER home are the same under plain sudo -E).
add_search_base() {
    local base="${1%/}" existing
    for existing in ${SEARCH_BASES[@]+"${SEARCH_BASES[@]}"}; do
        if [[ "$existing" -ef "$base" ]]; then
            return 0
        fi
    done
    SEARCH_BASES+=("$base")
}

# Fills SEARCH_BASES with the directories that exist and can be read. A base
# that does not exist is skipped silently. One that exists but cannot be read
# is named in a NOTE:, and so is one whose home directory cannot be entered,
# because it may well exist behind it.
# $1 = "note" to report problems, "quiet" to stay silent (--run is in use and
# the bases are only needed to look for a hardware inventory).
resolve_search_bases() {
    local mode="$1" base parent
    local candidates=()
    if (( ${#BASE_ARGS[@]} > 0 )); then
        for base in "${BASE_ARGS[@]}"; do
            if [[ ! -d "$base" && "$mode" == "note" ]]; then
                die "base directory not found: ${base}"
            fi
            candidates+=("$base")
        done
    else
        while IFS= read -r base; do
            candidates+=("$base")
        done < <(default_bases)
    fi

    for base in "${candidates[@]}"; do
        parent="$(dirname "$base")"
        if [[ -d "$base" ]]; then
            if [[ -r "$base" && -x "$base" ]]; then
                add_search_base "$base"
            elif [[ "$mode" == "note" ]]; then
                note "cannot read ${base} (run with sudo to include it)"
            fi
        elif [[ -d "$parent" && ! -x "$parent" && "$mode" == "note" ]]; then
            note "cannot read ${base} (run with sudo to include it)"
        fi
    done
}

# ---------------------------------------------------------------------------
# Run-directory selection
# ---------------------------------------------------------------------------
# Name format: <host>_<tag>_<YYYYmmdd_HHMMSS>. The timestamp in the name is used,
# not mtime: copying files changes mtime, the name does not. A lexical
# comparison of YYYYmmddHHMMSS strings is chronological.
RUN_NAME_PATTERN='^(.+)_([A-Za-z0-9]+)_([0-9]{8})_([0-9]{6})$'

# Prints "tag stamp" for a run directory name, or returns 1 if it is not one.
run_tag_and_stamp() {
    local name="${1##*/}"
    if [[ "$name" =~ $RUN_NAME_PATTERN ]]; then
        echo "${BASH_REMATCH[2]} ${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
        return 0
    fi
    return 1
}

# Epoch seconds for a YYYYmmddHHMMSS stamp in local time.
epoch_from_stamp() {
    local stamp="$1"
    date -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:8:2}:${stamp:10:2}:${stamp:12:2}" +%s
}

select_run_dirs() {
    local base="$1" since_secs="$2" cutoff dir info tag stamp
    cutoff="$(date -d "@$(( $(date +%s) - since_secs ))" +%Y%m%d%H%M%S)"
    for dir in "$base"/*_*_[0-9]*_[0-9]*; do
        [[ -d "$dir" ]] || continue
        info="$(run_tag_and_stamp "$dir")" || continue
        tag="${info%% *}"
        stamp="${info##* }"
        # Never include earlier bundles' staging or output.
        [[ "$tag" == "gather" ]] && continue
        if [[ "$stamp" > "$cutoff" || "$stamp" == "$cutoff" ]]; then
            echo "$dir"
        fi
    done
}

SELECTED=()
if (( ${#RUN_ARGS[@]} > 0 )); then
    resolve_search_bases quiet
    if [[ "$SINCE_GIVEN" == "yes" ]]; then
        note "--since ignored because --run was given"
    fi
    for run_arg in "${RUN_ARGS[@]}"; do
        [[ -d "$run_arg" ]] || die "run directory not found: ${run_arg}"
        run_arg="${run_arg%/}"
        run_tag_and_stamp "$run_arg" >/dev/null || die "not a run directory name (<host>_<tag>_<YYYYmmdd_HHMMSS>): ${run_arg}"
        duplicate="no"
        for existing in ${SELECTED[@]+"${SELECTED[@]}"}; do
            if [[ "$existing" == "$run_arg" ]]; then
                duplicate="yes"
            fi
        done
        if [[ "$duplicate" == "no" ]]; then
            SELECTED+=("$run_arg")
        fi
    done
else
    resolve_search_bases note
    if (( ${#SEARCH_BASES[@]} == 0 )); then
        die "no perf-diag directory found in the default locations. Use --base to name one"
    fi
    for search_base in "${SEARCH_BASES[@]}"; do
        while IFS= read -r selected_dir; do
            [[ -n "$selected_dir" ]] && SELECTED+=("$selected_dir")
        done < <(select_run_dirs "$search_base" "$SINCE_SECS")
    done
fi

if (( ${#SELECTED[@]} == 0 )); then
    die "no run directories found in ${SEARCH_BASES[*]} within ${SINCE}. Use --since for a longer period or --run to name a directory"
fi

HAS_RUN="no"
for selected_dir in "${SELECTED[@]}"; do
    selected_info="$(run_tag_and_stamp "$selected_dir")"
    if [[ "${selected_info%% *}" != "hw" ]]; then
        HAS_RUN="yes"
    fi
done
if [[ "$HAS_RUN" == "no" ]]; then
    die "only hardware inventories were selected; there is no run data to bundle"
fi

# ---------------------------------------------------------------------------
# Database detection
# ---------------------------------------------------------------------------
# Test hook: PERF_DIAG_PS_FILE names a file of canned ps output in the format
# below, so tests need no database process. Not a user option.
db_process_list() {
    if [[ -n "${PERF_DIAG_PS_FILE:-}" ]]; then
        cat "$PERF_DIAG_PS_FILE"
    else
        ps -ww -eo pid=,ppid=,user=,args= 2>/dev/null || true
    fi
}

db_display_name() {
    case "$1" in
        postgresql) echo "PostgreSQL" ;;
        mysql)      echo "MySQL" ;;
        oracle)     echo "Oracle" ;;
        mssql)      echo "SQL Server" ;;
    esac
}

# True when a run directory with this tag (postgresql, mysql, ...) is selected.
selection_has_tag() {
    local tag="$1" dir info
    for dir in "${SELECTED[@]}"; do
        info="$(run_tag_and_stamp "$dir")" || continue
        if [[ "${info%% *}" == "$tag" ]]; then
            return 0
        fi
    done
    return 1
}

# Sets DB_DETECTED (comma separated, empty when none). An engine that is running
# but has no run of its collector in the selection gets a NOTE:.
DB_DETECTED=""
detect_databases() {
    local engine engines=()
    DB_DETECTED="$(db_process_list | db_scan | db_engines_detected)"
    if [[ -n "$DB_DETECTED" ]]; then
        IFS=, read -r -a engines <<< "$DB_DETECTED"
    fi
    for engine in ${engines[@]+"${engines[@]}"}; do
        if ! selection_has_tag "$engine"; then
            note "$(db_display_name "$engine") is running on this host but the bundle has no ${engine} run; perfdiag will use process data only. Run linux/db/collect-${engine}-metrics.sh alongside collect-os-metrics.sh next time."
        fi
    done
}
detect_databases

[[ -d "$OUT_DIR" ]] || die "output directory not found: ${OUT_DIR}"
[[ -w "$OUT_DIR" ]] || die "output directory not writable: ${OUT_DIR}"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------
# Staged next to the output, not in /tmp: bundles can be hundreds of megabytes
# and /tmp is often a small tmpfs.
STAGE="$(mktemp -d "${OUT_DIR}/.gather-perf-diag.XXXXXX")"
cleanup() {
    rm -rf "$STAGE"
    if [[ -n "${PART_FILE:-}" ]]; then
        rm -f "$PART_FILE"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

TS="$(date +%Y%m%d_%H%M%S)"
HOST_SHORT="$(short_hostname)"
BUNDLE_NAME="${HOST_SHORT}_gather_${TS}"
BUNDLE_DIR="${STAGE}/${BUNDLE_NAME}"
mkdir -p "$BUNDLE_DIR"

# ---------------------------------------------------------------------------
# Hardware inventory (soft dependency on a sibling script)
# ---------------------------------------------------------------------------
oldest_stamp() {
    local dir info tag stamp oldest=""
    for dir in "$@"; do
        info="$(run_tag_and_stamp "$dir")" || continue
        tag="${info%% *}"
        stamp="${info##* }"
        [[ "$tag" == "hw" ]] && continue
        if [[ -z "$oldest" || "$stamp" < "$oldest" ]]; then
            oldest="$stamp"
        fi
    done
    echo "$oldest"
}

# Newest hw directory among the arguments, "" when none.
newest_hw_dir() {
    local dir info tag stamp newest="" newest_stamp=""
    for dir in "$@"; do
        info="$(run_tag_and_stamp "$dir")" || continue
        tag="${info%% *}"
        stamp="${info##* }"
        [[ "$tag" == "hw" ]] || continue
        if [[ -z "$newest" || "$stamp" > "$newest_stamp" ]]; then
            newest="$dir"
            newest_stamp="$stamp"
        fi
    done
    echo "$newest"
}

find_inventory_script() {
    if [[ -x "${SCRIPT_DIR}/collect-hw-inventory.sh" ]]; then
        echo "${SCRIPT_DIR}/collect-hw-inventory.sh"
    elif command -v collect-hw-inventory.sh >/dev/null 2>&1; then
        command -v collect-hw-inventory.sh
    fi
}

INVENTORY_DIR=""          # hw directory to copy into the bundle
INVENTORY_GENERATED=""    # hw directory created in the staging area

resolve_inventory() {
    local oldest candidate info stamp inventory_script inventory_err line search_base
    oldest="$(oldest_stamp "${SELECTED[@]}")"
    candidate="$(newest_hw_dir "${SELECTED[@]}")"
    if [[ -z "$candidate" ]] && (( ${#RUN_ARGS[@]} > 0 )); then
        # --run named only some directories: look for an inventory in the bases.
        local base_hw=()
        local entry
        for search_base in ${SEARCH_BASES[@]+"${SEARCH_BASES[@]}"}; do
            for entry in "$search_base"/*_hw_[0-9]*_[0-9]*; do
                [[ -d "$entry" ]] && base_hw+=("$entry")
            done
        done
        if (( ${#base_hw[@]} > 0 )); then
            candidate="$(newest_hw_dir "${base_hw[@]}")"
        fi
    fi

    if [[ -n "$candidate" ]]; then
        info="$(run_tag_and_stamp "$candidate")"
        stamp="${info##* }"
        if [[ -z "$oldest" || "$stamp" > "$oldest" || "$stamp" == "$oldest" ]]; then
            INVENTORY_DIR="$candidate"
            return 0
        fi
    fi

    inventory_script="$(find_inventory_script)"
    if [[ -n "$inventory_script" ]]; then
        echo "Running ${inventory_script} for a current hardware inventory..."
        inventory_err="$(mktemp "${STAGE}/hw-err.XXXXXX")"
        if "$inventory_script" -o "$STAGE" > /dev/null 2> "$inventory_err"; then
            INVENTORY_GENERATED="$(newest_hw_dir "$STAGE"/*_hw_[0-9]*_[0-9]*)"
            while IFS= read -r line; do
                case "$line" in
                    "NOTE: "*) note "inventory: ${line#NOTE: }" ;;
                esac
            done < "$inventory_err"
        else
            warn "collect-hw-inventory.sh failed; continuing without a fresh inventory"
        fi
        rm -f "$inventory_err"
        if [[ -n "$INVENTORY_GENERATED" ]]; then
            return 0
        fi
    fi

    if [[ -n "$candidate" ]]; then
        INVENTORY_DIR="$candidate"
        note "hardware inventory ${candidate##*/} is older than the oldest run; run collect-hw-inventory.sh for a current one"
        return 0
    fi

    warn "no hardware inventory; disk thresholds will use defaults. Run collect-hw-inventory.sh first or pass --no-inventory"
}

if [[ "$INVENTORY" == "yes" ]]; then
    resolve_inventory
fi

# ---------------------------------------------------------------------------
# sadf export
# ---------------------------------------------------------------------------
# file name | sadf activity arguments. Alternatives are separated by "||" and
# tried in order; the first that exits 0 wins. sysstat 10.x (RHEL 7, Ubuntu 14.04)
# takes -r without ALL and treats ALL as a second data file name.
SADF_EXPORTS=(
    "cpu|-u ALL -P ALL"
    "load|-q"
    "psi_cpu|-q CPU"
    "psi_io|-q IO"
    "psi_mem|-q MEM"
    "cswch|-w"
    "memory|-r ALL||-r"
    "paging|-B"
    "swap_space|-S"
    "swap_rate|-W"
    "disk|-d -p"
    "filesystem|-F"
    "net_dev|-n DEV"
    "net_edev|-n EDEV"
    "net_tcp|-n TCP"
    "net_etcp|-n ETCP"
    "net_sock|-n SOCK"
    "net_soft|-n SOFT"
    "interrupts|-I ALL"
    "fchost|-n FC"
)

SADF_AVAILABLE="yes"
if ! command -v sadf >/dev/null 2>&1; then
    SADF_AVAILABLE="no"
fi

# A non-zero sadf exit means the activity was not collected or this sadf does not
# know the keyword. When every alternative fails the file is skipped with a note.
# A CSV with no data rows is removed too, so the bundle holds no empty tables.
export_sa_file() {
    local sa_file="$1" out_dir="$2" entry name choices first_args args succeeded
    local exported=0
    mkdir -p "$out_dir"
    for entry in "${SADF_EXPORTS[@]}"; do
        name="${entry%%|*}"
        choices="${entry#*|}"
        first_args="${choices%%||*}"
        succeeded="no"
        while [[ -n "$choices" ]]; do
            args="${choices%%||*}"
            if [[ "$choices" == *"||"* ]]; then
                choices="${choices#*||}"
            else
                choices=""
            fi
            # args is a fixed word list from SADF_EXPORTS, so it is split on purpose.
            # shellcheck disable=SC2086
            if LC_ALL=C sadf -d -U "$sa_file" -- $args > "${out_dir}/${name}.csv" 2> "${out_dir}/${name}.err"; then
                succeeded="yes"
                if [[ "$args" != "$first_args" ]]; then
                    note "${sa_file##*/}: ${name} exported with '${args}' (this sadf does not accept '${first_args}')"
                fi
                break
            fi
        done
        if [[ "$succeeded" == "yes" ]]; then
            if grep -qv '^#' "${out_dir}/${name}.csv"; then
                exported=$(( exported + 1 ))
            else
                rm -f "${out_dir}/${name}.csv"
                note "${sa_file##*/}: ${name} (${args}) had no data rows; skipped"
            fi
            rm -f "${out_dir}/${name}.err"
        else
            note "${sa_file##*/}: ${name} (${first_args}) not in file or not supported: $(head -n 1 "${out_dir}/${name}.err")"
            rm -f "${out_dir}/${name}.csv" "${out_dir}/${name}.err"
        fi
    done
    if (( exported == 0 )); then
        warn "${sa_file##*/}: sadf exported nothing. Is this sadf older than the sadc that wrote the file? ($(sadf -V 2>&1 | head -n 1))"
        rmdir "$out_dir" 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# pidstat redaction
# ---------------------------------------------------------------------------
# Applied to the copy, never the original. GNU sed (the I flag).
redact_pidstat() {
    local source="$1" destination="$2"
    sed -E \
        -e 's/(--?(password|passwd|pwd|secret|token|api[-_]?key))(=| +)[^ ]+/\1\3<redacted>/Ig' \
        -e 's/(^| )-p[^ ]+/\1-p<redacted>/g' \
        -e 's/([A-Z_]*(PASSWORD|SECRET|TOKEN)[A-Z_]*=)[^ ]+/\1<redacted>/g' \
        "$source" > "$destination"
}

# ---------------------------------------------------------------------------
# Per run directory
# ---------------------------------------------------------------------------
RUN_NAMES=()
RUN_BYTES=()

dir_bytes() {
    local bytes
    if bytes="$(du -sb "$1" 2>/dev/null | cut -f1)" && [[ "$bytes" =~ ^[0-9]+$ ]]; then
        echo "$bytes"
    else
        echo $(( $(du -sk "$1" | cut -f1) * 1024 ))
    fi
}

stage_run_dir() {
    local source="$1" name destination sa_files=() sa_file pidstat_file copied_pidstat
    name="${source##*/}"
    destination="${BUNDLE_DIR}/${name}"

    echo "Adding ${name}"
    if ! cp -R "$source" "$destination" 2>/dev/null; then
        note "${name}: some files could not be read and were skipped (try running as root)"
        if [[ ! -d "$destination" ]]; then
            return 0
        fi
    fi
    RUN_NAMES+=("$name")
    RUN_BYTES+=("$(dir_bytes "$source")")

    # Detached-run bookkeeping is of no use off the host.
    rm -f "${destination}"/*.pid

    for sa_file in "$source"/sa_[0-9]*; do
        [[ -f "$sa_file" ]] || continue
        sa_files+=("$sa_file")
    done
    if (( ${#sa_files[@]} > 0 )); then
        if [[ "$SADF_AVAILABLE" == "yes" ]]; then
            if (( ${#sa_files[@]} > 1 )); then
                note "${name}: ${#sa_files[@]} sa files; exporting ${sa_files[0]##*/} only"
            fi
            export_sa_file "${sa_files[0]}" "${destination}/sadf"
        else
            note "${name}: sadf is not installed; the raw sa file is included but there are no CSV exports. ($(install_hint sysstat))"
        fi
    fi

    for pidstat_file in "$source"/pidstat_*.log; do
        [[ -f "$pidstat_file" ]] || continue
        copied_pidstat="${destination}/${pidstat_file##*/}"
        if [[ "$REDACT" == "yes" ]]; then
            if ! redact_pidstat "$pidstat_file" "$copied_pidstat"; then
                rm -f "$copied_pidstat"
                note "${name}: ${pidstat_file##*/} left out because redaction failed (GNU sed is required)"
            fi
        fi
    done
}

# Inventories are not copied as runs: only the newest one is bundled, chosen above.
for run_dir in "${SELECTED[@]}"; do
    run_info="$(run_tag_and_stamp "$run_dir")"
    if [[ "${run_info%% *}" == "hw" ]]; then
        continue
    fi
    stage_run_dir "$run_dir"
done

if [[ -n "$INVENTORY_DIR" ]]; then
    echo "Adding ${INVENTORY_DIR##*/}"
    cp -R "$INVENTORY_DIR" "${BUNDLE_DIR}/" 2>/dev/null || note "hardware inventory ${INVENTORY_DIR##*/} could not be copied"
    RUN_NAMES+=("${INVENTORY_DIR##*/}")
    RUN_BYTES+=("$(dir_bytes "$INVENTORY_DIR")")
elif [[ -n "$INVENTORY_GENERATED" ]]; then
    mv "$INVENTORY_GENERATED" "${BUNDLE_DIR}/"
    RUN_NAMES+=("${INVENTORY_GENERATED##*/}")
    RUN_BYTES+=("$(dir_bytes "${BUNDLE_DIR}/${INVENTORY_GENERATED##*/}")")
fi

# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
manifest_value() {
    local file="$1" key="$2"
    if [[ -r "$file" ]]; then
        grep -m1 "^${key}=" "$file" | cut -d= -f2- || true
    fi
}

# Earliest start and latest start + duration (or end=) across the run manifests.
# The hardware inventory is a point in time and does not widen the window.
WINDOW_START=""
WINDOW_END=""
compute_window() {
    local dir info tag stamp manifest start_iso start_epoch duration end_iso end_epoch
    for dir in "${SELECTED[@]}"; do
        info="$(run_tag_and_stamp "$dir")" || continue
        tag="${info%% *}"
        stamp="${info##* }"
        [[ "$tag" == "hw" ]] && continue
        manifest="${dir}/manifest.txt"
        start_iso="$(manifest_value "$manifest" start)"
        start_epoch=""
        if [[ -n "$start_iso" ]]; then
            start_epoch="$(date -d "$start_iso" +%s 2>/dev/null || true)"
        fi
        if [[ -z "$start_epoch" ]]; then
            start_epoch="$(epoch_from_stamp "$stamp")"
        fi
        end_epoch=""
        end_iso="$(manifest_value "$manifest" end)"
        if [[ -n "$end_iso" ]]; then
            end_epoch="$(date -d "$end_iso" +%s 2>/dev/null || true)"
        fi
        if [[ -z "$end_epoch" ]]; then
            duration="$(manifest_value "$manifest" duration_s)"
            if [[ "$duration" =~ ^[0-9]+$ ]]; then
                end_epoch=$(( start_epoch + duration ))
            else
                end_epoch="$(date +%s)"
            fi
        fi
        if [[ -z "$WINDOW_START" ]] || (( start_epoch < WINDOW_START )); then
            WINDOW_START="$start_epoch"
        fi
        if [[ -z "$WINDOW_END" ]] || (( end_epoch > WINDOW_END )); then
            WINDOW_END="$end_epoch"
        fi
    done
}
compute_window

# ---------------------------------------------------------------------------
# Kernel log excerpt
# ---------------------------------------------------------------------------
KERNEL_LOG_SOURCE="none"
KERNEL_EVENT_PATTERN='out of memory|oom-kill|killed process|hung_task|blocked for more than|soft lockup|hard lockup|'
KERNEL_EVENT_PATTERN+='i/o error|blk_update_request|buffer i/o error|nvme.*(timeout|reset)|scsi.*(timeout|abort|reset)|'
KERNEL_EVENT_PATTERN+='ext4-fs (error|warning)|xfs.*(error|corruption)|md/raid.*(fail|degraded)|multipath.*(fail|reinstated)|'
KERNEL_EVENT_PATTERN+='task abort|link (down|is not ready)|nf_conntrack: table full|page allocation failure|cpu clock throttled'
# Plain-file sources are not filtered by time (the formats differ and mawk has
# no mktime). The newest matches are kept and the analyser applies the window.
KERNEL_LOG_MAX_LINES=2000

# Prints the matching lines of stdin (case-insensitive), nothing else.
filter_kernel_events() {
    grep -iE "$KERNEL_EVENT_PATTERN" || true
}

collect_kernel_log() {
    local out="${BUNDLE_DIR}/kernel_events.log" body since_text until_text uptime_secs source=""
    local padded_start padded_end

    if [[ -z "$WINDOW_START" ]]; then
        note "no run window; kernel log skipped"
        return 0
    fi
    padded_start=$(( WINDOW_START - 300 ))
    padded_end=$(( WINDOW_END + 300 ))
    # Formatted for the tools instead of passing @epoch: journalctl does not
    # accept it everywhere.
    since_text="$(date -d "@${padded_start}" '+%Y-%m-%d %H:%M:%S')"
    until_text="$(date -d "@${padded_end}" '+%Y-%m-%d %H:%M:%S')"
    body="$(mktemp "${STAGE}/kernel.XXXXXX")"

    if [[ -d /run/systemd/system ]] && command -v journalctl >/dev/null 2>&1; then
        # pipefail makes a journalctl failure (no permission, no journal) fail the
        # pipeline, so the next source is tried.
        if journalctl -k -o short-iso --no-pager --since "$since_text" --until "$until_text" 2>/dev/null \
                | filter_kernel_events > "$body"; then
            source="journalctl"
        fi
    fi

    if [[ -z "$source" && -r /var/log/kern.log ]]; then
        filter_kernel_events < /var/log/kern.log | tail -n "$KERNEL_LOG_MAX_LINES" > "$body"
        source="kern.log"
        note "kernel log from kern.log is not filtered by time; the newest ${KERNEL_LOG_MAX_LINES} matching lines are kept"
    fi

    if [[ -z "$source" && -r /var/log/messages ]]; then
        filter_kernel_events < /var/log/messages | grep -i 'kernel' | tail -n "$KERNEL_LOG_MAX_LINES" > "$body" || true
        source="messages"
        note "kernel log from /var/log/messages is not filtered by time; the newest ${KERNEL_LOG_MAX_LINES} matching lines are kept"
    fi

    if [[ -z "$source" ]] && command -v dmesg >/dev/null 2>&1; then
        if dmesg 2>/dev/null | filter_kernel_events > "$body"; then
            source="dmesg"
            uptime_secs="$(awk '{print int($1)}' /proc/uptime)"
            note "kernel log from dmesg: timestamps are seconds since boot, not wall-clock"
        fi
    fi

    if [[ -z "$source" ]]; then
        rm -f "$body"
        note "no readable kernel log (journalctl, kern.log, messages and dmesg all failed). Run as root, or pass --no-kernel-log"
        return 0
    fi

    {
        echo "# source=${source}"
        echo "# window_start=${since_text} window_end=${until_text}"
        if [[ "$source" == "dmesg" ]]; then
            echo "# boot_epoch=$(( $(date +%s) - uptime_secs ))"
        fi
        cat "$body"
    } > "$out"
    rm -f "$body"
    KERNEL_LOG_SOURCE="$source"
}

if [[ "$KERNEL_LOG" == "yes" ]]; then
    collect_kernel_log
fi

# ---------------------------------------------------------------------------
# gather_manifest.txt
# ---------------------------------------------------------------------------
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        return 1
    fi
}

write_gather_manifest() {
    local manifest="${BUNDLE_DIR}/gather_manifest.txt" index file relative hash

    MANIFEST_PATH=""
    manifest_start "$manifest" 0 1 0
    # With --run nothing was searched for runs, so no bases are recorded.
    if (( ${#RUN_ARGS[@]} == 0 )); then
        for (( index = 0; index < ${#SEARCH_BASES[@]}; index++ )); do
            manifest_add "base.$(( index + 1 ))" "${SEARCH_BASES[$index]}"
        done
    fi
    index=0
    for index in "${!RUN_NAMES[@]}"; do
        manifest_add "run.$(( index + 1 ))" "${RUN_NAMES[$index]}"
        manifest_add "run.$(( index + 1 )).bytes" "${RUN_BYTES[$index]}"
    done
    if [[ -n "$WINDOW_START" ]]; then
        manifest_add window_start "$(date -d "@${WINDOW_START}" -Iseconds)"
        manifest_add window_end "$(date -d "@${WINDOW_END}" -Iseconds)"
    fi
    manifest_add kernel_log_source "$KERNEL_LOG_SOURCE"
    manifest_add redacted "$REDACT"
    if [[ -n "$DB_DETECTED" ]]; then
        manifest_add db_detected "$DB_DETECTED"
    fi

    if sha256_of /dev/null >/dev/null 2>&1; then
        while IFS= read -r -d '' file; do
            relative="${file#"${BUNDLE_DIR}"/}"
            if [[ "$relative" == "gather_manifest.txt" ]]; then
                continue
            fi
            hash="$(sha256_of "$file")"
            manifest_add "sha256.${relative}" "$hash"
        done < <(find "$BUNDLE_DIR" -type f -print0 | sort -z)
    else
        note "no sha256sum or shasum; file checksums not recorded"
    fi
    manifest_add notes "$NOTES_TEXT"
    manifest_finish
}
write_gather_manifest

# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------
PART_FILE=""
BUNDLE_PATH="${OUT_DIR}/${BUNDLE_NAME}.${FORMAT}"
PART_FILE="${BUNDLE_PATH}.part"

create_bundle() {
    local owner_flags=()
    if [[ "$FORMAT" == "zip" ]]; then
        ( cd "$STAGE" && zip -qr "$PART_FILE" "$BUNDLE_NAME" )
    else
        # GNU tar can write root ownership instead of the local user names.
        if tar --owner=0 --group=0 --numeric-owner -cf /dev/null /dev/null 2>/dev/null; then
            owner_flags=(--owner=0 --group=0 --numeric-owner)
        fi
        tar -C "$STAGE" ${owner_flags[@]+"${owner_flags[@]}"} -czf "$PART_FILE" "$BUNDLE_NAME"
    fi
    mv "$PART_FILE" "$BUNDLE_PATH"
    PART_FILE=""
}
create_bundle

if [[ "$IS_ROOT" == "yes" && -n "${SUDO_UID:-}" ]]; then
    chown "${SUDO_UID}:${SUDO_GID:-$SUDO_UID}" "$BUNDLE_PATH" 2>/dev/null || true
fi

echo ""
echo "Bundle: ${BUNDLE_PATH}"
echo "Size:   $(wc -c < "$BUNDLE_PATH" | tr -d ' ') bytes"
echo "Runs:   ${#RUN_NAMES[@]}"
if [[ "$REDACT" == "no" ]]; then
    echo "WARNING: pidstat logs were not redacted (--no-redact)" >&2
fi
