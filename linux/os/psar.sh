#!/usr/bin/env bash
#
# psar.sh — sar-like sampler with no dependency on sysstat.
# Reads /proc/stat, /proc/meminfo, /proc/diskstats, /proc/net/dev, /proc/loadavg
# directly and computes deltas between samples, same way sar does internally.
#
# Run with -h for usage.

set -euo pipefail

INTERVAL=2
COUNT=0
DISK=""
IFACE=""
SHOW_CPU=0
SHOW_MEM=0
SHOW_DISK=0
SHOW_NET=0
ANY_FLAG=0
NO_REPEAT_HEADERS=0

print_help() {
    cat <<EOF
psar.sh — sar-like sampler with no dependency on sysstat.

Usage: $0 [-i interval] [-c count] [-n] [-d disk] [-e iface] [-C|-M|-D|-N|-A] [-h]

  -i  interval in seconds (default 2)
  -c  number of samples, 0 = infinite (default 0)
  -n  no repeated headers — print header once only (vmstat-style)
  -d  disk device to report, e.g. sda, nvme0n1 (default: first non-loop disk)
  -e  network interface to report (default: first non-lo interface)
  -C  CPU only
  -M  memory only
  -D  disk only
  -N  network only
  -A  all (default if no flag given)
  -h  show this help and exit

Examples:
  $0                      # everything, every 2s, forever
  $0 -C -i 1 -c 10        # CPU only, 1s interval, 10 samples
  $0 -D -d nvme0n1p1      # disk only, specific partition
  $0 -n -c 50             # header printed once, then 50 samples
EOF
}

while getopts "i:c:d:e:CMDNAnh" opt; do
    case "$opt" in
        i) INTERVAL=$OPTARG ;;
        c) COUNT=$OPTARG ;;
        d) DISK=$OPTARG ;;
        e) IFACE=$OPTARG ;;
        C) SHOW_CPU=1; ANY_FLAG=1 ;;
        M) SHOW_MEM=1; ANY_FLAG=1 ;;
        D) SHOW_DISK=1; ANY_FLAG=1 ;;
        N) SHOW_NET=1; ANY_FLAG=1 ;;
        A) ANY_FLAG=1 ;;
        n) NO_REPEAT_HEADERS=1 ;;
        h) print_help; exit 0 ;;
        *) print_help >&2; exit 1 ;;
    esac
done

if [[ $ANY_FLAG -eq 0 ]] || { [[ $SHOW_CPU -eq 0 ]] && [[ $SHOW_MEM -eq 0 ]] && [[ $SHOW_DISK -eq 0 ]] && [[ $SHOW_NET -eq 0 ]]; }; then
    SHOW_CPU=1; SHOW_MEM=1; SHOW_DISK=1; SHOW_NET=1
fi

# Pick sensible defaults for disk/iface if not specified
if [[ -z "$DISK" ]]; then
    DISK=$(awk '$4 !~ /loop|ram/ && $4 !~ /[0-9]$/ {print $4; exit}' /proc/diskstats)
    [[ -z "$DISK" ]] && DISK=$(awk '$4 !~ /loop|ram/ {print $4; exit}' /proc/diskstats)
fi
if [[ -z "$IFACE" ]]; then
    IFACE=$(awk -F: '$1 !~ /lo/ && NR>2 {gsub(/ /,"",$1); print $1; exit}' /proc/net/dev)
fi

CLK_TCK=$(getconf CLK_TCK)
NCPU=$(nproc)

read_cpu() {
    # user nice system idle iowait irq softirq steal
    awk '/^cpu / {print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat
}

read_disk() {
    # fields: reads_completed, sectors_read, writes_completed, sectors_written, ms_io_in_progress-ish (field 13, ms doing I/O)
    awk -v d="$DISK" '$3==d {print $4,$6,$8,$10,$13}' /proc/diskstats
}

read_net() {
    awk -v i="$IFACE" -F: '$1 ~ i {gsub(/^ +/,"",$2); split($2,a," "); print a[1],a[9]}' /proc/net/dev
}

print_mem() {
    local ts=$1
    awk -v ts="$ts" '
        /^MemTotal:/     {tot=$2}
        /^MemFree:/      {free=$2}
        /^MemAvailable:/ {avail=$2}
        /^Buffers:/      {buf=$2}
        /^Cached:/       {cache=$2}
        /^SwapTotal:/    {stot=$2}
        /^SwapFree:/     {sfree=$2}
        END {
            used = tot - avail
            pct = (tot>0) ? used*100/tot : 0
            swused = stot - sfree
            swpct = (stot>0) ? swused*100/stot : 0
            printf "%-9s %-8s %10s %10s %8s%% %10s %10s %8s%%\n", \
                ts, "MEM", used"kB", avail"kB", pct, swused"kB", stot"kB", swpct
        }
    ' /proc/meminfo
}

print_load() {
    local ts=$1
    read -r l1 l5 l15 _ < /proc/loadavg
    printf "%-9s %-8s %6s %6s %6s\n" "$ts" "LOAD" "$l1" "$l5" "$l15"
}

