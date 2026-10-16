#!/usr/bin/env bash
#
# collect-hw-inventory.sh
# One-shot hardware inventory: CPU topology, memory settings, disk stacking,
# RAID membership, media class and filesystem-to-device map. Writes no time
# series. The analysis tool (perfdiag) reads hardware.txt to choose disk
# thresholds by media class.
#
# Needs only /proc and /sys. Root is optional: dmsetup, lvs, multipath and
# nvme output is skipped without it.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_VERSION="2.0.0"
ORIGINAL_ARGS=("$@")

# Test hooks: the unit tests point these at a fake sysfs tree and mountinfo.
# Not user options.
SYSFS="${HW_INVENTORY_SYSFS:-/sys}"
MOUNTINFO="${HW_INVENTORY_MOUNTINFO:-/proc/self/mountinfo}"

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
# Usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [-o outdir] [--imds] [-h] [-V]

  -o, --outdir   Base output directory. Default: /var/log/perf-diag
                 (~/perf-diag when the default is not writable)
      --imds     Ask the cloud instance metadata service for the instance
                 type (AWS IMDSv2, Azure). Off by default: this script makes
                 no network calls unless asked
  -h, --help     Show this help and exit
  -V, --version  Show the script name and version and exit

Writes <outdir>/<host>_hw_<timestamp>/ with manifest.txt, hardware.txt (the
file perfdiag reads) and raw/ (command output for people).

Runs in about a second and samples nothing. Root is optional. Without root,
dmsetup, lvs, multipath and nvme output is skipped with a NOTE: and the
device-mapper target of each dm device is left empty.

Examples:
  ${SCRIPT_NAME}
  sudo ${SCRIPT_NAME} --imds -o /var/log/perf-diag
EOF
}

# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
OUTDIR_BASE=""
USE_IMDS="no"
OPT_SHIFT=1

