#!/usr/bin/env bash
#
# collect-os-metrics.sh
# Cross-distro sysstat (sadc) + pidstat baseline collector.
# Supports: Debian, Ubuntu, RHEL, Rocky, AlmaLinux, Oracle Linux, Fedora, SLES.
#
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [-d|--duration <N><s|m|h|d>] [-i|--interval <N><s|m>] [-p|--process <name>[,<name>...]] [-o|--outdir <path>]

  -d, --duration   Collection period. e.g. 30m, 2h, 1d       (prompted if omitted)
  -i, --interval   Sample interval. e.g. 15s, 1m             (prompted if omitted)
  -p, --process    Comma-separated process name(s) for pidstat -C filter.
                    Omit to have pidstat report on all processes.
  -o, --outdir     Base output directory. Default: /var/log/sa/diag

Example:
  ${SCRIPT_NAME} -d 2h -i 30s -p postgres,pgbouncer
EOF
}

DURATION=""
INTERVAL=""
PROCESS_FILTER=""
OUTDIR_BASE="/var/log/sa/diag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--duration) DURATION="$2"; shift 2 ;;
    -i|--interval) INTERVAL="$2"; shift 2 ;;
    -p|--process)  PROCESS_FILTER="$2"; shift 2 ;;
    -o|--outdir)   OUTDIR_BASE="$2"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# sudo check
# ---------------------------------------------------------------------------
echo "Checking sudo privileges..."
if ! sudo -v 2>/dev/null; then
  echo "ERROR: this script requires sudo privileges to install/configure sysstat and run collection under systemd-run. Aborting." >&2
  exit 1
fi
echo "  sudo OK."

# ---------------------------------------------------------------------------
# Distro / family detection
# ---------------------------------------------------------------------------
if [[ ! -r /etc/os-release ]]; then
  echo "ERROR: cannot read /etc/os-release, unable to determine distro." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release

FAMILY="unknown"
case "${ID:-}" in
  ubuntu|debian)                         FAMILY="debian" ;;
  rhel|rocky|almalinux|ol|centos|fedora) FAMILY="rhel" ;;
  sles|sled|opensuse*|suse)              FAMILY="suse" ;;
  *)
    case "${ID_LIKE:-}" in
      *debian*)        FAMILY="debian" ;;
      *rhel*|*fedora*) FAMILY="rhel" ;;
      *suse*)          FAMILY="suse" ;;
    esac
    ;;
esac

if [[ "$FAMILY" == "unknown" ]]; then
  echo "WARNING: unrecognised distro (ID=${ID:-unset}, ID_LIKE=${ID_LIKE:-unset}). Continuing, but package/path detection may be wrong." >&2
fi

echo "Detected distro: ${PRETTY_NAME:-$ID} (family: ${FAMILY})"

# ---------------------------------------------------------------------------
# sysstat presence check
# ---------------------------------------------------------------------------
find_sadc() {
  local candidates=(/usr/lib64/sa/sadc /usr/lib/sa/sadc /usr/lib/sysstat/sadc)
  for p in "${candidates[@]}"; do
    [[ -x "$p" ]] && { echo "$p"; return 0; }
  done
  command -v sadc 2>/dev/null && return 0
  return 1
}

install_hint() {
  case "$FAMILY" in
    debian) echo "sudo apt update && sudo apt install -y sysstat" ;;
    rhel)   echo "sudo dnf install -y sysstat   # use 'yum' in place of 'dnf' on older RHEL/CentOS 7" ;;
    suse)   echo "sudo zypper install -y sysstat" ;;
    *)      echo "Install the 'sysstat' package using your distro's package manager." ;;
  esac
}

if ! SADC_PATH="$(find_sadc)"; then
  echo ""
  echo "sysstat is not installed (sadc binary not found)." >&2
  echo "Run the following, then re-run this script:" >&2
  echo "  $(install_hint)" >&2
  exit 1
fi

if ! command -v sar >/dev/null 2>&1 || ! command -v pidstat >/dev/null 2>&1; then
  echo ""
  echo "sysstat appears partially installed (sadc found at ${SADC_PATH}, but 'sar' or 'pidstat' is missing from PATH)." >&2
  echo "Reinstall with:" >&2
  echo "  $(install_hint)" >&2
  exit 1
fi

echo "sysstat OK (sadc: ${SADC_PATH}, sar: $(command -v sar), pidstat: $(command -v pidstat))"

