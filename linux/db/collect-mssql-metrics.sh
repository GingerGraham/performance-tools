#!/usr/bin/env bash
# collect-mssql-metrics.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./db-collect-lib.sh
source "${SCRIPT_DIR}/db-collect-lib.sh"

usage() {
  cat <<EOF
Usage: $(basename "$0") [-d duration] [-i interval] [-S server] [-U user] [-P password] [-n database] [-o outdir]

  -d  Collection duration, e.g. 2h, 30m, 1d
  -i  Sample interval, e.g. 15s, 1m
  -S  Server (host[,port] or host\\instance)   (default: localhost)
  -U  SQL login user       (omit to attempt trusted/AD auth where supported)
  -P  SQL login password   (or set SQLCMDPASSWORD env var)
  -n  Database name        (default: master)
  -o  Output base directory (default: /var/log/sa/diag)

Note: -C (trust server cert) and -N (encrypt) below assume mssql-tools18.
If you're on the older mssql-tools package, drop those two flags.
EOF
}

DURATION="" INTERVAL="" SRV="localhost" SQLUSER="" SQLPASS="${SQLCMDPASSWORD:-}" SQLDB="master" OUTBASE="/var/log/sa/diag"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) DURATION="$2"; shift 2 ;;
    -i) INTERVAL="$2"; shift 2 ;;
    -S) SRV="$2"; shift 2 ;;
    -U) SQLUSER="$2"; shift 2 ;;
    -P) SQLPASS="$2"; shift 2 ;;
    -n) SQLDB="$2"; shift 2 ;;
    -o) OUTBASE="$2"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

SQLCMD_BIN=""
for cand in sqlcmd /opt/mssql-tools18/bin/sqlcmd /opt/mssql-tools/bin/sqlcmd; do
  if command -v "$cand" >/dev/null 2>&1; then SQLCMD_BIN="$(command -v "$cand")"; break; fi
  [[ -x "$cand" ]] && { SQLCMD_BIN="$cand"; break; }
done

if [[ -z "$SQLCMD_BIN" ]]; then
  echo "ERROR: 'sqlcmd' not found (checked PATH, /opt/mssql-tools18/bin, /opt/mssql-tools/bin)." >&2
  echo "Microsoft's SQL tools aren't in the standard distro repos — install from Microsoft's repo:" >&2
  echo "  https://learn.microsoft.com/sql/linux/sql-server-linux-setup-tools" >&2
  echo "Typical flow: add Microsoft's package repo for your distro, then:" >&2
  echo "  sudo ACCEPT_EULA=Y dnf install -y mssql-tools18 unixODBC-devel     # RHEL family" >&2
  echo "  sudo ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev   # Debian/Ubuntu" >&2
  echo "SLES: Microsoft does not officially publish mssql-tools for SLES; use remote collection against the SQL Server host from a supported distro instead." >&2
  exit 1
fi

SQLCMD_FLAGS=(-S "$SRV" -d "$SQLDB" -C -N)
if [[ -n "$SQLUSER" ]]; then
  SQLCMD_FLAGS+=(-U "$SQLUSER" -P "$SQLPASS")
fi

if ! "$SQLCMD_BIN" "${SQLCMD_FLAGS[@]}" -Q "SELECT 1" >/dev/null 2>/tmp/mssql_conn_test.err; then
  echo "ERROR: cannot connect to SQL Server at '${SRV}'." >&2
  cat /tmp/mssql_conn_test.err >&2
  rm -f /tmp/mssql_conn_test.err
  exit 1
fi
rm -f /tmp/mssql_conn_test.err

prompt_duration_interval

OUTDIR=$(make_outdir "$OUTBASE" "mssql")
OUTFILE="${OUTDIR}/mssql_stats.csv"

echo "Output: ${OUTFILE}"
echo "Duration: ${DURATION} (${DURATION_SECS}s)  Interval: ${INTERVAL} (${INTERVAL_SECS}s)  Samples: ${SAMPLE_COUNT}"

QUERY="SET NOCOUNT ON;
SELECT
  GETDATE() AS ts,
  MAX(CASE WHEN counter_name = 'Buffer cache hit ratio' THEN cntr_value END) AS buffer_cache_hit_ratio,
  MAX(CASE WHEN counter_name = 'Page life expectancy' THEN cntr_value END) AS page_life_expectancy,
  MAX(CASE WHEN counter_name = 'Checkpoint pages/sec' THEN cntr_value END) AS checkpoint_pages_sec,
  MAX(CASE WHEN counter_name = 'Lazy writes/sec' THEN cntr_value END) AS lazy_writes_sec,
  MAX(CASE WHEN counter_name = 'Memory Grants Pending' THEN cntr_value END) AS memory_grants_pending,
  MAX(CASE WHEN counter_name = 'Target Server Memory (KB)' THEN cntr_value END) AS target_server_memory_kb,
  MAX(CASE WHEN counter_name = 'Total Server Memory (KB)' THEN cntr_value END) AS total_server_memory_kb,
  MAX(CASE WHEN counter_name = 'Batch Requests/sec' THEN cntr_value END) AS batch_requests_sec,
  MAX(CASE WHEN counter_name = 'SQL Compilations/sec' THEN cntr_value END) AS sql_compilations_sec,
  MAX(CASE WHEN counter_name = 'SQL Re-Compilations/sec' THEN cntr_value END) AS sql_recompilations_sec
FROM sys.dm_os_performance_counters
WHERE object_name LIKE '%Buffer Manager%'
   OR object_name LIKE '%Memory Manager%'
   OR object_name LIKE '%SQL Statistics%';"

echo "ts,buffer_cache_hit_ratio,page_life_expectancy,checkpoint_pages_sec,lazy_writes_sec,memory_grants_pending,target_server_memory_kb,total_server_memory_kb,batch_requests_sec,sql_compilations_sec,sql_recompilations_sec" > "$OUTFILE"

for (( i=0; i<SAMPLE_COUNT; i++ )); do
  "$SQLCMD_BIN" "${SQLCMD_FLAGS[@]}" -h -1 -W -s',' -Q "$QUERY" 2>>"${OUTDIR}/errors.log" | sed '/^$/d;/rows affected/d' >> "$OUTFILE" || true
  sleep "$INTERVAL_SECS"
done

echo "Done. $(wc -l < "$OUTFILE") rows written to ${OUTFILE}"
