#!/usr/bin/env bash
# collect-oracle-metrics.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./db-collect-lib.sh
source "${SCRIPT_DIR}/db-collect-lib.sh"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-d duration] [-i interval] [-c connect_string] [-o outdir]

  -d  Collection duration, e.g. 2h, 30m, 1d
  -i  Sample interval, e.g. 15s, 1m
  -c  Connect string, e.g. user/password@//host:1521/service_name
      (default: uses ORACLE_CONNECT_STRING env var if set)
  -o  Output base directory (default: /var/log/sa/diag)
EOF
}

DURATION="" INTERVAL="" CONNSTR="${ORACLE_CONNECT_STRING:-}" OUTBASE="/var/log/sa/diag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DURATION="$2"; shift 2 ;;
    -i) INTERVAL="$2"; shift 2 ;;
    -c) CONNSTR="$2"; shift 2 ;;
    -o) OUTBASE="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if ! command -v sqlplus >/dev/null 2>&1; then
  FAMILY="$(distro_family)"
  echo "ERROR: 'sqlplus' not found." >&2
  echo "Oracle Instant Client isn't in standard distro repos — install it directly from Oracle:" >&2
  echo "  https://www.oracle.com/database/technologies/instant-client/downloads.html" >&2
  case "$FAMILY" in
    rhel)   echo "  RHEL/Rocky/Alma/OL: Oracle publish an RPM (oracle-instantclient-sqlplus); Oracle Linux can also point at Oracle's public yum repo directly." >&2 ;;
    debian) echo "  Debian/Ubuntu: download the Instant Client Basic + SQL*Plus .zip packages, unzip, and set LD_LIBRARY_PATH / PATH (no native .deb from Oracle)." >&2 ;;
    suse)   echo "  SLES: use the generic Instant Client .zip (basic + sqlplus), same manual LD_LIBRARY_PATH setup as Debian." >&2 ;;
    *)      echo "  Use the generic Instant Client zip and set LD_LIBRARY_PATH accordingly." >&2 ;;
  esac
  exit 1
fi

if [[ -z "$CONNSTR" ]]; then
  echo "ERROR: no connect string supplied (-c, or ORACLE_CONNECT_STRING env var)." >&2
  exit 1
fi

if ! sqlplus -s "$CONNSTR" >/tmp/ora_conn_test.log 2>&1 <<'SQL'
SELECT 1 FROM dual;
EXIT;
SQL
then
  echo "ERROR: sqlplus invocation failed." >&2
  cat /tmp/ora_conn_test.log >&2
  rm -f /tmp/ora_conn_test.log
  exit 1
fi
if grep -qi 'ORA-' /tmp/ora_conn_test.log; then
  echo "ERROR: Oracle connection failed:" >&2
  cat /tmp/ora_conn_test.log >&2
  rm -f /tmp/ora_conn_test.log
  exit 1
fi
rm -f /tmp/ora_conn_test.log

prompt_duration_interval

OUTDIR=$(make_outdir "$OUTBASE" "oracle")
OUTFILE="${OUTDIR}/oracle_stats.csv"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION} (${DURATION_SECS}s)  Interval: ${INTERVAL} (${INTERVAL_SECS}s)  Samples: ${SAMPLE_COUNT}"

echo "ts,physical_reads,physical_writes,session_logical_reads,redo_size,user_calls,db_block_gets,consistent_gets,parse_count_total,parse_count_hard,active_sessions" > "$OUTFILE"

run_query() {
  sqlplus -s "$CONNSTR" <<'SQL'
SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF TERMOUT OFF TRIMSPOOL ON MARKUP CSV ON
SELECT
  TO_CHAR(SYSDATE,'YYYY-MM-DD HH24:MI:SS'),
  MAX(DECODE(name,'physical reads',value)),
  MAX(DECODE(name,'physical writes',value)),
  MAX(DECODE(name,'session logical reads',value)),
  MAX(DECODE(name,'redo size',value)),
  MAX(DECODE(name,'user calls',value)),
  MAX(DECODE(name,'db block gets',value)),
  MAX(DECODE(name,'consistent gets',value)),
  MAX(DECODE(name,'parse count (total)',value)),
  MAX(DECODE(name,'parse count (hard)',value)),
  (SELECT COUNT(*) FROM v$session WHERE status='ACTIVE')
FROM v$sysstat
WHERE name IN ('physical reads','physical writes','session logical reads',
  'redo size','user calls','db block gets','consistent gets',
  'parse count (total)','parse count (hard)');
EXIT;
SQL
}

for (( i=0; i<SAMPLE_COUNT; i++ )); do
  run_query 2>>"${OUTDIR}/errors.log" | sed '/^$/d' >> "$OUTFILE" || true
  sleep "$INTERVAL_SECS"
done

echo "Done. $(wc -l < "$OUTFILE") rows written to ${OUTFILE}"