header_printed=0
print_headers() {
    [[ $SHOW_CPU  -eq 1 ]] && printf "%-9s %-8s %8s %8s %8s %8s %8s\n" TIME CPU %usr %sys %iowait %steal %idle
    [[ $SHOW_MEM  -eq 1 ]] && printf "%-9s %-8s %10s %10s %9s %10s %10s %9s\n" TIME MEM used avail "%used" swused swtotal "%swap"
    [[ $SHOW_DISK -eq 1 ]] && printf "%-9s %-8s %10s %12s %12s\n" TIME "DISK($DISK)" tps "rd_sec/s" "wr_sec/s"
    [[ $SHOW_NET  -eq 1 ]] && printf "%-9s %-8s %12s %12s\n" TIME "NET($IFACE)" "rxB/s" "txB/s"
    printf "%-9s %-8s %6s %6s %6s\n" TIME LOAD 1m 5m 15m
    header_printed=1
}

sample=0
prev_cpu=($(read_cpu))
prev_disk=($(read_disk))
prev_net=($(read_net))

while true; do
    sleep "$INTERVAL"
    ts=$(date +%H:%M:%S)

    if [[ $header_printed -eq 0 ]]; then
        print_headers
    elif [[ $NO_REPEAT_HEADERS -eq 0 && $((sample % 20)) -eq 0 ]]; then
        print_headers
    fi

    if [[ $SHOW_CPU -eq 1 ]]; then
        curr_cpu=($(read_cpu))
        d_user=$((curr_cpu[0]-prev_cpu[0])); d_nice=$((curr_cpu[1]-prev_cpu[1]))
        d_sys=$((curr_cpu[2]-prev_cpu[2]));  d_idle=$((curr_cpu[3]-prev_cpu[3]))
        d_iowait=$((curr_cpu[4]-prev_cpu[4])); d_irq=$((curr_cpu[5]-prev_cpu[5]))
        d_softirq=$((curr_cpu[6]-prev_cpu[6])); d_steal=$((curr_cpu[7]-prev_cpu[7]))
        total=$((d_user+d_nice+d_sys+d_idle+d_iowait+d_irq+d_softirq+d_steal))
        if [[ $total -gt 0 ]]; then
            pct_usr=$(awk -v a=$((d_user+d_nice)) -v t=$total 'BEGIN{printf "%.1f", a*100/t}')
            pct_sys=$(awk -v a=$((d_sys+d_irq+d_softirq)) -v t=$total 'BEGIN{printf "%.1f", a*100/t}')
            pct_iowait=$(awk -v a=$d_iowait -v t=$total 'BEGIN{printf "%.1f", a*100/t}')
            pct_steal=$(awk -v a=$d_steal -v t=$total 'BEGIN{printf "%.1f", a*100/t}')
            pct_idle=$(awk -v a=$d_idle -v t=$total 'BEGIN{printf "%.1f", a*100/t}')
        else
            pct_usr=0; pct_sys=0; pct_iowait=0; pct_steal=0; pct_idle=0
        fi
        printf "%-9s %-8s %8s %8s %8s %8s %8s\n" "$ts" "CPU" "$pct_usr" "$pct_sys" "$pct_iowait" "$pct_steal" "$pct_idle"
        prev_cpu=("${curr_cpu[@]}")
    fi

    [[ $SHOW_MEM -eq 1 ]] && print_mem "$ts"

    if [[ $SHOW_DISK -eq 1 && -n "$DISK" ]]; then
        curr_disk=($(read_disk))
        if [[ ${#curr_disk[@]} -eq 5 ]]; then
            d_reads=$((curr_disk[0]-prev_disk[0])); d_rsec=$((curr_disk[1]-prev_disk[1]))
            d_writes=$((curr_disk[2]-prev_disk[2])); d_wsec=$((curr_disk[3]-prev_disk[3]))
            tps=$(awk -v r=$((d_reads+d_writes)) -v s=$INTERVAL 'BEGIN{printf "%.1f", r/s}')
            rsec_s=$(awk -v v=$d_rsec -v s=$INTERVAL 'BEGIN{printf "%.1f", v/s}')
            wsec_s=$(awk -v v=$d_wsec -v s=$INTERVAL 'BEGIN{printf "%.1f", v/s}')
            printf "%-9s %-8s %10s %12s %12s\n" "$ts" "DISK($DISK)" "$tps" "$rsec_s" "$wsec_s"
            prev_disk=("${curr_disk[@]}")
        fi
    fi

    if [[ $SHOW_NET -eq 1 && -n "$IFACE" ]]; then
        curr_net=($(read_net))
        if [[ ${#curr_net[@]} -eq 2 ]]; then
            d_rx=$((curr_net[0]-prev_net[0])); d_tx=$((curr_net[1]-prev_net[1]))
            rx_s=$(awk -v v=$d_rx -v s=$INTERVAL 'BEGIN{printf "%.0f", v/s}')
            tx_s=$(awk -v v=$d_tx -v s=$INTERVAL 'BEGIN{printf "%.0f", v/s}')
            printf "%-9s %-8s %12s %12s\n" "$ts" "NET($IFACE)" "$rx_s" "$tx_s"
            prev_net=("${curr_net[@]}")
        fi
    fi

    print_load "$ts"

    sample=$((sample+1))
    if [[ $COUNT -gt 0 && $sample -ge $COUNT ]]; then
        break
    fi
done