while [[ $# -gt 0 ]]; do
    arg="$1"
    has_next="no"
    if [[ $# -ge 2 ]]; then
        has_next="yes"
    fi
    case "$arg" in
        -o|--outdir|--outdir=*) take_value "$arg" "${2-}" "$has_next"; OUTDIR_BASE="$OPT_VALUE" ;;
        --imds)       USE_IMDS="yes"; OPT_SHIFT=1 ;;
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

IS_ROOT="no"
if (( EUID == 0 )); then
    IS_ROOT="yes"
fi

# ---------------------------------------------------------------------------
# Run directory
# ---------------------------------------------------------------------------
TS="$(date +%Y%m%d_%H%M%S)"
HOST_SHORT="$(short_hostname)"

if [[ -z "$OUTDIR_BASE" ]]; then
    OUTDIR_BASE="/var/log/perf-diag"
    if ! mkdir -p "$OUTDIR_BASE" 2>/dev/null || [[ ! -w "$OUTDIR_BASE" ]]; then
        OUTDIR_BASE="${HOME}/perf-diag"
        note "/var/log/perf-diag is not writable; using ${OUTDIR_BASE}"
    fi
fi

OUTDIR="${OUTDIR_BASE}/${HOST_SHORT}_hw_${TS}"
RAW_DIR="${OUTDIR}/raw"
HW_FILE="${OUTDIR}/hardware.txt"

mkdir -p "$RAW_DIR" || die "cannot create ${RAW_DIR}"
: > "$HW_FILE"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# First line of a file with surrounding blanks removed. Empty when the file is
# missing or unreadable. Some sysfs attributes return an I/O error, so errors
# from the redirection are discarded.
sys_read() {
    local value=""
    { IFS= read -r value < "$1"; } 2>/dev/null || true
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

# Appends key=value to hardware.txt. Newlines in the value become spaces.
hw_put() {
    local key="$1" value="${2-}"
    value="${value//$'\n'/ }"
    value="${value//$'\r'/ }"
    printf '%s=%s\n' "$key" "$value" >> "$HW_FILE"
}

# Number of entries in a directory. Prints 0 when it does not exist.
count_entries() {
    local entry count=0
    for entry in "$1"/*; do
        if [[ -e "$entry" ]]; then
            count=$(( count + 1 ))
        fi
    done
    echo "$count"
}

# Names of the entries in a directory, comma separated. Empty when none.
list_entries() {
    local entry names=""
    for entry in "$1"/*; do
        if [[ -e "$entry" ]]; then
            names="${names:+${names},}${entry##*/}"
        fi
    done
    echo "$names"
}

# Runs a command and keeps its output in raw/. The file is removed when the
# command is missing, fails or prints nothing, so raw/ never holds empty or
# partial files.
save_raw() {
    local name="$1"
    shift
    if ! command -v "$1" >/dev/null 2>&1; then
        return 1
    fi
    if "$@" > "${RAW_DIR}/${name}" 2>/dev/null && [[ -s "${RAW_DIR}/${name}" ]]; then
        return 0
    fi
    rm -f "${RAW_DIR}/${name}"
    return 1
}

copy_raw() {
    local name="$1" source="$2"
    if [[ -r "$source" ]]; then
        cp "$source" "${RAW_DIR}/${name}" 2>/dev/null || rm -f "${RAW_DIR}/${name}"
    fi
}

# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------
# Topology comes from sysfs, not lscpu -J: JSON output needs util-linux 2.30 and
# RHEL 7 / Ubuntu 14.04 ship older versions.
collect_cpu() {
    local cpu_dir pkg core_id logical model flags sockets physical_cores
    local governor virt_flag="" hypervisor="no" online
    local -A seen_packages=()
    local -A seen_cores=()

    logical="$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || true)"
    if [[ -z "$logical" || "$logical" == "0" ]]; then
        logical="$(nproc 2>/dev/null || echo 1)"
    fi

    for cpu_dir in "${SYSFS}"/devices/system/cpu/cpu[0-9]*; do
        if [[ ! -r "${cpu_dir}/topology/core_id" ]]; then
            continue
        fi
        pkg="$(sys_read "${cpu_dir}/topology/physical_package_id")"
        core_id="$(sys_read "${cpu_dir}/topology/core_id")"
        seen_packages["$pkg"]=1
        seen_cores["${pkg}:${core_id}"]=1
    done
    sockets="${#seen_packages[@]}"
    physical_cores="${#seen_cores[@]}"
    if (( physical_cores == 0 )); then
        note "CPU topology not in sysfs; assuming ${logical} single-thread cores in one socket"
        sockets=1
        physical_cores="$logical"
    fi

    model="$(awk -F: '/^model name|^Model name|^cpu model|^Hardware/ {sub(/^[ \t]+/, "", $2); print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    if [[ -z "$model" ]] && command -v lscpu >/dev/null 2>&1; then
        model="$(LC_ALL=C lscpu 2>/dev/null | awk -F: '/^Model name/ {sub(/^[ \t]+/, "", $2); print $2; exit}' || true)"
    fi

    flags="$(awk -F: '/^flags|^Features/ {print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    case " ${flags} " in
        *" hypervisor "*) hypervisor="yes" ;;
    esac
    case " ${flags} " in
        *" vmx "*) virt_flag="vmx" ;;
        *" svm "*) virt_flag="svm" ;;
    esac

    governor="$(sys_read "${SYSFS}/devices/system/cpu/cpu0/cpufreq/scaling_governor")"
    online="$(sys_read "${SYSFS}/devices/system/cpu/online")"

    hw_put cpu.model "$model"
    hw_put cpu.logical "$logical"
    hw_put cpu.sockets "$sockets"
    hw_put cpu.physical_cores "$physical_cores"
    hw_put cpu.cores_per_socket "$(( physical_cores / sockets ))"
    hw_put cpu.threads_per_core "$(( logical / physical_cores ))"
    hw_put cpu.online "$online"
    hw_put cpu.governor "$governor"
    hw_put cpu.hypervisor "$hypervisor"
    hw_put cpu.flags_virt "$virt_flag"

    if ! save_raw lscpu.txt lscpu; then
        note "lscpu not available; raw/lscpu.txt not written"
    fi
}

