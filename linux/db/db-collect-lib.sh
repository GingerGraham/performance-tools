#!/usr/bin/env bash
# db-collect-lib.sh — shared helpers for DB metric collectors.
# Source this from each per-engine script; it is not meant to be run directly.

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
      [[ "$allow_hours_days" == "yes" ]] || { echo "ERROR: unit '${unit}' not valid here" >&2; return 1; }
      echo $((num * 3600)) ;;
    d|day|days)
      [[ "$allow_hours_days" == "yes" ]] || { echo "ERROR: unit '${unit}' not valid here" >&2; return 1; }
      echo $((num * 86400)) ;;
    *) echo "ERROR: unrecognised unit '${unit}' in '${val}'" >&2; return 1 ;;
  esac
}

# Sets DURATION_SECS, INTERVAL_SECS, SAMPLE_COUNT as globals.
# Expects DURATION and INTERVAL to already be set (possibly empty) by the caller.
prompt_duration_interval() {
  if [[ -z "${DURATION:-}" ]]; then
    read -rp "Collection duration (e.g. 30m, 2h, 1d): " DURATION
  fi
  if [[ -z "${INTERVAL:-}" ]]; then
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
}

# $1 = base dir, $2 = engine tag. Prints the created dir.
make_outdir() {
  local base="$1" tag="$2"
  local ts dir
  ts="$(date +%Y%m%d_%H%M%S)"
  dir="${base}/${tag}_${ts}"
  mkdir -p "$dir"
  echo "$dir"
}

distro_family() {
  # shellcheck disable=SC1091
  source /etc/os-release 2>/dev/null || { echo "unknown"; return; }
  case "${ID:-}" in
    ubuntu|debian) echo "debian" ;;
    rhel|rocky|almalinux|ol|centos|fedora) echo "rhel" ;;
    sles|sled|opensuse*|suse) echo "suse" ;;
    *)
      case "${ID_LIKE:-}" in
        *debian*) echo "debian" ;;
        *rhel*|*fedora*) echo "rhel" ;;
        *suse*) echo "suse" ;;
        *) echo "unknown" ;;
      esac
      ;;
  esac
}
