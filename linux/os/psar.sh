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

PROG="${0##*/}"

INTERVAL=2
COUNT=0
DISK_LIST=""
IFACE_LIST=""
WRITE_DIR=""
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

Usage: $PROG [-i interval] [-c count] [-n] [-d disk[,disk...]] [-e iface[,iface...]]
       $(printf '%*s' "${#PROG}" '') [-C] [-M] [-D] [-N] [-L] [-A] [-w dir] [-h]

  -i  interval in whole seconds, 1 or more (default 2)
  -c  number of samples, 0 = infinite (default 0)
  -n  print each section header once (vmstat-style). Ignored with a
      warning when more than one section is active
  -d  disks to report, by kernel name, comma separated: vda, sda, nvme0n1, dm-0
      Implies -D. Default: every whole disk (no partitions, loop, ram, zram, sr)
  -e  network interfaces to report, comma separated. Implies -N.
      Default: every interface except lo, veth*, docker*, virbr*, cni*, br-*
      "all" selects every interface except lo. Name lo explicitly to include it
  -C  CPU
  -M  memory
  -D  disk
  -N  network
  -L  load average
  -A  all sections (overrides individual section flags)
  -w  directory for CSV files (not yet implemented)
  -h  show this help and exit

With no section flag, all sections are shown. Section flags are additive.
"-A -d vda" shows every section, with the disk section limited to vda.

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

Reading %util: it only indicates saturation on single-queue devices such as
spinning disks. NVMe, SSD arrays and cloud block storage service many IOs in
parallel and can be far from saturated at 100%. For those, use aqu-sz and the
await columns.

Stacked devices are counted at each layer. A write to an LVM volume on vda3
appears on both the mapper device and on vda, as in sar -d and iostat.

Examples:
  $PROG                     # all sections every 2s, forever
  $PROG -D -c 5             # all whole disks, 5 samples
  $PROG -d vda,vdb -i 1     # only vda and vdb, every second
  $PROG -A -d vda -c 3      # every section, disk section limited to vda
  $PROG -e eth0 -n          # eth0 only, header printed once
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

warn() {
    echo "WARNING: $*" >&2
}

note() {
    echo "NOTE: $*" >&2
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

while getopts "i:c:d:e:w:CMDNLAnh" opt; do
    case "$opt" in
        i) INTERVAL=$OPTARG ;;
        c) COUNT=$OPTARG ;;
        d) DISK_LIST=$OPTARG; SHOW_DISK=1 ;;
        e) IFACE_LIST=$OPTARG; SHOW_NET=1 ;;
        w) WRITE_DIR=$OPTARG ;;
        C) SHOW_CPU=1 ;;
        M) SHOW_MEM=1 ;;
        D) SHOW_DISK=1 ;;
        N) SHOW_NET=1 ;;
        L) SHOW_LOAD=1 ;;
        A) SHOW_ALL=1 ;;
        n) NO_REPEAT_HEADERS=1 ;;
        h) print_help; exit 0 ;;
        *) print_help >&2; exit 1 ;;
    esac
done
shift $(( OPTIND - 1 ))