# ---------------------------------------------------------------------------
# NUMA, memory, swap, vm settings
# ---------------------------------------------------------------------------
collect_numa() {
    local node_dir node_id node_count=0 mem_kb
    for node_dir in "${SYSFS}"/devices/system/node/node[0-9]*; do
        if [[ ! -d "$node_dir" ]]; then
            continue
        fi
        node_count=$(( node_count + 1 ))
    done
    if (( node_count == 0 )); then
        return 0
    fi
    hw_put numa.nodes "$node_count"
    for node_dir in "${SYSFS}"/devices/system/node/node[0-9]*; do
        if [[ ! -d "$node_dir" ]]; then
            continue
        fi
        node_id="${node_dir##*node}"
        mem_kb="$(awk '/MemTotal:/ {print $4}' "${node_dir}/meminfo" 2>/dev/null || true)"
        hw_put "numa.node${node_id}.cpus" "$(sys_read "${node_dir}/cpulist")"
        hw_put "numa.node${node_id}.mem_kb" "$mem_kb"
    done
}

# The active choice is the word in [brackets], e.g. "always [madvise] never".
bracketed_choice() {
    local text="$1"
    if [[ "$text" =~ \[([^]]+)\] ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "$text"
    fi
}

collect_memory() {
    local thp_dir="${SYSFS}/kernel/mm/transparent_hugepage"
    hw_put mem.total_kb "$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
    hw_put mem.hugepages_total "$(awk '/^HugePages_Total:/ {print $2}' /proc/meminfo)"
    hw_put mem.hugepage_kb "$(awk '/^Hugepagesize:/ {print $2}' /proc/meminfo)"
    hw_put mem.thp_enabled "$(bracketed_choice "$(sys_read "${thp_dir}/enabled")")"
    hw_put mem.thp_defrag "$(bracketed_choice "$(sys_read "${thp_dir}/defrag")")"
}

collect_swap() {
    local index=0 device type size priority
    # /proc/swaps: Filename Type Size Used Priority (header on the first line)
    while read -r device type size _ priority; do
        index=$(( index + 1 ))
        hw_put "swap.${index}.device" "$device"
        hw_put "swap.${index}.type" "$type"
        hw_put "swap.${index}.size_kb" "$size"
        hw_put "swap.${index}.priority" "$priority"
    done < <(tail -n +2 /proc/swaps 2>/dev/null || true)
    copy_raw swaps.txt /proc/swaps
}

collect_vm() {
    local key
    for key in swappiness dirty_ratio dirty_background_ratio dirty_bytes \
               dirty_background_bytes overcommit_memory min_free_kbytes; do
        hw_put "vm.${key}" "$(sys_read "/proc/sys/vm/${key}")"
    done

    local path
    {
        for path in /proc/sys/vm/*; do
            if [[ -f "$path" && -r "$path" ]]; then
                echo "vm.${path##*/} = $(sys_read "$path")"
            fi
        done
    } > "${RAW_DIR}/sysctl_vm.txt" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Disks
# ---------------------------------------------------------------------------
# Whole-disk devices only. /sys/block never lists partitions.
list_disks() {
    local dev name
    for dev in "${SYSFS}"/block/*; do
        [[ -e "$dev" ]] || continue
        name="${dev##*/}"
        case "$name" in
            loop*|ram*|zram*|sr*|fd*) continue ;;
        esac
        echo "$name"
    done
}

# dm-N and md devices can sit on partitions (a LUKS volume on vda3). The
# inventory records the whole disk so every name in slaves= is also a disk.<name>.
whole_disk_of() {
    local name="$1" resolved
    if [[ -e "${SYSFS}/class/block/${name}/partition" ]]; then
        resolved="$(readlink -f "${SYSFS}/class/block/${name}" 2>/dev/null || true)"
        resolved="${resolved%/*}"
        echo "${resolved##*/}"
    else
        echo "$name"
    fi
}

