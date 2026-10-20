#!/usr/bin/env bash
#
# psar.sh — sar-like sampler with no dependency on sysstat.
#
# Reads /proc/stat, /proc/meminfo, /proc/vmstat, /proc/diskstats, /proc/net/dev
# and /proc/loadavg directly. Each sample is the difference between two
# snapshots divided by the real elapsed time (taken from /proc/uptime), which is
# how sar and iostat derive their rates.
#
# Each active section is computed by a single awk process per sample.
#
# Portability: bash 4.1+, POSIX awk (mawk compatible), no bc, no python.
#
# Run with -h for usage.

set -euo pipefail

SCRIPT_NAME="${0##*/}"
SCRIPT_VERSION="2.0.0"
PROG="$SCRIPT_NAME"
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

INTERVAL=2
COUNT=""
DURATION=""
DISK_LIST=""
IFACE_LIST=""
OUTDIR_BASE=""
OPT_SHIFT=1
SHOW_CPU=0
SHOW_MEM=0
SHOW_DISK=0
SHOW_NET=0
SHOW_LOAD=0
SHOW_ALL=0
NO_REPEAT_HEADERS=0

# Width of the label column for sections that have a single row per sample
# (CPU, MEM, LOAD). DEV and IFACE columns size themselves to the longest name.
LABEL_WIDTH=6
MIN_NAME_WIDTH=6

print_help() {
    cat <<EOF
$PROG — sar-like sampler with no dependency on sysstat.

Usage: $PROG [-i interval] [-d duration | -c count] [--header-once]
       $(printf '%*s' "${#PROG}" '') [--dev disk[,disk...]] [--iface iface[,iface...]]
       $(printf '%*s' "${#PROG}" '') [-C] [-M] [-D] [-N] [-L] [-A] [-o dir] [-h] [-V]

  -i, --interval     sample interval: 2, 2s, 1m. A bare number is seconds,
                     1 or more (default 2)
  -d, --duration     total run time: 30, 30s, 15m, 2h, 1d. A bare number is
                     seconds. Mutually exclusive with -c
  -c, --count        number of samples, 0 = until stopped (default 0)
      --header-once  print each section header once (vmstat-style). Ignored
                     with a warning when more than one section is active
      --dev LIST     disks to report, by kernel name, comma separated: vda,
                     sda, nvme0n1, dm-0. Implies -D. Default: every whole
                     disk (no partitions, loop, ram, zram, sr)
      --iface LIST   network interfaces to report, comma separated. Implies -N.
                     Default: every interface except lo, veth*, docker*,
                     virbr*, cni*, br-*. "all" selects every interface except
                     lo. Name lo explicitly to include it
  -C                 CPU
  -M                 memory
  -D                 disk
  -N                 network
  -L                 load average
  -A                 all sections (overrides individual section flags)
  -o, --outdir DIR   also write CSV files and manifest.txt to
                     DIR/<host>_psar_<timestamp>/ (cpu.csv, mem.csv, disk.csv,
                     net.csv and load.csv, active sections only)
  -h, --help         show this help and exit
  -V, --version      show the script name and version and exit

With no section flag, all sections are shown. Section flags are additive.
"-A --dev vda" shows every section, with the disk section limited to vda.

Columns:
  CPU    %usr (user+nice)  %sys  %irq  %soft  %iowait  %steal  %idle
         run/blkd are instantaneous runnable/blocked task counts (vmstat r/b)
         cs/s is context switches per second
  MEM    usedkB = MemTotal - MemAvailable. dirtykB/wbackkB are Dirty/Writeback
         swpin/s, swpout/s are pages per second. majflt/s is major faults/s
  DEV    r/s w/s: completed IOs/s.  rkB/s wkB/s: throughput
         rareq-sz wareq-sz: average request size, kB
         r_await w_await: average latency including queue time, ms
         aqu-sz: average queue depth.  %util: time with at least one IO in flight
         d/s dkB/s: discards (kernel 4.18+).  f/s: flushes (kernel 5.5+)
  IFACE  rx/tx kB/s, packets/s, errors/s, drops/s
  LOAD   1, 5 and 15 minute load averages

CSV files (-o): console output is unchanged. Each run writes to its own
directory. Timestamps are ISO 8601 (date -Iseconds). Disk and network files
have one row per device per sample, with a "device" or "iface" column after
ts. Column names follow the console headers with "/" replaced by "_" and "%"
replaced by an "_pct" suffix (r/s -> r_s, %util -> util_pct). manifest.txt
records the host, kernel, command line and start and end times.

Reading %util: it only indicates saturation on single-queue devices such as
spinning disks. NVMe, SSD arrays and cloud block storage service many IOs in
parallel and can be far from saturated at 100%. For those, use aqu-sz and the
await columns.

Stacked devices are counted at each layer. A write to an LVM volume on vda3
appears on both the mapper device and on vda, as in sar -d and iostat.

Examples:
  $PROG                          # all sections every 2s, forever
  $PROG -D -c 5                  # all whole disks, 5 samples
  $PROG --dev vda,vdb -i 1       # only vda and vdb, every second
  $PROG -A --dev vda -d 30s      # every section for 30 seconds, disk limited to vda
  $PROG --iface eth0 --header-once   # eth0 only, header printed once
  $PROG -A -o ./metrics -d 10m   # all sections for 10 minutes, also write CSV files
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    arg="$1"
    has_next="no"
    if [[ $# -ge 2 ]]; then
        has_next="yes"
    fi
    case "$arg" in
        -i|--interval|--interval=*) take_value "$arg" "${2-}" "$has_next"; INTERVAL="$OPT_VALUE" ;;
        -d|--duration|--duration=*) take_value "$arg" "${2-}" "$has_next"; DURATION="$OPT_VALUE" ;;
        -c|--count|--count=*)       take_value "$arg" "${2-}" "$has_next"; COUNT="$OPT_VALUE" ;;
        -o|--outdir|--outdir=*)     take_value "$arg" "${2-}" "$has_next"; OUTDIR_BASE="$OPT_VALUE" ;;
        --dev|--dev=*)              take_value "$arg" "${2-}" "$has_next"; DISK_LIST="$OPT_VALUE"; SHOW_DISK=1 ;;
        --iface|--iface=*)          take_value "$arg" "${2-}" "$has_next"; IFACE_LIST="$OPT_VALUE"; SHOW_NET=1 ;;
        --header-once) NO_REPEAT_HEADERS=1; OPT_SHIFT=1 ;;
        -C) SHOW_CPU=1; OPT_SHIFT=1 ;;
        -M) SHOW_MEM=1; OPT_SHIFT=1 ;;
        -D) SHOW_DISK=1; OPT_SHIFT=1 ;;
        -N) SHOW_NET=1; OPT_SHIFT=1 ;;
        -L) SHOW_LOAD=1; OPT_SHIFT=1 ;;
        -A) SHOW_ALL=1; OPT_SHIFT=1 ;;
        -h|--help)    print_help; exit 0 ;;
        -V|--version) echo "${SCRIPT_NAME} ${SCRIPT_VERSION}"; exit 0 ;;
        # Options from earlier versions. Say what replaced them instead of
        # reporting an unknown argument.
        -e) usage_error "-e was removed; interface lists now use --iface" ;;
        -w) usage_error "-w was removed; CSV output now uses -o <dir>" ;;
        -n) usage_error "-n was removed; use --header-once" ;;
        --)           shift; break ;;
        *)            usage_error "unknown argument '${arg}'" ;;
    esac
    shift "$OPT_SHIFT"