# ---------------------------------------------------------------------------
# Duration / interval parsing
# ---------------------------------------------------------------------------
parse_to_seconds() {
  local val="$1" allow_hours_days="$2"
  local num unit
  if [[ ! "$val" =~ ^([0-9]+)([a-zA-Z]+)$ ]]; then
    echo "ERROR: cannot parse '${val}' — expected e.g. 30s, 15m, 2h, 1d" >&2
    return 1
  fi
  num="${BASH_REMATCH[1]}"
  unit="${BASH_REMATCH[2]}"
  case "$unit" in
    s|sec|secs|second|seconds) echo $((num)) ;;
    m|min|mins|minute|minutes) echo $((num * 60)) ;;
    h|hr|hrs|hour|hours)
      [[ "$allow_hours_days" == "yes" ]] || { echo "ERROR: unit '${unit}' not valid here (seconds/minutes only)" >&2; return 1; }
      echo $((num * 3600)) ;;
    d|day|days)
      [[ "$allow_hours_days" == "yes" ]] || { echo "ERROR: unit '${unit}' not valid here (seconds/minutes only)" >&2; return 1; }
      echo $((num * 86400)) ;;
    *) echo "ERROR: unrecognised unit '${unit}' in '${val}'" >&2; return 1 ;;
  esac
}

if [[ -z "$DURATION" ]]; then
  read -rp "Collection duration (e.g. 30m, 2h, 1d): " DURATION
fi
if [[ -z "$INTERVAL" ]]; then
  read -rp "Sample interval (e.g. 15s, 1m): " INTERVAL
fi

DURATION_SECS=$(parse_to_seconds "$DURATION" yes) || exit 1
INTERVAL_SECS=$(parse_to_seconds "$INTERVAL" no) || exit 1

if (( INTERVAL_SECS < 1 )); then
  echo "ERROR: interval must be at least 1 second." >&2
  exit 1
fi
if (( DURATION_SECS < INTERVAL_SECS )); then
  echo "ERROR: duration must be >= interval." >&2
  exit 1
fi

SAMPLE_COUNT=$(( DURATION_SECS / INTERVAL_SECS ))

echo ""
echo "Duration:  ${DURATION}  (${DURATION_SECS}s)"
echo "Interval:  ${INTERVAL}  (${INTERVAL_SECS}s)"
echo "Samples:   ${SAMPLE_COUNT}"

# ---------------------------------------------------------------------------
# Output location
# ---------------------------------------------------------------------------
TS="$(date +%Y%m%d_%H%M%S)"
OUTDIR="${OUTDIR_BASE}/${TS}"
sudo mkdir -p "$OUTDIR"
sudo chmod 755 "$OUTDIR"

SADC_OUT="${OUTDIR}/sa_${TS}"
PIDSTAT_OUT="${OUTDIR}/pidstat_${TS}.log"

echo ""
echo "Output directory: ${OUTDIR}"
echo "  sadc binary log:  ${SADC_OUT}"
echo "  pidstat log:       ${PIDSTAT_OUT}"

# ---------------------------------------------------------------------------
# Launch collection under transient systemd units (survives shell exit)
# ---------------------------------------------------------------------------
SADC_UNIT="perf-diag-sadc-${TS}"
PIDSTAT_UNIT="perf-diag-pidstat-${TS}"

echo ""
echo "Starting sadc collection (unit: ${SADC_UNIT})..."
sudo systemd-run --unit="${SADC_UNIT}" --description="perf-diag sadc collection ${TS}" \
  "${SADC_PATH}" -S DISK "${INTERVAL_SECS}" "${SAMPLE_COUNT}" "${SADC_OUT}"

echo "Starting pidstat collection (unit: ${PIDSTAT_UNIT})..."
if [[ -n "$PROCESS_FILTER" ]]; then
  # pidstat -C takes a regex; turn a comma list into name1|name2|...
  PIDSTAT_C="${PROCESS_FILTER//,/|}"
  sudo systemd-run --unit="${PIDSTAT_UNIT}" --description="perf-diag pidstat collection ${TS}" \
    bash -c "pidstat -u -r -d -w -C '${PIDSTAT_C}' ${INTERVAL_SECS} ${SAMPLE_COUNT} > '${PIDSTAT_OUT}'"
else
  sudo systemd-run --unit="${PIDSTAT_UNIT}" --description="perf-diag pidstat collection ${TS}" \
    bash -c "pidstat -u -r -d -w ${INTERVAL_SECS} ${SAMPLE_COUNT} > '${PIDSTAT_OUT}'"
fi

echo ""
echo "Collection running. It will stop automatically after ${DURATION} (${SAMPLE_COUNT} samples)."
echo ""
echo "Check status:"
echo "  systemctl status ${SADC_UNIT}"
echo "  systemctl status ${PIDSTAT_UNIT}"
echo ""
echo "Stop early:"
echo "  sudo systemctl stop ${SADC_UNIT} ${PIDSTAT_UNIT}"
echo ""
echo "Read back once complete:"
echo "  sar -f ${SADC_OUT} -u ALL   # CPU"
echo "  sar -f ${SADC_OUT} -r       # memory"
echo "  sar -f ${SADC_OUT} -d -p    # disk"
echo "  sar -f ${SADC_OUT} -n DEV   # network"
echo "  less ${PIDSTAT_OUT}         # per-process"