# slaves, translated to whole disks, deduplicated, comma separated.
slave_disks() {
    local name="$1" entry disk result=""
    for entry in "${SYSFS}/block/${name}/slaves"/*; do
        if [[ ! -e "$entry" ]]; then
            continue
        fi
        disk="$(whole_disk_of "${entry##*/}")"
        case ",${result}," in
            *",${disk},"*) ;;
            *) result="${result:+${result},}${disk}" ;;
        esac
    done
    echo "$result"
}

# First of nvme, virtio, usb, ata, host (SCSI) found in the resolved device path.
transport_of() {
    local resolved="$1"
    case "$resolved" in
        *"/nvme/"*|*"/nvme"[0-9]*) echo "nvme" ;;
        *"/virtio"*) echo "virtio" ;;
        *"/usb"*) echo "usb" ;;
        *"/ata"[0-9]*) echo "ata" ;;
        *"/host"[0-9]*) echo "host" ;;
        *) echo "" ;;
    esac
}

RAID_HBA_DRIVERS="megaraid_sas hpsa smartpqi aacraid mpi3mr"
RAID_MODEL_PATTERN='PERC|LOGICAL VOLUME|MR9|RAID'

# Prints "suspected" or "no". mpt3sas is normally a plain HBA and is not listed.
detect_hw_raid() {
    local resolved="$1" model="$2" host_number proc_name driver
    if [[ "$resolved" =~ /host([0-9]+)/ ]]; then
        host_number="${BASH_REMATCH[1]}"
        proc_name="$(sys_read "${SYSFS}/class/scsi_host/host${host_number}/proc_name")"
        for driver in $RAID_HBA_DRIVERS; do
            if [[ "$proc_name" == "$driver" ]]; then
                echo "suspected"
                return 0
            fi
        done
    fi
    if [[ "${model^^}" =~ $RAID_MODEL_PATTERN ]]; then
        echo "suspected"
        return 0
    fi
    echo "no"
}

# First rule that matches wins. See the user guide for the table.
classify_disk() {
    local name="$1" model="$2" vendor="$3" rotational="$4" hw_raid="$5"
    case "$model" in
        *"Amazon Elastic Block Store"*)          echo "cloud-ebs"; return 0 ;;
        *"Amazon EC2 NVMe Instance Storage"*)    echo "nvme"; return 0 ;;
        *Microsoft*|*MSFT*)                      echo "cloud-azure"; return 0 ;;
    esac
    if [[ "${vendor,,}" == "msft" ]]; then
        echo "cloud-azure"
        return 0
    fi
    case "$name" in
        dm-*|md*)   echo "stacked"; return 0 ;;
        nvme*)      echo "nvme"; return 0 ;;
        vd*|xvd*)   echo "virtual"; return 0 ;;
    esac
    if [[ "$hw_raid" == "suspected" ]]; then
        echo "hw-raid"
        return 0
    fi
    case "$rotational" in
        1) echo "hdd" ;;
        0) echo "ssd" ;;
        *) echo "unknown" ;;
    esac
}

# Per-device values gathered in the first pass, keyed by sysfs name.
declare -A D_CLASS=()
declare -A D_SLAVES=()
declare -A D_MD_LEVEL=()
declare -A D_MD_DISKS=()
declare -A D_DM_TARGET=()
declare -A D_DM_STRIPES=()
declare -A D_MODEL=()
declare -A D_VENDOR=()
declare -A D_ROT=()
declare -A D_RESOLVED=()
declare -A D_HWRAID=()
DMSETUP_TABLE_OK="no"

# Target type and stripe count of the first line of `dmsetup table <name>`.
# Line format: <start> <length> <target> <args...>. For "striped" the first
# argument is the stripe count.
load_dm_target() {
    local name="$1" mapper_name line target stripes
    mapper_name="$(sys_read "${SYSFS}/block/${name}/dm/name")"
    if [[ -z "$mapper_name" || "$DMSETUP_TABLE_OK" != "yes" ]]; then
        return 0
    fi
    line="$(dmsetup table "$mapper_name" 2>/dev/null | head -n 1 || true)"
    if [[ -z "$line" ]]; then
        return 0
    fi
    read -r _ _ target stripes _ <<< "$line"
    D_DM_TARGET["$name"]="$target"
    if [[ "$target" == "striped" ]]; then
        D_DM_STRIPES["$name"]="$stripes"
    fi
}