if [[ $# -gt 0 ]]; then
    die "unexpected argument '$1' (see -h)"
fi

if [[ ! "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 1 ]]; then
    die "-i needs a whole number of seconds, 1 or more (got '$INTERVAL')"
fi

if [[ ! "$COUNT" =~ ^[0-9]+$ ]]; then
    die "-c needs a whole number, 0 or more (got '$COUNT')"
fi

if [[ -n "$WRITE_DIR" ]]; then
    die "-w (CSV output) is not implemented yet"
fi

# -A, or no section selected at all, means every section.
if [[ $SHOW_ALL -eq 1 ]] \
    || [[ $(( SHOW_CPU + SHOW_MEM + SHOW_DISK + SHOW_NET + SHOW_LOAD )) -eq 0 ]]; then
    SHOW_CPU=1; SHOW_MEM=1; SHOW_DISK=1; SHOW_NET=1; SHOW_LOAD=1
fi

ACTIVE_SECTIONS=$(( SHOW_CPU + SHOW_MEM + SHOW_DISK + SHOW_NET + SHOW_LOAD ))

if [[ $NO_REPEAT_HEADERS -eq 1 && $ACTIVE_SECTIONS -gt 1 ]]; then
    warn "-n ignored: headers are needed to tell $ACTIVE_SECTIONS interleaved sections apart"
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
# Section printers
#
# Each takes the previous and current snapshot (as strings), the timestamp,
# the real elapsed centiseconds and whether to print the header. Each runs
# one awk process. Negative deltas (counter reset or wrap) are clamped to 0.
# ---------------------------------------------------------------------------

print_cpu() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5"

    awk -v ts="$ts" -v elapsed_cs="$elapsed_cs" -v header="$header" -v lw="$LABEL_WIDTH" '
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
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_mem() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5"

    awk -v ts="$ts" -v elapsed_cs="$elapsed_cs" -v header="$header" -v lw="$LABEL_WIDTH" \
        -v fallback="$MEM_AVAILABLE_FALLBACK" '
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
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_disk() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5"

    awk -v name_map="$DISK_NAME_MAP" -v elapsed_cs="$elapsed_cs" -v ts="$ts" \
        -v w="$DISK_WIDTH" -v nf="$DISKSTATS_FIELDS" -v header="$header" '
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

                printf "%-8s %-" w "s %8.1f %8.1f %10.1f %10.1f %8.1f %8.1f %8.2f %8.2f %7.2f %6.1f", \
                    ts, display[d], r_s, w_s, rkb_s, wkb_s, rsz, wsz, r_aw, w_aw, aqu, util
                if (nf >= 18) printf " %8.1f %10.1f", delta[d, 15] / elapsed_s, delta[d, 17] / 2 / elapsed_s
                if (nf >= 20) printf " %8.1f", delta[d, 19] / elapsed_s
                printf "\n"
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_net() {
    local prev="$1" curr="$2" ts="$3" elapsed_cs="$4" header="$5"

    awk -v names="$IFACE_NAMES" -v elapsed_cs="$elapsed_cs" -v ts="$ts" \
        -v w="$IFACE_WIDTH" -v header="$header" '
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
                printf "%-8s %-" w "s %10.1f %10.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f\n", \
                    ts, d, \
                    delta[d, 2] / 1024 / elapsed_s, delta[d, 10] / 1024 / elapsed_s, \
                    delta[d, 3] / elapsed_s, delta[d, 11] / elapsed_s, \
                    delta[d, 4] / elapsed_s, delta[d, 12] / elapsed_s, \
                    delta[d, 5] / elapsed_s, delta[d, 13] / elapsed_s
            }
        }
    ' <(printf '%s\n' "$prev") <(printf '%s\n' "$curr")
}

print_load() {
    local ts="$1" header="$2"
    local load_1m load_5m load_15m

    read -r load_1m load_5m load_15m _ < /proc/loadavg
    if [[ $header -eq 1 ]]; then
        printf "%-8s %-${LABEL_WIDTH}s %6s %6s %6s\n" "$ts" "LOAD" "1m" "5m" "15m"
    fi
    printf "%-8s %-${LABEL_WIDTH}s %6s %6s %6s\n" "$ts" "-" "$load_1m" "$load_5m" "$load_15m"
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
    ts="$(date +%H:%M:%S)"

    if [[ $NO_REPEAT_HEADERS -eq 1 && $sample -gt 0 ]]; then
        header=0
    else
        header=1
    fi

    if [[ $SHOW_CPU -eq 1 ]]; then
        print_cpu "$prev_cpu" "$snap_cpu" "$ts" "$elapsed_cs" "$header"
        end_block
    fi
    if [[ $SHOW_MEM -eq 1 ]]; then
        print_mem "$prev_mem" "$snap_mem" "$ts" "$elapsed_cs" "$header"
        end_block
    fi
    if [[ $SHOW_DISK -eq 1 ]]; then
        print_disk "$prev_disk" "$snap_disk" "$ts" "$elapsed_cs" "$header"
        end_block
    fi
    if [[ $SHOW_NET -eq 1 ]]; then
        print_net "$prev_net" "$snap_net" "$ts" "$elapsed_cs" "$header"
        end_block
    fi
    if [[ $SHOW_LOAD -eq 1 ]]; then
        print_load "$ts" "$header"
        end_block
    fi

    prev_cpu="$snap_cpu"; prev_mem="$snap_mem"; prev_disk="$snap_disk"; prev_net="$snap_net"
    prev_cs="$curr_cs"
    sample=$(( sample + 1 ))
done