done

if [[ $# -gt 0 ]]; then
    usage_error "unexpected argument '$1'"
fi

if [[ -n "$DURATION" && -n "$COUNT" ]]; then
    usage_error "-d/--duration and -c/--count are mutually exclusive"
fi
if [[ -n "$COUNT" && ! "$COUNT" =~ ^[0-9]+$ ]]; then
    usage_error "-c/--count must be a whole number, 0 or more"
fi

INTERVAL_SECS=$(parse_to_seconds "$INTERVAL" no) || exit 2
if (( INTERVAL_SECS < 1 )); then
    usage_error "interval must be at least 1 second"
fi

# COUNT 0 means until stopped. -d turns a duration into a sample count.
if [[ -n "$DURATION" ]]; then
    if ! DURATION_SECS=$(parse_to_seconds "$DURATION" yes); then
        usage_error "-d now sets the run duration; disk lists now use --dev"
    fi
    if (( DURATION_SECS < INTERVAL_SECS )); then
        usage_error "duration must be >= interval"
    fi
    COUNT=$(( DURATION_SECS / INTERVAL_SECS ))
elif [[ -z "$COUNT" ]]; then
    COUNT=0
fi
# Used by the sample loop and the manifest.
INTERVAL="$INTERVAL_SECS"

# -A, or no section selected at all, means every section.
if [[ $SHOW_ALL -eq 1 ]] \
    || [[ $(( SHOW_CPU + SHOW_MEM + SHOW_DISK + SHOW_NET + SHOW_LOAD )) -eq 0 ]]; then
    SHOW_CPU=1; SHOW_MEM=1; SHOW_DISK=1; SHOW_NET=1; SHOW_LOAD=1
fi

ACTIVE_SECTIONS=$(( SHOW_CPU + SHOW_MEM + SHOW_DISK + SHOW_NET + SHOW_LOAD ))

if [[ $NO_REPEAT_HEADERS -eq 1 && $ACTIVE_SECTIONS -gt 1 ]]; then
    warn "--header-once ignored: headers are needed to tell $ACTIVE_SECTIONS interleaved sections apart"
    NO_REPEAT_HEADERS=0
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# True when the space-separated list in $2 already holds the word $1.
list_has() {
    case " $2 " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

# Centiseconds since boot. /proc/uptime always has two decimal places, so
# removing the point gives an integer: "12345.67" -> "1234567".
read_uptime_cs() {
    local up
    read -r up _ < /proc/uptime
    echo "${up/./}"
}

# ---------------------------------------------------------------------------
# Disk enumeration
# ---------------------------------------------------------------------------

# Whole-disk devices only. /sys/block never lists partitions, so no
# "ends in a digit" heuristic is needed (that heuristic breaks nvme0n1,
# mmcblk0, md0 and dm-0).
list_disks() {
    local dev name
    for dev in /sys/block/*; do
        [[ -e "$dev" ]] || continue
        name="${dev##*/}"
        case "$name" in
            loop*|ram*|zram*|sr*|fd*) continue ;;
        esac
        # /sys/block uses '!' where /proc/diskstats uses '/' (cciss!c0d0 -> cciss/c0d0)
        echo "${name//!//}"
    done
}

# Display name: LVM, LUKS and multipath dm-N devices resolve to their mapper name.
display_name() {
    local kname="$1"
    local sysname="${kname//\//!}"
    local mapper_name
    if [[ -r "/sys/block/${sysname}/dm/name" ]]; then
        read -r mapper_name < "/sys/block/${sysname}/dm/name"
        echo "$mapper_name"
    else
        echo "$kname"
    fi
}

DISK_NAME_MAP=""
DISK_WIDTH=$MIN_NAME_WIDTH
DISKSTATS_FIELDS=0

setup_disks() {
    local requested kname dname

    if [[ -n "$DISK_LIST" ]]; then
        requested="${DISK_LIST//,/ }"
    else
        requested="$(list_disks | tr '\n' ' ')"
    fi

    for kname in $requested; do
        if ! awk -v d="$kname" '$3 == d {found = 1} END {exit !found}' /proc/diskstats; then
            warn "device '${kname}' not present in /proc/diskstats, skipping"
            continue
        fi
        dname="$(display_name "$kname")"
        DISK_NAME_MAP="${DISK_NAME_MAP} ${kname}=${dname}"
        if [[ ${#dname} -gt $DISK_WIDTH ]]; then
            DISK_WIDTH=${#dname}
        fi
    done

    if [[ -z "$DISK_NAME_MAP" ]]; then
        die "no reportable disks found"
    fi

    # Field count: 14 = base, 18 = adds discards (4.18+), 20 = adds flushes (5.5+)
    DISKSTATS_FIELDS="$(awk 'NR == 1 {print NF}' /proc/diskstats)"
    if [[ $DISKSTATS_FIELDS -lt 18 ]]; then
        note "kernel $(uname -r) does not expose discard counters (needs 4.18+); d/s and dkB/s omitted"
    fi
    if [[ $DISKSTATS_FIELDS -lt 20 ]]; then
        note "kernel $(uname -r) does not expose flush counters (needs 5.5+); f/s omitted"
    fi
}

# ---------------------------------------------------------------------------
# Network interface enumeration
# ---------------------------------------------------------------------------

# Every interface name in /proc/net/dev. Handles old kernels that print
# "eth0:12345" with no space after the colon.
list_interfaces() {
    awk 'NR > 2 {
        line = $0
        sub(/^ +/, "", line)
        sub(/:.*/, "", line)
        print line
    }' /proc/net/dev
}

is_virtual_interface() {
    case "$1" in
        lo|veth*|docker*|virbr*|cni*|br-*) return 0 ;;
        *) return 1 ;;
    esac
}

IFACE_NAMES=""
IFACE_WIDTH=$MIN_NAME_WIDTH

setup_interfaces() {
    local available iface token

    available="$(list_interfaces | tr '\n' ' ')"

    if [[ -z "$IFACE_LIST" ]]; then
        for iface in $available; do
            if ! is_virtual_interface "$iface"; then
                IFACE_NAMES="${IFACE_NAMES} ${iface}"
            fi
        done
    else
        for token in ${IFACE_LIST//,/ }; do
            if [[ "$token" == "all" ]]; then
                for iface in $available; do
                    if [[ "$iface" != "lo" ]] && ! list_has "$iface" "$IFACE_NAMES"; then
                        IFACE_NAMES="${IFACE_NAMES} ${iface}"
                    fi
                done
            elif list_has "$token" "$available"; then
                if ! list_has "$token" "$IFACE_NAMES"; then
                    IFACE_NAMES="${IFACE_NAMES} ${token}"
                fi
            else
                warn "interface '${token}' not present in /proc/net/dev, skipping"
            fi
        done
    fi

    if [[ -z "${IFACE_NAMES// /}" ]]; then
        die "no reportable network interfaces found"
    fi

    for iface in $IFACE_NAMES; do
        if [[ ${#iface} -gt $IFACE_WIDTH ]]; then
            IFACE_WIDTH=${#iface}
        fi
    done
}

# ---------------------------------------------------------------------------
# Memory capability check
# ---------------------------------------------------------------------------

MEM_AVAILABLE_FALLBACK=0

setup_memory() {
    if ! grep -q '^MemAvailable:' /proc/meminfo; then
        MEM_AVAILABLE_FALLBACK=1
        note "kernel $(uname -r) has no MemAvailable (needs 3.14+); using MemFree + Buffers + Cached"
    fi
}

# ---------------------------------------------------------------------------
# CSV output and manifest (-o)
# ---------------------------------------------------------------------------

# Path of each CSV file. Empty means that file is not written.
CSV_CPU="" CSV_MEM="" CSV_DISK="" CSV_NET="" CSV_LOAD=""

# Per-run directory, set by setup_csv.
WRITE_DIR=""

# Create the file with its header, or confirm an existing file has the same
# header before appending to it.
init_csv_file() {
    local path="$1" header="$2" existing=""

    if [[ -s "$path" ]]; then
        read -r existing < "$path" || true
        if [[ "$existing" != "$header" ]]; then
            die "$path already exists with a different header; use another directory or remove it"
        fi
    else
        echo "$header" >> "$path" || die "cannot write $path"
    fi
}

setup_csv() {
    local disk_header run_dir

    run_dir="${OUTDIR_BASE}/$(short_hostname)_psar_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$run_dir" || die "cannot create directory '$run_dir'"
    if [[ ! -w "$run_dir" ]]; then
        die "directory '$run_dir' is not writable; run with sudo or pass a different -o <dir>"
    fi
    WRITE_DIR="$run_dir"

    if [[ $SHOW_CPU -eq 1 ]]; then
        CSV_CPU="$WRITE_DIR/cpu.csv"
        init_csv_file "$CSV_CPU" \
            "ts,usr_pct,sys_pct,irq_pct,soft_pct,iowait_pct,steal_pct,idle_pct,run,blkd,cs_s"
    fi

    if [[ $SHOW_MEM -eq 1 ]]; then
        CSV_MEM="$WRITE_DIR/mem.csv"
        init_csv_file "$CSV_MEM" \
            "ts,usedkB,availkB,used_pct,dirtykB,wbackkB,swusedkB,swpin_s,swpout_s,majflt_s"
    fi

    if [[ $SHOW_DISK -eq 1 ]]; then
        CSV_DISK="$WRITE_DIR/disk.csv"
        disk_header="ts,device,r_s,w_s,rkB_s,wkB_s,rareq-sz,wareq-sz,r_await,w_await,aqu-sz,util_pct"
        if [[ $DISKSTATS_FIELDS -ge 18 ]]; then disk_header="${disk_header},d_s,dkB_s"; fi
        if [[ $DISKSTATS_FIELDS -ge 20 ]]; then disk_header="${disk_header},f_s"; fi
        init_csv_file "$CSV_DISK" "$disk_header"
    fi

    if [[ $SHOW_NET -eq 1 ]]; then
        CSV_NET="$WRITE_DIR/net.csv"
        init_csv_file "$CSV_NET" \
            "ts,iface,rxkB_s,txkB_s,rxpck_s,txpck_s,rxerr_s,txerr_s,rxdrop_s,txdrop_s"
    fi

    if [[ $SHOW_LOAD -eq 1 ]]; then
        CSV_LOAD="$WRITE_DIR/load.csv"
        init_csv_file "$CSV_LOAD" "ts,1m,5m,15m"
    fi

    write_manifest
}

write_manifest() {
    local sections=""
    if [[ $SHOW_CPU  -eq 1 ]]; then sections="${sections} cpu"; fi
    if [[ $SHOW_MEM  -eq 1 ]]; then sections="${sections} mem"; fi
    if [[ $SHOW_DISK -eq 1 ]]; then sections="${sections} disk"; fi
    if [[ $SHOW_NET  -eq 1 ]]; then sections="${sections} net"; fi
    if [[ $SHOW_LOAD -eq 1 ]]; then sections="${sections} load"; fi

    if [[ $COUNT -eq 0 ]]; then
        manifest_start "${WRITE_DIR}/manifest.txt" "$INTERVAL" "unlimited" "unlimited"
    else
        manifest_start "${WRITE_DIR}/manifest.txt" "$INTERVAL" "$COUNT" "$(( COUNT * INTERVAL ))"
    fi
    manifest_add sections "${sections# }"
    manifest_add disks "${DISK_NAME_MAP# }"
    manifest_add ifaces "${IFACE_NAMES# }"
    manifest_add notes "$NOTES_TEXT"
}

# ---------------------------------------------------------------------------
# Section printers
#
# Each takes the previous and current snapshot (as strings), the console
# timestamp, the real elapsed centiseconds, whether to print the header and the
# ISO 8601 timestamp used in CSV rows. Each runs one awk process, which also
# appends the CSV rows when its CSV_* path is set. Negative deltas (counter
# reset or wrap) are clamped to 0.
# ---------------------------------------------------------------------------

print_cpu() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5" iso="$6"

    awk -v ts="$ts" -v elapsed_cs="$elapsed_cs" -v header="$header" -v lw="$LABEL_WIDTH" \
        -v iso="$iso" -v csv="$CSV_CPU" '
        function clamp(v) { return (v < 0) ? 0 : v }
        BEGIN {
            if (header) {
                printf "%-8s %-" lw "s %7s %7s %7s %7s %7s %7s %7s %5s %5s %9s\n", \
                    ts, "CPU", "%usr", "%sys", "%irq", "%soft", "%iowait", "%steal", "%idle", \
                    "run", "blkd", "cs/s"
            }
        }
        # First input: previous snapshot
        FNR == NR {
            if ($1 == "cpu") {
                for (f = 2; f <= NF; f++) prev_cpu[f] = $f
            } else if ($1 == "ctxt") {
                prev_ctxt = $2
            }
            next
        }
        # Second input: current snapshot
        $1 == "cpu"           { for (f = 2; f <= NF; f++) curr_cpu[f] = $f }
        $1 == "ctxt"          { curr_ctxt = $2 }
        $1 == "procs_running" { running = $2 }
        $1 == "procs_blocked" { blocked = $2 }
        END {
            # /proc/stat cpu fields: 2 user, 3 nice, 4 system, 5 idle, 6 iowait,
            # 7 irq, 8 softirq, 9 steal. Guest time is already inside user.
            usr     = clamp(curr_cpu[2] - prev_cpu[2]) + clamp(curr_cpu[3] - prev_cpu[3])
            sys     = clamp(curr_cpu[4] - prev_cpu[4])
            idle    = clamp(curr_cpu[5] - prev_cpu[5])
            iowait  = clamp(curr_cpu[6] - prev_cpu[6])
            irq     = clamp(curr_cpu[7] - prev_cpu[7])
            softirq = clamp(curr_cpu[8] - prev_cpu[8])
            steal   = clamp(curr_cpu[9] - prev_cpu[9])
            total = usr + sys + idle + iowait + irq + softirq + steal

            if (total > 0) {
                scale = 100 / total
            } else {
                scale = 0
            }
            cs_rate = clamp(curr_ctxt - prev_ctxt) / (elapsed_cs / 100)

            printf "%-8s %-" lw "s %7.1f %7.1f %7.1f %7.1f %7.1f %7.1f %7.1f %5.0f %5.0f %9.1f\n", \
                ts, "all", usr * scale, sys * scale, irq * scale, softirq * scale, \
                iowait * scale, steal * scale, idle * scale, running, blocked, cs_rate

            if (csv != "") {
                printf "%s,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.0f,%.0f,%.1f\n", \
                    iso, usr * scale, sys * scale, irq * scale, softirq * scale, \
                    iowait * scale, steal * scale, idle * scale, running, blocked, cs_rate >> csv
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_mem() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5" iso="$6"

    awk -v ts="$ts" -v elapsed_cs="$elapsed_cs" -v header="$header" -v lw="$LABEL_WIDTH" \
        -v fallback="$MEM_AVAILABLE_FALLBACK" -v iso="$iso" -v csv="$CSV_MEM" '
        function clamp(v) { return (v < 0) ? 0 : v }
        BEGIN {
            if (header) {
                printf "%-8s %-" lw "s %10s %10s %6s %9s %9s %9s %8s %8s %8s\n", \
                    ts, "MEM", "usedkB", "availkB", "%used", "dirtykB", "wbackkB", \
                    "swusedkB", "swpin/s", "swpout/s", "majflt/s"
            }
        }
        # Snapshots hold /proc/meminfo followed by /proc/vmstat. Keys do not
        # overlap (meminfo keys end in a colon), so one table serves both.
        FNR == NR { prev[$1] = $2; next }
        { curr[$1] = $2 }
        END {
            elapsed_s = elapsed_cs / 100

            total = curr["MemTotal:"]
            if (fallback) {
                avail = curr["MemFree:"] + curr["Buffers:"] + curr["Cached:"]
            } else {
                avail = curr["MemAvailable:"]
            }
            used = total - avail
            if (total > 0) {
                pct_used = used * 100 / total
            } else {
                pct_used = 0
            }
            swap_used = curr["SwapTotal:"] - curr["SwapFree:"]

            swpin   = clamp(curr["pswpin"] - prev["pswpin"]) / elapsed_s
            swpout  = clamp(curr["pswpout"] - prev["pswpout"]) / elapsed_s
            majflt  = clamp(curr["pgmajfault"] - prev["pgmajfault"]) / elapsed_s

            printf "%-8s %-" lw "s %10.0f %10.0f %6.1f %9.0f %9.0f %9.0f %8.1f %8.1f %8.1f\n", \
                ts, "-", used, avail, pct_used, curr["Dirty:"], curr["Writeback:"], \
                swap_used, swpin, swpout, majflt

            if (csv != "") {
                printf "%s,%.0f,%.0f,%.1f,%.0f,%.0f,%.0f,%.1f,%.1f,%.1f\n", \
                    iso, used, avail, pct_used, curr["Dirty:"], curr["Writeback:"], \
                    swap_used, swpin, swpout, majflt >> csv
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_disk() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5" iso="$6"

    awk -v name_map="$DISK_NAME_MAP" -v elapsed_cs="$elapsed_cs" -v ts="$ts" \
        -v w="$DISK_WIDTH" -v nf="$DISKSTATS_FIELDS" -v header="$header" \
        -v iso="$iso" -v csv="$CSV_DISK" '
        function clamp(v) { return (v < 0) ? 0 : v }
        BEGIN {
            n = split(name_map, pairs, " ")
            for (i = 1; i <= n; i++) {
                split(pairs[i], kv, "=")
                display[kv[1]] = kv[2]
                order[i] = kv[1]
            }
            ndev = n
            elapsed_s  = elapsed_cs / 100
            elapsed_ms = elapsed_cs * 10

            if (header) {
                printf "%-8s %-" w "s %8s %8s %10s %10s %8s %8s %8s %8s %7s %6s", \
                    ts, "DEV", "r/s", "w/s", "rkB/s", "wkB/s", "rareq-sz", "wareq-sz", \
                    "r_await", "w_await", "aqu-sz", "%util"
                if (nf >= 18) printf " %8s %10s", "d/s", "dkB/s"
                if (nf >= 20) printf " %8s", "f/s"
                printf "\n"
            }
        }
        # First input: previous snapshot. Every field is stored so any column can be diffed.
        FNR == NR {
            if ($3 in display) {
                for (f = 4; f <= NF; f++) prev[$3, f] = $f
                seen[$3] = 1
            }
            next
        }
        # Second input: current snapshot.
        ($3 in display) && ($3 in seen) {
            for (f = 4; f <= NF; f++) delta[$3, f] = clamp($f - prev[$3, f])
            have[$3] = 1
        }
        END {
            for (i = 1; i <= ndev; i++) {
                d = order[i]
                if (!(d in have)) continue

                reads  = delta[d, 4];  rsec = delta[d, 6];  rms = delta[d, 7]
                writes = delta[d, 8];  wsec = delta[d, 10]; wms = delta[d, 11]
                io_ms  = delta[d, 13]; weighted_ms = delta[d, 14]

                # Sectors in /proc/diskstats are always 512 bytes, whatever the
                # device logical block size. 2 sectors = 1 kB.
                r_s   = reads  / elapsed_s
                w_s   = writes / elapsed_s
                rkb_s = rsec / 2 / elapsed_s
                wkb_s = wsec / 2 / elapsed_s
                rsz   = (reads  > 0) ? (rsec / 2) / reads  : 0
                wsz   = (writes > 0) ? (wsec / 2) / writes : 0
                r_aw  = (reads  > 0) ? rms / reads  : 0
                w_aw  = (writes > 0) ? wms / writes : 0
                aqu   = weighted_ms / elapsed_ms
                util  = io_ms * 100 / elapsed_ms
                if (util > 100) util = 100

                disc_s    = delta[d, 15] / elapsed_s
                disc_kb_s = delta[d, 17] / 2 / elapsed_s
                flush_s   = delta[d, 19] / elapsed_s

                printf "%-8s %-" w "s %8.1f %8.1f %10.1f %10.1f %8.1f %8.1f %8.2f %8.2f %7.2f %6.1f", \
                    ts, display[d], r_s, w_s, rkb_s, wkb_s, rsz, wsz, r_aw, w_aw, aqu, util
                if (nf >= 18) printf " %8.1f %10.1f", disc_s, disc_kb_s
                if (nf >= 20) printf " %8.1f", flush_s
                printf "\n"

                if (csv != "") {
                    printf "%s,%s,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.2f,%.2f,%.2f,%.1f", \
                        iso, display[d], r_s, w_s, rkb_s, wkb_s, rsz, wsz, r_aw, w_aw, aqu, util >> csv
                    if (nf >= 18) printf ",%.1f,%.1f", disc_s, disc_kb_s >> csv
                    if (nf >= 20) printf ",%.1f", flush_s >> csv
                    printf "\n" >> csv
                }
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_net() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5" iso="$6"

    awk -v names="$IFACE_NAMES" -v elapsed_cs="$elapsed_cs" -v ts="$ts" \
        -v w="$IFACE_WIDTH" -v header="$header" -v iso="$iso" -v csv="$CSV_NET" '
        function clamp(v) { return (v < 0) ? 0 : v }
        BEGIN {
            n = split(names, list, " ")
            for (i = 1; i <= n; i++) wanted[list[i]] = i
            elapsed_s = elapsed_cs / 100

            if (header) {
                printf "%-8s %-" w "s %10s %10s %9s %9s %9s %9s %9s %9s\n", \
                    ts, "IFACE", "rxkB/s", "txkB/s", "rxpck/s", "txpck/s", \
                    "rxerr/s", "txerr/s", "rxdrop/s", "txdrop/s"
            }
        }
        # /proc/net/dev lines look like "  eth0: 123 ..." and on some old kernels
        # "eth0:123 ...". Replacing the first colon with a space handles both.
        # Fields then: 1 name, 2 rx bytes, 3 rx packets, 4 rx errs, 5 rx drop,
        # 10 tx bytes, 11 tx packets, 12 tx errs, 13 tx drop.
        { sub(/:/, " ") }
        $1 in wanted && FNR == NR {
            for (f = 2; f <= 13; f++) prev[$1, f] = $f
            seen[$1] = 1
            next
        }
        FNR == NR { next }
        ($1 in wanted) && ($1 in seen) {
            for (f = 2; f <= 13; f++) delta[$1, f] = clamp($f - prev[$1, f])
            have[$1] = 1
        }
        END {
            for (i = 1; i <= n; i++) {
                d = list[i]
                if (!(d in have)) continue
                rx_kb = delta[d, 2] / 1024 / elapsed_s;  tx_kb = delta[d, 10] / 1024 / elapsed_s
                rx_pk = delta[d, 3] / elapsed_s;         tx_pk = delta[d, 11] / elapsed_s
                rx_er = delta[d, 4] / elapsed_s;         tx_er = delta[d, 12] / elapsed_s
                rx_dr = delta[d, 5] / elapsed_s;         tx_dr = delta[d, 13] / elapsed_s

                printf "%-8s %-" w "s %10.1f %10.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f\n", \
                    ts, d, rx_kb, tx_kb, rx_pk, tx_pk, rx_er, tx_er, rx_dr, tx_dr

                if (csv != "") {
                    printf "%s,%s,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f\n", \
                        iso, d, rx_kb, tx_kb, rx_pk, tx_pk, rx_er, tx_er, rx_dr, tx_dr >> csv
                }
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_load() {
    local ts="$1" header="$2" iso="$3"
    local load_1m load_5m load_15m

    read -r load_1m load_5m load_15m _ < /proc/loadavg
    if [[ $header -eq 1 ]]; then
        printf "%-8s %-${LABEL_WIDTH}s %6s %6s %6s\n" "$ts" "LOAD" "1m" "5m" "15m"
    fi
    printf "%-8s %-${LABEL_WIDTH}s %6s %6s %6s\n" "$ts" "-" "$load_1m" "$load_5m" "$load_15m"
    if [[ -n "$CSV_LOAD" ]]; then
        printf '%s,%s,%s,%s\n' "$iso" "$load_1m" "$load_5m" "$load_15m" >> "$CSV_LOAD"
    fi
}

# Blank line between blocks, except in header-once mode where output is a
# single continuous table.
end_block() {
    if [[ $NO_REPEAT_HEADERS -eq 0 ]]; then
        echo
    fi
}

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

snap_cpu="" snap_mem="" snap_disk="" snap_net=""
prev_cpu="" prev_mem="" prev_disk="" prev_net=""

# $(< file) is a bash builtin redirect, not a cat fork.
take_snapshots() {
    if [[ $SHOW_CPU  -eq 1 ]]; then snap_cpu="$(< /proc/stat)"; fi
    if [[ $SHOW_MEM  -eq 1 ]]; then snap_mem="$(< /proc/meminfo)"$'\n'"$(< /proc/vmstat)"; fi
    if [[ $SHOW_DISK -eq 1 ]]; then snap_disk="$(< /proc/diskstats)"; fi
    if [[ $SHOW_NET  -eq 1 ]]; then snap_net="$(< /proc/net/dev)"; fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if [[ $SHOW_DISK -eq 1 ]]; then setup_disks; fi
if [[ $SHOW_NET  -eq 1 ]]; then setup_interfaces; fi
if [[ $SHOW_MEM  -eq 1 ]]; then setup_memory; fi
if [[ -n "$OUTDIR_BASE" ]]; then
    # Appends end= to manifest.txt when sampling finishes or is stopped.
    trap manifest_finish EXIT
    setup_csv
fi

take_snapshots
prev_cpu="$snap_cpu"; prev_mem="$snap_mem"; prev_disk="$snap_disk"; prev_net="$snap_net"
prev_cs="$(read_uptime_cs)"

sample=0
while [[ $COUNT -eq 0 || $sample -lt $COUNT ]]; do
    sleep "$INTERVAL"

    # Section snapshots first, then a single uptime read, so the elapsed time
    # covers the same window as the counters.
    take_snapshots
    curr_cs="$(read_uptime_cs)"
    elapsed_cs=$(( curr_cs - prev_cs ))
    if [[ $elapsed_cs -lt 1 ]]; then
        elapsed_cs=1
    fi
    # One date call gives both timestamps: "2026-10-16T12:19:11+00:00" -> "12:19:11"
    iso="$(date -Iseconds)"
    ts="${iso:11:8}"

    if [[ $NO_REPEAT_HEADERS -eq 1 && $sample -gt 0 ]]; then
        header=0
    else
        header=1
    fi

    if [[ $SHOW_CPU -eq 1 ]]; then
        print_cpu "$prev_cpu" "$snap_cpu" "$ts" "$elapsed_cs" "$header" "$iso"
        end_block
    fi
    if [[ $SHOW_MEM -eq 1 ]]; then
        print_mem "$prev_mem" "$snap_mem" "$ts" "$elapsed_cs" "$header" "$iso"
        end_block
    fi
    if [[ $SHOW_DISK -eq 1 ]]; then
        print_disk "$prev_disk" "$snap_disk" "$ts" "$elapsed_cs" "$header" "$iso"
        end_block
    fi
    if [[ $SHOW_NET -eq 1 ]]; then
        print_net "$prev_net" "$snap_net" "$ts" "$elapsed_cs" "$header" "$iso"
        end_block
    fi
    if [[ $SHOW_LOAD -eq 1 ]]; then
        print_load "$ts" "$header" "$iso"
        end_block
    fi

    prev_cpu="$snap_cpu"; prev_mem="$snap_mem"; prev_disk="$snap_disk"; prev_net="$snap_net"
    prev_cs="$curr_cs"
    sample=$(( sample + 1 ))
done
