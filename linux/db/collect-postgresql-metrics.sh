#!/usr/bin/env bash
# collect-postgresql-metrics.sh
# Loops pg_stat_bgwriter / pg_stat_database / pg_stat_activity at a fixed
# interval for a fixed duration, matching the OS-level sadc/pidstat window.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./db-collect-lib.sh
source "${SCRIPT_DIR}/db-collect-lib.sh"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-d duration] [-i interval] [-h host] [-p port] [-U user] [-w dbname] [-o outdir]

  -d  Collection duration, e.g. 2h, 30m, 1d     (prompted if omitted)
  -i  Sample interval, e.g. 15s, 1m             (prompted if omitted)
  -h  Postgres host          (default: localhost)
  -p  Postgres port          (default: 5432)
  -U  Postgres user          (default: current \$USER)
  -w  Database name          (default: postgres)
  -o  Output base directory  (default: /var/log/sa/diag)

Authentication: uses standard libpq env vars / ~/.pgpass. Set PGPASSWORD
or configure .pgpass before running if the connection needs a password.
EOF
}

DURATION="" INTERVAL="" PGHOST_="localhost" PGPORT_="5432" PGUSER_="${USER}" PGDB_="postgres" OUTBASE="/var/log/sa/diag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DURATION="$2"; shift 2 ;;
    -i) INTERVAL="$2"; shift 2 ;;
    -h) PGHOST_="$2"; shift 2 ;;
    -p) PGPORT_="$2"; shift 2 ;;
    -U) PGUSER_="$2"; shift 2 ;;
    -w) PGDB_="$2"; shift 2 ;;
    -o) OUTBASE="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if ! command -v psql >/dev/null 2>&1; then
  FAMILY="$(distro_family)"
  echo "ERROR: 'psql' not found." >&2
  echo "Install the PostgreSQL client, then re-run this script:" >&2
  case "$FAMILY" in
    debian) echo "  sudo apt update && sudo apt install -y postgresql-client" >&2 ;;
    rhel)   echo "  sudo dnf install -y postgresql   # package may be postgresqlNN on RHEL/Rocky/Alma, e.g. postgresql16" >&2 ;;
    suse)   echo "  sudo zypper install -y postgresql" >&2 ;;
    *)      echo "  Install the postgresql client package for your distro." >&2 ;;
  esac
  exit 1
fi

if ! PGCONNECT_TIMEOUT=5 psql -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -c '\q' 2>/tmp/pg_conn_test.err; then
  echo "ERROR: cannot connect to PostgreSQL at ${PGHOST_}:${PGPORT_}/${PGDB_} as ${PGUSER_}." >&2
  cat /tmp/pg_conn_test.err >&2
  echo "Check the host/port/user/database, and that PGPASSWORD or ~/.pgpass is set correctly." >&2
  rm -f /tmp/pg_conn_test.err
  exit 1
fi
rm -f /tmp/pg_conn_test.err

prompt_duration_interval

OUTDIR=$(make_outdir "$OUTBASE" "postgresql")
OUTFILE="${OUTDIR}/pg_stats.csv"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION} (${DURATION_SECS}s)  Interval: ${INTERVAL} (${INTERVAL_SECS}s)  Samples: ${SAMPLE_COUNT}"

echo "ts,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,bgwriter_checkpoints_timed,bgwriter_checkpoints_req,bgwriter_buffers_checkpoint,bgwriter_buffers_clean,active_connections,idle_connections,waiting_connections" > "$OUTFILE"

QUERY="
SELECT
  now(),
  d.xact_commit, d.xact_rollback, d.blks_read, d.blks_hit,
  d.tup_returned, d.tup_fetched, d.tup_inserted, d.tup_updated, d.tup_deleted,
  d.temp_files, d.temp_bytes, d.deadlocks,
  b.checkpoints_timed, b.checkpoints_req, b.buffers_checkpoint, b.buffers_clean,
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'active'),
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle'),
  (SELECT count(*) FROM pg_stat_activity WHERE wait_event IS NOT NULL)
FROM pg_stat_database d, pg_stat_bgwriter b
WHERE d.datname = current_database();
"

for (( i=0; i<SAMPLE_COUNT; i++ )); do
  psql -h "$PGHOST_" -p "$PGPORT_" -U "$PGUSER_" -d "$PGDB_" -At -F',' -c "$QUERY" >> "$OUTFILE" 2>>"${OUTDIR}/errors.log" || true
  sleep "$INTERVAL_SECS"
done

echo "Done. $(wc -l < "$OUTFILE") rows written to ${OUTFILE}"
