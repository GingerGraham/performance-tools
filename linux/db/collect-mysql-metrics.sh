#!/usr/bin/env bash
# collect-mysql-metrics.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./db-collect-lib.sh
source "${SCRIPT_DIR}/db-collect-lib.sh"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-d duration] [-i interval] [-h host] [-P port] [-u user] [-o outdir]

  -d  Collection duration, e.g. 2h, 30m, 1d
  -i  Sample interval, e.g. 15s, 1m
  -h  MySQL/MariaDB host    (default: localhost)
  -P  Port                  (default: 3306)
  -u  User                  (default: root)
  -o  Output base directory (default: /var/log/sa/diag)

Authentication: uses MYSQL_PWD env var, or ~/.my.cnf. Set one of these
before running if the connection needs a password.
EOF
}

DURATION="" INTERVAL="" DBHOST="localhost" DBPORT="3306" DBUSER="root" OUTBASE="/var/log/sa/diag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DURATION="$2"; shift 2 ;;
    -i) INTERVAL="$2"; shift 2 ;;
    -h) DBHOST="$2"; shift 2 ;;
    -P) DBPORT="$2"; shift 2 ;;
    -u) DBUSER="$2"; shift 2 ;;
    -o) OUTBASE="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

MYSQL_BIN=""
for cand in mysql mariadb; do
  if command -v "$cand" >/dev/null 2>&1; then MYSQL_BIN="$cand"; break; fi
done

if [[ -z "$MYSQL_BIN" ]]; then
  FAMILY="$(distro_family)"
  echo "ERROR: no MySQL/MariaDB client ('mysql' or 'mariadb') found." >&2
  echo "Install a client, then re-run:" >&2
  case "$FAMILY" in
    debian) echo "  sudo apt update && sudo apt install -y mariadb-client   # or mysql-client-core-* if using Oracle's MySQL repo" >&2 ;;
    rhel)   echo "  sudo dnf install -y mysql   # MariaDB-based RHEL derivatives may ship this as 'mariadb'" >&2 ;;
    suse)   echo "  sudo zypper install -y mariadb-client" >&2 ;;
    *)      echo "  Install the mysql/mariadb client package for your distro." >&2 ;;
  esac
  exit 1
fi

if ! "$MYSQL_BIN" -h "$DBHOST" -P "$DBPORT" -u "$DBUSER" -e 'SELECT 1' >/dev/null 2>/tmp/mysql_conn_test.err; then
  echo "ERROR: cannot connect to MySQL/MariaDB at ${DBHOST}:${DBPORT} as ${DBUSER}." >&2
  cat /tmp/mysql_conn_test.err >&2
  echo "Check host/port/user, and that MYSQL_PWD or ~/.my.cnf is set correctly." >&2
  rm -f /tmp/mysql_conn_test.err
  exit 1
fi
rm -f /tmp/mysql_conn_test.err

prompt_duration_interval

OUTDIR=$(make_outdir "$OUTBASE" "mysql")
OUTFILE="${OUTDIR}/mysql_stats.csv"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION} (${DURATION_SECS}s)  Interval: ${INTERVAL} (${INTERVAL_SECS}s)  Samples: ${SAMPLE_COUNT}"

VARS="Threads_connected Threads_running Questions Slow_queries Com_select Com_insert Com_update Com_delete Innodb_buffer_pool_reads Innodb_buffer_pool_read_requests Innodb_row_lock_waits Innodb_row_lock_time Innodb_row_lock_time_avg Created_tmp_disk_tables Created_tmp_tables Open_files Open_tables Aborted_connects Aborted_clients"

{
  printf 'ts'
  for v in $VARS; do printf ',%s' "$v"; done
  printf '\n'
} > "$OUTFILE"

for (( i=0; i<SAMPLE_COUNT; i++ )); do
  ts="$(date -Iseconds)"
  row="$ts"
  status_out="$("$MYSQL_BIN" -h "$DBHOST" -P "$DBPORT" -u "$DBUSER" -N -e "SHOW GLOBAL STATUS;" 2>>"${OUTDIR}/errors.log" || true)"
  for v in $VARS; do
    val="$(printf '%s\n' "$status_out" | awk -v k="$v" '$1==k{print $2}')"
    row="${row},${val:-}"
  done
  echo "$row" >> "$OUTFILE"
  sleep "$INTERVAL_SECS"
done

echo "Done. $(wc -l < "$OUTFILE") rows written to ${OUTFILE}"