# Parallelism: how many independent devices can service IO at once. Recursive,
# because dm linear/crypt/multipath inherit from their slaves.
# Prints a number, or nothing when it is unknown or does not apply.
disk_parallelism() {
    local name="$1" depth="${2:-0}" level disks target slave value
    if (( depth > 8 )); then
        return 0
    fi
    case "${D_CLASS[$name]:-}" in
        hdd)
            echo 1
            ;;
        stacked)
            level="${D_MD_LEVEL[$name]:-}"
            disks="${D_MD_DISKS[$name]:-}"
            target="${D_DM_TARGET[$name]:-}"
            if [[ -n "$level" && "$disks" =~ ^[0-9]+$ ]]; then
                case "$level" in
                    raid0|raid1) echo "$disks"; return 0 ;;
                    raid10)      echo $(( disks / 2 )); return 0 ;;
                    raid4|raid5) echo $(( disks - 1 )); return 0 ;;
                    raid6)       echo $(( disks - 2 )); return 0 ;;
                esac
            elif [[ "$target" == "striped" && -n "${D_DM_STRIPES[$name]:-}" ]]; then
                echo "${D_DM_STRIPES[$name]}"
                return 0
            fi
            # linear, crypt, multipath, unknown dm target (no root), md linear:
            # take the first slave that has a value.
            local IFS=","
            for slave in ${D_SLAVES[$name]:-}; do
                value="$(disk_parallelism "$slave" $(( depth + 1 )))"
                if [[ -n "$value" ]]; then
                    echo "$value"
                    return 0
                fi
            done
            ;;
        *)
            ;;
    esac
}

collect_disks() {
    local name sysname kname resolved model vendor rotational hw_raid
    local class dm_display
    local -a names=()

    if [[ "$IS_ROOT" == "yes" ]] && command -v dmsetup >/dev/null 2>&1; then
        DMSETUP_TABLE_OK="yes"
    fi

    # Pass 1: read everything that does not depend on another device.
    while read -r sysname; do
        [[ -n "$sysname" ]] || continue
        names+=("$sysname")
        resolved="$(readlink -f "${SYSFS}/block/${sysname}/device" 2>/dev/null || true)"
        model="$(sys_read "${SYSFS}/block/${sysname}/device/model")"
        vendor="$(sys_read "${SYSFS}/block/${sysname}/device/vendor")"
        if [[ -z "$model" ]]; then
            # NVMe namespaces: the model lives on the controller.
            model="$(sys_read "${SYSFS}/block/${sysname}/device/../model")"
        fi
        rotational="$(sys_read "${SYSFS}/block/${sysname}/queue/rotational")"
        case "$sysname" in
            dm-*|md*) hw_raid="no" ;;
            *)        hw_raid="$(detect_hw_raid "$resolved" "$model")" ;;
        esac
        class="$(classify_disk "$sysname" "$model" "$vendor" "$rotational" "$hw_raid")"

        D_MODEL["$sysname"]="$model"
        D_VENDOR["$sysname"]="$vendor"
        D_ROT["$sysname"]="$rotational"
        D_RESOLVED["$sysname"]="$resolved"
        D_HWRAID["$sysname"]="$hw_raid"
        D_CLASS["$sysname"]="$class"
        D_SLAVES["$sysname"]="$(slave_disks "$sysname")"
        D_MD_LEVEL["$sysname"]="$(sys_read "${SYSFS}/block/${sysname}/md/level")"
        D_MD_DISKS["$sysname"]="$(sys_read "${SYSFS}/block/${sysname}/md/raid_disks")"
        if [[ "$sysname" == dm-* ]]; then
            load_dm_target "$sysname"
        fi
    done < <(list_disks)

    if (( ${#names[@]} == 0 )); then
        note "no block devices found in /sys/block"
        return 0
    fi

    # Pass 2: write, now that every device's class and slaves are known.
    for sysname in "${names[@]}"; do
        kname="${sysname//!//}"
        dm_display="$(sys_read "${SYSFS}/block/${sysname}/dm/name")"
        local prefix="disk.${kname}"
        local size_sectors parallelism chunk_bytes queue_dir="${SYSFS}/block/${sysname}/queue"

        size_sectors="$(sys_read "${SYSFS}/block/${sysname}/size")"
        chunk_bytes="$(sys_read "${SYSFS}/block/${sysname}/md/chunk_size")"
        parallelism="$(disk_parallelism "$sysname")"
        if [[ "${D_CLASS[$sysname]}" == "hw-raid" ]]; then
            note "${kname}: hardware RAID suspected, parallelism unknown; pass --spindles ${kname}=N to perfdiag"
        fi

        hw_put "${prefix}.display" "$dm_display"
        if [[ "$size_sectors" =~ ^[0-9]+$ ]]; then
            hw_put "${prefix}.size_bytes" "$(( size_sectors * 512 ))"
        else
            hw_put "${prefix}.size_bytes" ""
        fi
        hw_put "${prefix}.rotational" "${D_ROT[$sysname]}"
        hw_put "${prefix}.model" "${D_MODEL[$sysname]}"
        hw_put "${prefix}.vendor" "${D_VENDOR[$sysname]}"
        hw_put "${prefix}.class" "${D_CLASS[$sysname]}"
        hw_put "${prefix}.scheduler" "$(sys_read "${queue_dir}/scheduler")"
        hw_put "${prefix}.nr_requests" "$(sys_read "${queue_dir}/nr_requests")"
        hw_put "${prefix}.read_ahead_kb" "$(sys_read "${queue_dir}/read_ahead_kb")"
        hw_put "${prefix}.max_sectors_kb" "$(sys_read "${queue_dir}/max_sectors_kb")"
        hw_put "${prefix}.hw_queues" "$(count_entries "${SYSFS}/block/${sysname}/mq")"
        hw_put "${prefix}.logical_block" "$(sys_read "${queue_dir}/logical_block_size")"
        hw_put "${prefix}.physical_block" "$(sys_read "${queue_dir}/physical_block_size")"
        hw_put "${prefix}.slaves" "${D_SLAVES[$sysname]//!//}"
        hw_put "${prefix}.holders" "$(list_entries "${SYSFS}/block/${sysname}/holders")"
        hw_put "${prefix}.md_level" "${D_MD_LEVEL[$sysname]}"
        hw_put "${prefix}.md_raid_disks" "${D_MD_DISKS[$sysname]}"
        if [[ "$chunk_bytes" =~ ^[0-9]+$ ]]; then
            hw_put "${prefix}.md_chunk_kb" "$(( chunk_bytes / 1024 ))"
        else
            hw_put "${prefix}.md_chunk_kb" ""
        fi
        hw_put "${prefix}.dm_target" "${D_DM_TARGET[$sysname]:-}"
        hw_put "${prefix}.dm_stripes" "${D_DM_STRIPES[$sysname]:-}"
        hw_put "${prefix}.transport_hint" "$(transport_of "${D_RESOLVED[$sysname]}")"
        hw_put "${prefix}.hw_raid" "${D_HWRAID[$sysname]}"
        hw_put "${prefix}.parallelism" "$parallelism"
    done
}

# Device-mapper, LVM, multipath and NVMe command output. All need root.
collect_storage_raw() {
    local skipped=""

    copy_raw mdstat.txt /proc/mdstat

    if [[ "$IS_ROOT" == "yes" ]]; then
        save_raw dmsetup_table.txt dmsetup table || true
        save_raw lvs.txt lvs -o +devices || true
        save_raw multipath.txt multipath -ll || true
        save_raw nvme_list.txt nvme list || true
    else
        skipped="dmsetup, lvs, multipath, nvme"
        note "not root: ${skipped} output skipped. Re-run with sudo for device-mapper targets and LVM/multipath detail"
    fi

    if ! save_raw lsblk.txt lsblk -o NAME,KNAME,TYPE,SIZE,ROTA,MODEL,TRAN,FSTYPE,MOUNTPOINT; then
        if ! save_raw lsblk.txt lsblk; then
            note "lsblk not available; raw/lsblk.txt not written"
        fi
    fi
    save_raw lspci_storage.txt lspci -nn || true
    if [[ -f "${RAW_DIR}/lspci_storage.txt" ]]; then
        grep -iE 'raid|sata|scsi|nvme|storage|fibre' "${RAW_DIR}/lspci_storage.txt" > "${RAW_DIR}/lspci_storage.filtered" 2>/dev/null || true
        mv "${RAW_DIR}/lspci_storage.filtered" "${RAW_DIR}/lspci_storage.txt"
    fi

    local host
    {
        for host in "${SYSFS}"/class/scsi_host/host*; do
            if [[ -d "$host" ]]; then
                echo "${host##*/} proc_name=$(sys_read "${host}/proc_name")"
            fi
        done
    } > "${RAW_DIR}/scsi_hosts.txt" 2>/dev/null || true

    # Vendor RAID CLIs are recorded as present or absent, never run.
    local tool
    {
        for tool in storcli storcli64 perccli perccli64 ssacli hpssacli; do
            if command -v "$tool" >/dev/null 2>&1; then
                echo "${tool}: present"
            else
                echo "${tool}: absent"
            fi
        done
    } > "${RAW_DIR}/raid_tools.txt"
}

# ---------------------------------------------------------------------------
# Filesystems
# ---------------------------------------------------------------------------
PSEUDO_FS=" proc sysfs tmpfs devtmpfs cgroup cgroup2 overlay squashfs nsfs tracefs debugfs securityfs pstore bpf autofs mqueue hugetlbfs configfs fusectl binfmt_misc devpts ramfs rpc_pipefs efivarfs selinuxfs "

# Block device a filesystem sits on: major:minor (mountinfo field 3) resolved
# through /sys/dev/block. A partition is reported as its whole disk.
kname_for_devno() {
    local devno="$1" resolved name
    resolved="$(readlink -f "${SYSFS}/dev/block/${devno}" 2>/dev/null || true)"
    if [[ -z "$resolved" || ! -d "$resolved" ]]; then
        return 0
    fi
    name="${resolved##*/}"
    if [[ -e "${resolved}/partition" ]]; then
        resolved="${resolved%/*}"
        name="${resolved##*/}"
    fi
    echo "${name//!//}"
}

collect_filesystems() {
    local index=0 line devno mount_point fstype source
    local -A seen=()
    local before after mount_point_clean kname

    copy_raw mountinfo.txt "$MOUNTINFO"
    if [[ ! -r "$MOUNTINFO" ]]; then
        note "${MOUNTINFO} not readable; no filesystem map"
        return 0
    fi

    while IFS= read -r line; do
        # Everything before " - " is fixed fields plus optional fields; after it
        # come fstype, source and super options.
        before="${line%% - *}"
        after="${line#* - }"
        read -r _ _ devno _ mount_point _ <<< "$before"
        read -r fstype source _ <<< "$after"

        case "$PSEUDO_FS" in
            *" ${fstype} "*) continue ;;
        esac
        case "$fstype" in
            cgroup*|fuse.*portal*) continue ;;
        esac

        mount_point_clean="$(printf '%b' "$mount_point")"
        if [[ -n "${seen["${mount_point_clean}|${source}"]:-}" ]]; then
            continue
        fi
        seen["${mount_point_clean}|${source}"]=1

        kname="$(kname_for_devno "$devno")"
        index=$(( index + 1 ))
        hw_put "fs.${index}.mountpoint" "$mount_point_clean"
        hw_put "fs.${index}.source" "$source"
        hw_put "fs.${index}.fstype" "$fstype"
        hw_put "fs.${index}.kname" "$kname"
    done < "$MOUNTINFO"
}

# ---------------------------------------------------------------------------
# Cloud
# ---------------------------------------------------------------------------
# GET with a 2 second timeout, curl or wget. Extra arguments are header lines.
http_request() {
    local method="$1" url="$2"
    shift 2
    local header
    if command -v curl >/dev/null 2>&1; then
        local -a args=(-fsS -m 2 -X "$method")
        for header in "$@"; do
            args+=(-H "$header")
        done
        curl "${args[@]}" "$url"
    elif command -v wget >/dev/null 2>&1; then
        local -a args=(-q -O- -T 2 -t 1 --method="$method")
        if [[ "$method" == "PUT" ]]; then
            args+=(--body-data=)
        fi
        for header in "$@"; do
            args+=(--header="$header")
        done
        wget "${args[@]}" "$url"
    else
        return 127
    fi
}

# Only characters that appear in instance and VM size names are accepted.
valid_instance_type() {
    [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]
}

query_imds() {
    local provider="$1" token instance_type=""
    case "$provider" in
        aws)
            if token="$(http_request PUT "http://169.254.169.254/latest/api/token" \
                    "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null)"; then
                instance_type="$(http_request GET "http://169.254.169.254/latest/meta-data/instance-type" \
                    "X-aws-ec2-metadata-token: ${token}" 2>/dev/null || true)"
            fi
            ;;
        azure)
            instance_type="$(http_request GET \
                "http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text" \
                "Metadata: true" 2>/dev/null || true)"
            ;;
        *)
            note "--imds given but the cloud provider is '${provider:-unknown}'; no lookup done"
            return 0
            ;;
    esac

    if [[ -n "$instance_type" ]] && valid_instance_type "$instance_type"; then
        echo "$instance_type"
    else
        note "instance metadata service gave no usable instance type (not reachable, or IMDS blocked)"
    fi
}

collect_cloud() {
    local dmi="${SYSFS}/class/dmi/id" sys_vendor product_name asset_tag provider="" instance_type=""

    sys_vendor="$(sys_read "${dmi}/sys_vendor")"
    product_name="$(sys_read "${dmi}/product_name")"
    asset_tag="$(sys_read "${dmi}/board_asset_tag")"

    case "$sys_vendor" in
        "Amazon EC2") provider="aws" ;;
        "Microsoft Corporation")
            if [[ "$product_name" == "Virtual Machine" ]]; then
                provider="azure"
            fi
            ;;
        "Google") provider="gcp" ;;
    esac

    if [[ "$USE_IMDS" == "yes" ]]; then
        instance_type="$(query_imds "$provider")"
    fi

    hw_put cloud.provider "$provider"
    hw_put cloud.sys_vendor "$sys_vendor"
    hw_put cloud.product_name "$product_name"
    hw_put cloud.instance_type "$instance_type"
    hw_put cloud.asset_tag "$asset_tag"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
manifest_start "${OUTDIR}/manifest.txt" 0 1 0
manifest_add root "$IS_ROOT"
manifest_add imds "$USE_IMDS"

collect_cpu
collect_numa
collect_memory
collect_swap
collect_vm
collect_storage_raw
collect_disks
collect_filesystems
collect_cloud

manifest_add notes "$NOTES_TEXT"
manifest_finish

# Run as root through sudo: hand the directory to the invoking user, as the
# collectors do, so the files can be copied off without sudo.
if [[ "$IS_ROOT" == "yes" && -n "${SUDO_UID:-}" ]]; then
    chown -R "${SUDO_UID}:${SUDO_GID:-$SUDO_UID}" "$OUTDIR" 2>/dev/null || true
fi

echo "Output directory: ${OUTDIR}"
echo "  hardware.txt:  ${HW_FILE}"
echo "  manifest:      ${OUTDIR}/manifest.txt"
echo "  raw output:    ${RAW_DIR}/"
