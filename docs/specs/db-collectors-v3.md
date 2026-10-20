# DB collectors v3 and database-aware perfdiag — build spec

Status: proposal, 2026-10-20. Reviewed against `GingerGraham/performance-tools` at `8be88c7`.

Scope:

- `linux/db/collect-{postgresql,mysql,oracle,mssql}-metrics.sh` (all at 2.0.0)
- `linux/os/gather-perf-diag.sh`, `linux/os/collect-hw-inventory.sh`, `linux/os/collect-os-metrics.sh` (detection and bundling only)
- `linux/analysis/perfdiag/` (DB loading, detection, rules, hypotheses, report)

Goal: perfdiag finds databases on its own. When a DB collector run is in the bundle, or a database server shows up in the process data, the analysis covers it. When neither is present, the output is unchanged: no empty section, no extra notes, no new switch.

This document is written to be built from by Graham or Claude Code. Section 15 gives the build order. Section 16 lists facts to verify on a live server before release.

---

## 1. Review findings

Severity: **D** = defect (wrong data or failed samples), **G** = gap (data the analysis needs and does not get), **I** = integration (data exists but never reaches the analyser).

### 1.1 All DB collectors

| # | Sev | Finding |
|---|-----|---------|
| A1 | I | DB collectors are told not to run under `sudo`. `/var/log/perf-diag` is created root-owned by `collect-os-metrics.sh` (only the run directory is handed to the user). So DB runs fall back to `~/perf-diag`, and `gather-perf-diag.sh` (default `--base /var/log/perf-diag`) never bundles them. In practice perfdiag never sees DB data. |
| A2 | G | No record of whether the target is on this host. The analyser cannot tell if the OS data in the bundle belongs to the database server or to a client box (RDS, Azure Database, remote VM). |
| A3 | G | No server start or statistics-reset marker. A reset is only visible as a negative delta, and a reset between two samples with net growth is invisible. |
| A4 | D | No query timeout. A hung server holds the sample loop. `PGCONNECT_TIMEOUT` covers connect only; mysql, sqlplus and sqlcmd sample calls have none. |
| A5 | G | No snapshot of the server settings that decide what a number means (buffer sizes, durability settings, `max_connections`, timing switches). |
| A6 | G | No file locations. Without data, log/WAL and temp paths, DB findings cannot be tied to the disk devices that perfdiag already analyses. |
| A7 | D | A malformed row (field count different from the header) is written to the CSV. Nothing checks it. |

### 1.2 PostgreSQL (`collect-postgresql-metrics.sh`)

| # | Sev | Finding |
|---|-----|---------|
| P1 | D | `pg_stat_database` is filtered to `current_database()` and the default `--database` is `postgres`. With defaults, every database counter (`xact_*`, `blks_*`, `tup_*`, `temp_*`, `deadlocks`) describes the maintenance database, not the application. Counters must be totals across all databases, with a per-database breakdown alongside. |
| P2 | D | `psql` is run without `-X`. A `~/.psqlrc` with `\timing`, `\pset` or `\x` changes the output: `\timing on` appends a `Time: …` line, which `echo` writes into the CSV as a second, broken row. |
| P3 | D | `wait_event` exists from 9.6. On 9.2–9.5 (Ubuntu 14.04 ships 9.3, 16.04 ships 9.5, RHEL 7 ships 9.2) every sample query fails. |
| P4 | D | Session counts include the collector's own session and, on 10+, walsenders and autovacuum workers (both can be `state = 'active'`). A replica's walsender shows as a permanently active connection. Filter to `backend_type = 'client backend'` and `pid <> pg_backend_pid()`. |
| P5 | G | `idle in transaction` is not counted. That is the state that holds locks and blocks vacuum. |
| P6 | G | No `blk_read_time`/`blk_write_time` (needs `track_io_timing`), no checkpoint write/sync time, no backend buffer writes, no WAL volume, no wait-event breakdown, no oldest-transaction age, no replication lag, no `max_connections`. |
| P7 | D (minor) | Default `--host localhost` forces TCP. Local peer authentication over the Unix socket fails. Default should be the libpq default (socket); `--host localhost` stays available. |
| P8 | I | No `-v ON_ERROR_STOP=1`; harmless today with one statement, needed once a sample runs several statements. |

### 1.3 MySQL / MariaDB (`collect-mysql-metrics.sh`)

| # | Sev | Finding |
|---|-----|---------|
| M1 | D | MariaDB 11 prints `mysql: Deprecated program name. It will be removed in a future release, use '/usr/bin/mariadb' instead` when its client is invoked as `mysql`. The script prefers `mysql`, so every sample appends that line to `errors.log` and the first-sample check reports "queries are failing". |
| M2 | D | With the default `--host localhost` the client uses the Unix socket and ignores `--port`. On a multi-instance host `--port 3307` silently samples the instance on the default socket. |
| M3 | D (meaning) | `Innodb_row_lock_time_avg` is an average since server start. It does not describe the sample interval. The analyser must use Δ`Innodb_row_lock_time` / Δ`Innodb_row_lock_waits`. |
| M4 | G | Missing the counters that separate "IO-bound" from "flush-bound" from "lock-bound": `Innodb_buffer_pool_wait_free`, `Innodb_log_waits`, dirty/total pages, `Innodb_data_*` and `Innodb_os_log_*` bytes and fsyncs, `Innodb_row_lock_current_waits`, history list length, `Threads_created`, `Sort_merge_passes`, `Select_full_join`, `Uptime`, replica lag. No InnoDB file IO latency (available from `performance_schema` when it is on). |
| M5 | Low | One `awk` process per variable per sample (19 per sample). Replace with one `awk` pass. |

### 1.4 Oracle (`collect-oracle-metrics.sh`)

| # | Sev | Finding |
|---|-----|---------|
| O1 | D (probable) | Counters are selected as bare `NUMBER`s. SQL\*Plus formats unformatted numbers to `NUMWIDTH` (default 10) and shows wider values in scientific notation. `redo size` and `session logical reads` pass 10 digits within days on a busy instance; the row then fails the `^[0-9.,-]+$` check and is dropped into `errors.log`. Verify on a live instance (section 16); the fix (explicit `TO_CHAR`) is correct either way. |
| O2 | D | `SET MARKUP CSV ON` needs SQL\*Plus 12.2 or later. An 11g or 12.1 Instant Client fails every sample with an SP2 error. |
| O3 | D (meaning) | `active_sessions` counts background processes and sessions that are `ACTIVE` but idle-waiting. It is not the number of user sessions doing work. |
| O4 | G | No time model (`DB time`, `DB CPU`) and no wait events. Those are the standard way to split Oracle time into CPU, IO, commit and locks; without them the analysis cannot say why the database is slow. All of it is available from views that need no Diagnostics Pack licence (section 5.4). |
| O5 | G | No multitenant context (CDB or PDB, container name), no instance name or host name. |

### 1.5 SQL Server (`collect-mssql-metrics.sh`)

| # | Sev | Finding |
|---|-----|---------|
| S1 | G | Only memory and compilation counters. No wait statistics (`sys.dm_os_wait_stats`), which are the primary SQL Server diagnostic. |
| S2 | G | No per-file IO latency (`sys.dm_io_virtual_file_stats`). That is the one source that lines up directly with OS per-device latency. |
| S3 | G | No blocking, lock, deadlock, log-flush, scheduler or worker figures. |
| S4 | Low | Rate-versus-gauge is decided by counter name. Record `cntr_type` so the analyser decides from the server's own metadata. |
| S5 | Low | `-C` (trust server certificate) is always added when supported. Keep it, but say so in the help and the manifest (`tls_trust_server_certificate=yes`). |

### 1.6 perfdiag

| # | Sev | Finding |
|---|-----|---------|
| X1 | I | DB runs are discovered (`bundle._RUN_NAME` matches `<host>_postgresql_<ts>`) and then ignored by `loader.load_host`. The only use is `hypotheses.next_steps_for`, which adds a fixed PostgreSQL checkpoint line when a `postgresql` run tag exists. |
| X2 | I | A host whose bundle holds only DB runs is skipped: `LoadError("host … has no os or psar run to analyse")`. A DB collector run from a client box against a managed database is therefore never analysed. |
| X3 | G | Database server processes in the pidstat data are only used as generic "top process" names. The analyser does not know it is looking at a database, so it gives generic next steps. |
| X4 | Low | `bundle._run_from_directory` maps script names to tags for renamed directories, but only for the OS scripts. Add the four DB scripts. |

---

## 2. Shared collector changes (all four scripts)

`SCRIPT_VERSION="3.0.0"` in every DB collector. New shared code goes into block D, identical in all four scripts, as the existing blocks are.

### 2.1 Output contract

Each run directory keeps its existing wide CSV (same file name, existing column names keep their meaning except where section 3–6 says otherwise) and adds:

| File | Content |
|------|---------|
| `<engine>_*.csv` (long format) | Per-sample rows with a key column, for waits, files or databases. Named per engine below |
| `db_paths.txt` | `key=value`: where the database keeps its files (section 2.6) |

`manifest.txt` gains:

| Key | Value |
|-----|-------|
| `collector_schema` | `3`. perfdiag treats a missing key as schema 2 |
| `db_engine` | `postgresql`, `mysql`, `mariadb`, `oracle` or `mssql`. The run tag stays `mysql` for MariaDB |
| `db_instance` | A short display id without secrets: `localhost:5432`, `/var/run/postgresql:5432`, `ORCL/PDB1`, `MSSQLSERVER` |
| `server_hostname` | The host name the server reports about itself (section 2.4) |
| `target_local` | `yes`, `no` or `unknown` |
| `target_local_basis` | Why: `socket`, `loopback`, `server hostname`, `local address`, or `none` |
| `server_start` | Server start time as the server reports it (text; used only for display) |
| `setting.<name>` | One line per recorded server setting (section 2.5) |
| `files` | Space-separated list of the data files this run wrote |
| `query_timeout_s` | The per-sample timeout in force (section 2.3) |

### 2.2 One client call per sample, routed by a record prefix

Each sample runs one client invocation that returns several result sets. Every row starts with a one-letter record type (`S` summary, `W` waits, `F` files, `D` databases, `A` active-session snapshot). Block D routes rows:

```bash
# Splits one sample's client output by its first field and appends each row,
# with the sample timestamp in front, to the file registered for that record type.
# RECORD_FILES maps a record letter to a CSV path, e.g. RECORD_FILES[S]=".../pg_stats.csv".
declare -A RECORD_FILES=()
declare -A RECORD_FIELDS=()     # expected field count per record letter, without ts

route_sample_output() {
    local ts="$1" output="$2" line record rest field_count
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        record="${line%%,*}"
        rest="${line#*,}"
        if [[ -z "${RECORD_FILES[$record]+set}" ]]; then
            echo "${ts} unexpected output: ${line}" >> "$ERROR_LOG"
            continue
        fi
        field_count="$(csv_field_count "$rest")"
        if [[ "$field_count" != "${RECORD_FIELDS[$record]}" ]]; then
            echo "${ts} ${record} row has ${field_count} fields, expected ${RECORD_FIELDS[$record]}: ${line}" >> "$ERROR_LOG"
            continue
        fi
        echo "${ts},${rest}" >> "${RECORD_FILES[$record]}"
    done <<< "$output"
}
```

`csv_field_count` counts fields with quoted strings respected (one `awk` using `FPAT="([^,]*)|(\"([^\"]|\"\")*\")"`; GNU awk is not on every minimal image, so implement it as a small bash loop over characters if `gawk` is missing — readability over speed, rows are short).

String values (database names, wait names, file paths) are emitted already CSV-quoted by the SQL (`'"' || replace(x, '"', '""') || '"'` or the dialect equivalent). Numbers are never quoted. The analyser uses Python's `csv` module, so quoted commas are safe.

The summary record type `S` produces exactly one row per sample. Its absence for a sample means the sample failed; the long-format files have no row for that timestamp either.

### 2.3 Per-sample timeout

```bash
# A sample may take at most this long, so one hung query cannot hold the schedule.
QUERY_TIMEOUT_S=$(( INTERVAL_SECS > 6 ? INTERVAL_SECS - 2 : INTERVAL_SECS > 2 ? INTERVAL_SECS - 1 : 1 ))

run_with_timeout() {
    timeout --kill-after=2 "${QUERY_TIMEOUT_S}" "$@"
}
```

`timeout` is coreutils and present on every target distro, including Ubuntu 14.04. Exit status 124 is logged as `sample timed out after N s`. Engine-level timeouts are added as well where they are simple (PostgreSQL `PGOPTIONS='-c statement_timeout=…'`, `sqlcmd -t`, `mysql --connect-timeout`).

### 2.4 Target locality

```bash
# Sets TARGET_LOCAL (yes|no|unknown) and TARGET_LOCAL_BASIS.
# $1 = host as given (may be empty or a socket directory), $2 = host name the server reports.
resolve_target_locality() {
    local host="$1" server_host="$2" address
    TARGET_LOCAL="no"
    TARGET_LOCAL_BASIS="none"
    if [[ -z "$host" || "$host" == /* ]]; then
        TARGET_LOCAL="yes"; TARGET_LOCAL_BASIS="socket"; return
    fi
    case "$host" in
        localhost|localhost.*|127.*|::1|"[::1]")
            TARGET_LOCAL="yes"; TARGET_LOCAL_BASIS="loopback"; return ;;
    esac
    if [[ -n "$server_host" && "${server_host%%.*}" == "$(short_hostname)" ]]; then
        TARGET_LOCAL="yes"; TARGET_LOCAL_BASIS="server hostname"; return
    fi
    for address in $(getent ahosts "$host" 2>/dev/null | awk '{print $1}' | sort -u); do
        if local_addresses | grep -qxF "$address"; then
            TARGET_LOCAL="yes"; TARGET_LOCAL_BASIS="local address"; return
        fi
    done
    if [[ -z "$server_host" ]]; then
        TARGET_LOCAL="unknown"
    fi
}

local_addresses() {
    ip -o addr show 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4}'
}
```

A database in a container on this host, reached through a published port on `localhost`, is `yes` (same kernel and disks). One reached on its container IP is `no`; the manifest basis makes that visible.

### 2.5 Settings snapshot

```bash
# Records one server setting in the manifest. Newlines are removed; values are not secrets.
record_setting() {
    local name="$1" value="$2"
    value="${value//$'\n'/ }"
    manifest_add "setting.${name}" "$value"
}
```

The per-engine lists are in sections 3–6. A setting the login cannot read is skipped with one `NOTE:` naming the missing privilege.

### 2.6 db_paths.txt

```
path.data.1=/var/lib/pgsql/16/data
path.wal.1=/var/lib/pgsql/16/data/pg_wal
path.temp.1=/var/lib/pgsql/16/data/base/pgsql_tmp
path.tablespace.1=/srv/pg_ts1
path.asm_disk.1=/dev/mapper/asm_data1
path.wal.1.note=assumed inside the data directory; the pg_wal symlink could not be read
```

Roles: `data`, `wal` (PostgreSQL), `redo` (MySQL, Oracle), `log` (SQL Server transaction log), `temp`, `tablespace`, `asm_disk`. Directories, not individual files, except `asm_disk`. Written only when `target_local=yes`; a remote server's paths mean nothing on this host. At most 50 entries per role.

### 2.7 Output location

Unchanged default (`/var/log/perf-diag`, else `~/perf-diag`). When the home fallback is used, the `NOTE:` also says: `gather-perf-diag.sh finds runs under ~/perf-diag of the invoking user, SUDO_USER and the database service accounts (section 7.1)`. The gather side is the fix for A1.

### 2.8 Version numbers

`server_version` keeps its current meaning. Add `server_version_num` (an integer the scripts already compute or can: PostgreSQL `server_version_num`, MySQL `@@version` major·10000+minor·100+patch, Oracle `version_full` digits, SQL Server `ProductMajorVersion`). perfdiag gates on the number, not the text.

---

## 3. PostgreSQL collector v3

### 3.1 Client invocation

```bash
PSQL_ARGS=(-X -q -A -t -F',' -v ON_ERROR_STOP=1 -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE")
if [[ -n "$PG_HOST" ]]; then
    PSQL_ARGS+=(-h "$PG_HOST")
fi
export PGCONNECT_TIMEOUT=5
export PGOPTIONS="-c statement_timeout=$(( QUERY_TIMEOUT_S * 1000 ))"
export PGAPPNAME="perf-diag-collector"
```

- `--host` default becomes empty (libpq default socket). Help: `default: the local Unix socket; pass --host localhost for TCP`.
- `--database` keeps default `postgres`; it is now only the database to connect to. The help and doc say counters cover all databases.
- `PGAPPNAME` makes the collector's sessions recognisable in `pg_stat_activity` and lets the queries exclude any leftover collector session by `application_name`.

### 3.2 Version gates

| Feature | Minimum | Below the minimum |
|---------|---------|-------------------|
| `state`, `blk_read_time`, `temp_bytes`, `deadlocks`, `pg_tablespace_location` | 9.2 | Refuse to run: `PostgreSQL 9.2 or later is needed` |
| `wait_event_type`, `wait_event` | 9.6 | Use the `waiting` boolean; no `pg_waits.csv` |
| `backend_type`, `pg_monitor`, `pg_current_wal_lsn`/`pg_wal_lsn_diff`, `pg_stat_replication.replay_lag` | 10 | Filter autovacuum by `query LIKE 'autovacuum:%'`; use `pg_xlog_location_diff(pg_current_xlog_location(), '0/0')`; no replay lag on the primary |
| `pg_stat_wal` | 14 | WAL columns empty; `wal_lsn_bytes` still filled |
| `pg_stat_io` | 16 | Backend writes from `pg_stat_bgwriter.buffers_backend` |
| `pg_stat_checkpointer` | 17 | Checkpoint counters from `pg_stat_bgwriter` |

Each gate is a bash `if (( SERVER_VERSION_NUM >= … ))` that sets one SQL fragment variable, as `CHECKPOINT_COLUMNS` does today. Name each fragment for what it is (`CLIENT_BACKEND_FILTER`, `WAIT_COLUMN_EXPR`, `WAL_LSN_EXPR`), not by version.

### 3.3 Records

**S → `pg_stats.csv`** (one row per sample). Existing columns first, then new ones. The connection columns change meaning (P4) and the database columns become cluster totals (P1); both are documented as a 3.0.0 change.

| Column | Source | Type |
|--------|--------|------|
| `xact_commit` … `deadlocks` (existing 12) | `sum()` over `pg_stat_database` | counter |
| `bgwriter_checkpoints_timed`, `bgwriter_checkpoints_req`, `bgwriter_buffers_checkpoint`, `bgwriter_buffers_clean` | as today | counter |
| `active_connections`, `idle_connections`, `waiting_connections` | client backends only, excluding self | gauge |
| `blk_read_time_ms`, `blk_write_time_ms` | `sum(blk_read_time)`, `sum(blk_write_time)` | counter (ms) |
| `checkpoint_write_time_ms`, `checkpoint_sync_time_ms` | `pg_stat_bgwriter.checkpoint_*_time` (<17), `pg_stat_checkpointer.write_time/sync_time` (17+) | counter (ms) |
| `backend_buffer_writes`, `backend_buffer_fsyncs` | `buffers_backend`, `buffers_backend_fsync` (<16); `sum(writes)`, `sum(fsyncs)` from `pg_stat_io` where `backend_type = 'client backend'` and `object = 'relation'` (16+) | counter |
| `maxwritten_clean`, `buffers_alloc` | `pg_stat_bgwriter` | counter |
| `idle_in_transaction_connections` | `state IN ('idle in transaction', 'idle in transaction (aborted)')` | gauge |
| `total_connections` | client backends | gauge |
| `max_connections` | `current_setting('max_connections')::int` | gauge |
| `oldest_xact_age_s` | `max(extract(epoch from now() - xact_start))` over client backends | gauge |
| `oldest_active_query_age_s` | same with `query_start`, `state = 'active'` | gauge |
| `xid_age_max` | `max(age(datfrozenxid))` from `pg_database` | gauge |
| `autovacuum_workers` | `backend_type = 'autovacuum worker'` (10+) | gauge |
| `wal_records`, `wal_fpi`, `wal_bytes`, `wal_buffers_full` | `pg_stat_wal` (14+). PostgreSQL 18 removed the write/sync columns from this view, so they are not collected on any version | counter |
| `wal_lsn_bytes` | `pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')`; NULL during recovery | counter |
| `in_recovery` | `pg_is_in_recovery()::int` | gauge |
| `replication_lag_s` | standby: `extract(epoch from now() - pg_last_xact_replay_timestamp())`; primary (10+): `max(extract(epoch from replay_lag))` from `pg_stat_replication`; NULL when neither applies | gauge |
| `server_start_marker` | `extract(epoch from pg_postmaster_start_time())::bigint` | marker |
| `stats_reset_marker` | `extract(epoch from max(stats_reset))::bigint` from `pg_stat_database` | marker |

Shape of the query (one `SELECT` with scalar subqueries, as today):

```sql
SELECT 'S',
  d.xact_commit, d.xact_rollback, ... , d.blk_read_time_ms, d.blk_write_time_ms,
  ${CHECKPOINT_COLUMNS},
  ${BACKEND_WRITE_COLUMNS},
  a.active, a.idle, a.waiting, a.idle_in_xact, a.total, a.oldest_xact_s, a.oldest_query_s,
  current_setting('max_connections')::int,
  (SELECT max(age(datfrozenxid)) FROM pg_database),
  ${AUTOVACUUM_COUNT_EXPR},
  ${WAL_STATS_COLUMNS},
  ${WAL_LSN_EXPR},
  pg_is_in_recovery()::int,
  ${REPLICATION_LAG_EXPR},
  extract(epoch from pg_postmaster_start_time())::bigint,
  d.stats_reset_marker
FROM
  (SELECT sum(xact_commit) AS xact_commit, ..., sum(blk_read_time) AS blk_read_time_ms,
          sum(blk_write_time) AS blk_write_time_ms,
          extract(epoch from max(stats_reset))::bigint AS stats_reset_marker
   FROM pg_stat_database) d,
  (SELECT sum(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS active, ...
   FROM pg_stat_activity
   WHERE pid <> pg_backend_pid() AND ${CLIENT_BACKEND_FILTER}) a,
  ${CHECKPOINT_TABLES};
```

Use `sum(CASE WHEN … THEN 1 ELSE 0 END)`, not `count(*) FILTER (WHERE …)`: `FILTER` needs 9.4, and one form everywhere keeps one code path. `sum` over no rows is NULL; wrap each in `coalesce(…, 0)`.

**W → `pg_waits.csv`** (9.6+): `ts,wait_event_type,wait_event,sessions`

```sql
SELECT 'W',
  '"' || coalesce(wait_event_type, 'CPU') || '"',
  '"' || coalesce(wait_event, 'CPU') || '"',
  count(*)
FROM pg_stat_activity
WHERE state = 'active' AND pid <> pg_backend_pid() AND ${CLIENT_BACKEND_FILTER}
GROUP BY 2, 3;
```

`CPU` means "active with no wait event": on CPU or in uninstrumented code. A type with no row in a sample had zero sessions. These are snapshots at the sample instant, not averages; the doc and the report say so.

**D → `pg_database.csv`**: `ts,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,blk_read_time_ms,blk_write_time_ms`, one row per database with a non-NULL `datname`, name CSV-quoted.

### 3.4 Settings and paths

Settings (`SELECT name, setting, unit FROM pg_settings WHERE name IN (…)`, recorded as `setting.<name>=<setting><unit>`): `block_size`, `shared_buffers`, `max_connections`, `track_io_timing`, `checkpoint_timeout`, `max_wal_size` (or `checkpoint_segments` before 9.5), `checkpoint_completion_target`, `synchronous_commit`, `synchronous_standby_names`, `wal_sync_method`, `full_page_writes`, `effective_io_concurrency`, `work_mem`, `temp_tablespaces`, `data_directory`.

`data_directory` needs `pg_read_all_settings` (part of `pg_monitor`) or superuser. Without it: `NOTE: data_directory is not readable for this login (grant pg_monitor); database files cannot be placed on devices`.

When `target_local=yes`:

- `path.data.1` = `data_directory`.
- `path.wal.1` = `readlink -f "$data_directory/pg_wal"` (`pg_xlog` before 10). The data directory is mode 0700, so this usually fails for a monitoring login; then write the unresolved path with the `.note` line from section 2.6.
- `path.tablespace.N` from `SELECT pg_tablespace_location(oid) FROM pg_tablespace WHERE pg_tablespace_location(oid) <> ''`.
- `path.temp.1` = the first `temp_tablespaces` location when set, else `<data>/base/pgsql_tmp`.

`server_hostname`: `inet_server_addr()`; NULL means a socket connection. PostgreSQL has no SQL function for its host name, so locality relies on the socket, loopback and local-address checks.

### 3.5 Privileges (doc)

- 10+: `GRANT pg_monitor TO monitor;` covers every query here.
- 9.2–9.6: superuser, or accept that other users' `state` and wait columns are NULL (the connection and wait figures under-report; the collector adds a `NOTE:` when `pg_has_role(current_user, 'pg_monitor', 'member')` is false or the role does not exist and the login is not superuser).
- `track_io_timing` off: `NOTE: track_io_timing is off, so read and write latency is not measured. Enable with ALTER SYSTEM SET track_io_timing = on; SELECT pg_reload_conf(); after checking timer overhead with pg_test_timing`.

---

## 4. MySQL / MariaDB collector v3

### 4.1 Client choice and arguments

```bash
# MariaDB 11 warns on every call when its client runs under the name "mysql".
choose_client() {
    local mysql_path=""
    if command -v mysql >/dev/null 2>&1; then
        mysql_path="$(readlink -f "$(command -v mysql)")"
    fi
    if [[ -n "$mysql_path" && "${mysql_path##*/}" == "mariadb" ]]; then
        echo "mariadb"
    elif [[ -n "$mysql_path" ]]; then
        echo "mysql"
    elif command -v mariadb >/dev/null 2>&1; then
        echo "mariadb"
    fi
}

MYSQL_ARGS=(--batch --skip-column-names --connect-timeout=5 -u "$DB_USER")
```

- New `--socket PATH`. Host handling: `--socket` given → `--socket PATH`; `--host localhost` with an explicit `--port` → `-h localhost -P PORT --protocol=TCP` and a `NOTE:`; otherwise `-h HOST -P PORT` as today.
- Default user stays `root`. Doc: on Debian and Ubuntu, `root` authenticates through `unix_socket`/`auth_socket`, which needs the OS user `root`, so run `sudo ./collect-mysql-metrics.sh` for a local root login. This is the one DB collector where `sudo` is the normal local path.

### 4.2 Flavour

`db_engine` = `mariadb` when `VERSION()` contains `MariaDB`, else `mysql`. `setting.version_comment` records Percona and others. Version gates:

| Feature | MySQL | MariaDB |
|---------|-------|---------|
| `SHOW REPLICA STATUS` | 8.0.22 | 10.5.1 |
| else `SHOW SLAVE STATUS` | | |
| `information_schema.INNODB_METRICS` | 5.6 | 10.0 |
| `performance_schema.file_summary_by_event_name` | 5.5 | 5.5 (needs `performance_schema=ON`, off by default on MariaDB) |
| `Innodb_checkpoint_age`, `Innodb_history_list_length` status | — | yes |

### 4.3 Records

One client call per sample:

```sql
SHOW GLOBAL STATUS;
SELECT 'H', COUNT FROM information_schema.INNODB_METRICS WHERE NAME = 'trx_rseg_history_len';
SELECT 'P', CONCAT('"', EVENT_NAME, '"'), COUNT_READ, SUM_TIMER_READ, COUNT_WRITE, SUM_TIMER_WRITE,
       COUNT_MISC, SUM_TIMER_MISC
FROM performance_schema.file_summary_by_event_name
WHERE EVENT_NAME LIKE 'wait/io/file/innodb/%'
  AND COUNT_READ + COUNT_WRITE + COUNT_MISC > 0;
```

`SHOW GLOBAL STATUS` rows are `name<TAB>value` with no record letter, so MySQL is the one engine that does not hand its raw output straight to `route_sample_output`. One `awk` pass reads the whole output: two-field rows build the status map; rows starting `H` or `P` (status variable names never equal either) are converted from tabs to commas and passed through. At the end it prints the `S` row in `STATUS_VARIABLES` order with `history_list_length` from the `H` row. The result goes to `route_sample_output` like the other engines (fixes M5). The `P` query is included only when `@@performance_schema = 1` at start; the `H` query only when `INNODB_METRICS` is readable (needs `PROCESS`). Replica status is a second call per sample, made only when the server was a replica at start; take the maximum `Seconds_Behind_Source` (or `_Master`) across channels; MariaDB multi-source records the default connection only, with a `NOTE:`.

**S → `mysql_stats.csv`**: `ts` + `STATUS_VARIABLES` (existing 19 first) + `history_list_length,replica_lag_s,replica_io_running,replica_sql_running`. Added status variables:

```
Uptime Threads_created Connections Max_used_connections Com_commit Com_rollback
Select_full_join Select_scan Sort_merge_passes Table_locks_waited
Innodb_buffer_pool_wait_free Innodb_buffer_pool_pages_total Innodb_buffer_pool_pages_dirty
Innodb_buffer_pool_pages_free Innodb_buffer_pool_pages_flushed
Innodb_data_reads Innodb_data_writes Innodb_data_read Innodb_data_written Innodb_data_fsyncs
Innodb_os_log_written Innodb_os_log_fsyncs Innodb_log_waits
Innodb_row_lock_current_waits Innodb_rows_read Innodb_rows_inserted Innodb_rows_updated Innodb_rows_deleted
Innodb_checkpoint_age Innodb_checkpoint_max_age Innodb_history_list_length
Bytes_received Bytes_sent
```

A variable the server does not report is an empty field, as today.

**P → `mysql_file_io.csv`**: `ts,event_name,count_read,sum_timer_read_ps,count_write,sum_timer_write_ps,count_misc,sum_timer_misc_ps`. Timers are picoseconds, cumulative. `misc` includes fsync.

### 4.4 Settings and paths

`SHOW GLOBAL VARIABLES WHERE Variable_name IN (…)`: `hostname`, `version_comment`, `max_connections`, `innodb_buffer_pool_size`, `innodb_flush_log_at_trx_commit`, `sync_binlog`, `innodb_flush_method`, `innodb_io_capacity`, `innodb_io_capacity_max`, `innodb_log_file_size`, `innodb_log_files_in_group`, `innodb_redo_log_capacity`, `innodb_doublewrite`, `performance_schema`, `datadir`, `tmpdir`, `innodb_data_home_dir`, `innodb_log_group_home_dir`, `innodb_tmpdir`, `log_bin`, `thread_cache_size`, `long_query_time`.

`server_hostname` = `@@hostname`. Paths: `data` = `datadir`; `redo` = `innodb_log_group_home_dir` resolved against `datadir` when relative (`./`), plus `<datadir>/#innodb_redo` on 8.0.30+; `temp` = each entry of `tmpdir` (colon-separated) and `innodb_tmpdir` when set.

### 4.5 Privileges (doc)

```sql
CREATE USER 'monitor'@'localhost' IDENTIFIED BY '...';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'localhost';
GRANT SELECT ON performance_schema.* TO 'monitor'@'localhost';
```

---

## 5. Oracle collector v3

### 5.1 Output without SQL\*Plus formatting

Drop `SET MARKUP CSV ON` (O2). Every record is built as one string in SQL, with every number through `TO_CHAR(x, 'TM9')` (text-minimum format, no exponent, no padding; fixes O1):

```sql
SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF TRIMSPOOL ON TRIMOUT ON LINESIZE 32767 TAB OFF
SELECT 'S'
  || ',' || TO_CHAR(MAX(DECODE(s.name, 'physical reads', s.value)), 'TM9')
  || ',' || TO_CHAR(MAX(DECODE(s.name, 'physical writes', s.value)), 'TM9')
  ...
FROM v$sysstat s
WHERE s.name IN (...);
```

`NULL || ','` yields `','`, so a missing value is an empty field. This works on SQL\*Plus 10.2 and later. `TM9` is in every supported release; verify it on 11.2 (section 16). Strings use `'"' || REPLACE(x, '"', '""') || '"'`.

`run_sqlplus` keeps the stdin-connect design. Add `WHENEVER OSERROR EXIT FAILURE` and wrap in `run_with_timeout`.

### 5.2 Records

**S → `oracle_stats.csv`**: existing 10 columns, then:

| Column | Source | Type |
|--------|--------|------|
| `physical_read_total_io_requests`, `physical_write_total_io_requests`, `physical_read_total_bytes`, `physical_write_total_bytes` | `v$sysstat` (all IO, including redo and archive) | counter |
| `user_commits`, `user_rollbacks`, `execute_count`, `sorts_disk`, `sorts_memory`, `redo_log_space_requests` | `v$sysstat` | counter |
| `logons_current` | `v$sysstat` `logons current` | gauge |
| `db_time_us`, `db_cpu_us` | `v$sys_time_model` `DB time`, `DB CPU` | counter (µs) |
| `user_sessions` | `v$session` `type = 'USER'` | gauge |
| `user_active_sessions` | `type = 'USER' AND status = 'ACTIVE' AND NOT (state = 'WAITING' AND wait_class = 'Idle') AND sid <> SYS_CONTEXT('USERENV', 'SID')` | gauge |
| `processes_current`, `processes_limit`, `sessions_current`, `sessions_limit` | `v$resource_limit` (`limit_value` is text and can be `UNLIMITED`; map that to NULL) | gauge |
| `server_start_marker` | `ROUND((startup_time - DATE '1970-01-01') * 86400)` from `v$instance` (instance-local clock; used only to detect a change) | marker |

`active_sessions` keeps its old meaning for compatibility and the doc says to prefer `user_active_sessions`.

**E → `oracle_waits.csv`**: `ts,kind,name,total_waits,time_waited_us,total_waits_fg,time_waited_us_fg`

- `kind = class`: every row of `v$system_wait_class` except `Idle`. Its time columns are centiseconds (`TIME_WAITED`, `TIME_WAITED_FG`); multiply by 10000.
- `kind = event`: `v$system_event` for this fixed list (`TIME_WAITED_MICRO`, `TIME_WAITED_MICRO_FG`, `TOTAL_WAITS`, `TOTAL_WAITS_FG`):

```
db file sequential read        db file scattered read          db file parallel read
direct path read               direct path read temp           direct path write temp
log file sync                  log file parallel write         log buffer space
log file switch (checkpoint incomplete)                        log file switch completion
free buffer waits              buffer busy waits               read by other session
enq: TX - row lock contention  enq: TX - index contention      enq: TM - contention
latch: cache buffers chains    library cache: mutex X          cursor: pin S wait on X
resmgr:cpu quantum             gc buffer busy acquire          gc cr block busy
```

**A → `oracle_active.csv`**: `ts,state_class,sessions` — active user sessions (same filter as `user_active_sessions`) grouped by `CASE WHEN state = 'WAITING' THEN wait_class ELSE 'ON CPU' END`. A snapshot, like PostgreSQL's `pg_waits.csv`.

### 5.3 Settings, identity and paths

- `v$instance`: `instance_name`, `host_name` (→ `server_hostname`), `version_full` (18c+) or `version`, `startup_time` (→ `server_start`).
- `v$database`: `cdb` (12c+), `database_role`, `log_mode`. `SYS_CONTEXT('USERENV', 'CON_NAME')` → `setting.con_name`. `db_instance` = `instance_name/con_name`.
- `v$parameter`: `sga_target`, `sga_max_size`, `memory_target`, `pga_aggregate_target`, `db_cache_size`, `processes`, `sessions`, `filesystemio_options`, `disk_asynch_io`, `db_writer_processes`, `resource_manager_plan`, `cpu_count`, `db_block_size`, `cluster_database`.
- Paths (local only): distinct directories of `v$datafile.name` → `data`, `v$tempfile.name` → `temp`, `v$logfile.member` → `redo`. Names starting with `+` are ASM disk groups: record the group as `path.data.N=+DATA` and add `path.asm_disk.N` from `v$asm_disk_stat.path` (the `_stat` view avoids disk discovery). AFD labels (`AFD:…`) cannot be mapped to a device; note it.

Connected to a PDB, `v$sysstat` and `v$sys_time_model` return figures for that container. Doc: connect to `CDB$ROOT` for instance-wide figures (verify the scope per view on 19c and 23ai, section 16). `cluster_database=TRUE`: the doc already says figures are for one RAC instance; the manifest records it so the report can repeat it.

### 5.4 Licensing (doc, prominent)

The collector reads `v$sysstat`, `v$sys_time_model`, `v$system_event`, `v$system_wait_class`, `v$session`, `v$resource_limit`, `v$instance`, `v$database`, `v$parameter`, `v$datafile`, `v$tempfile`, `v$logfile`, `v$asm_disk_stat`. It never reads `v$active_session_history`, `DBA_HIST_*` or AWR, which need the Diagnostics Pack. State this in the script header, the help text and the doc.

Privilege: `GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO monitor;` (or `SELECT ANY DICTIONARY`). Each sample is one logon: it adds to `logons cumulative` and appears in the listener log; at intervals of 5 s or more that is acceptable and the doc says so.

---

## 6. SQL Server collector v3

### 6.1 Client arguments

Add `-l 5` (login timeout), `-t "$QUERY_TIMEOUT_S"` (query timeout), `-b` (non-zero exit on error). Keep `-h -1 -W -s','`. Record `tls_trust_server_certificate=yes|no` in the manifest (S5).

### 6.2 Records

**S → `mssql_stats.csv`**: existing 10 columns, then the counters below from `sys.dm_os_performance_counters`, plus scheduler and request figures. Match `object_name` with `LIKE N'%:Locks'` etc. (named instances prefix `MSSQL$NAME:`) and `RTRIM(instance_name) = N'_Total'` where an instance applies.

| Column | Object : counter (instance) |
|--------|-----------------------------|
| `user_connections` | General Statistics : User Connections |
| `processes_blocked` | General Statistics : Processes blocked |
| `lock_waits_total` | Locks : Lock Waits/sec (`_Total`) |
| `lock_wait_time_ms_total` | Locks : Lock Wait Time (ms) (`_Total`) |
| `deadlocks_total` | Locks : Number of Deadlocks/sec (`_Total`) |
| `log_flushes_total` | Databases : Log Flushes/sec (`_Total`) |
| `log_flush_wait_time_ms_total` | Databases : Log Flush Wait Time (`_Total`) |
| `log_bytes_flushed_total` | Databases : Log Bytes Flushed/sec (`_Total`) |
| `page_reads_total`, `page_writes_total`, `free_list_stalls_total` | Buffer Manager : Page reads/sec, Page writes/sec, Free list stalls/sec |
| `full_scans_total`, `page_splits_total`, `workfiles_created_total`, `worktables_created_total` | Access Methods |
| `memory_grants_outstanding` | Memory Manager : Memory Grants Outstanding |

Further columns:

| Column | Source |
|--------|--------|
| `active_requests` | `COUNT(*)` from `sys.dm_exec_requests` joined to `sys.dm_exec_sessions` with `is_user_process = 1`, excluding `@@SPID` |
| `runnable_tasks`, `work_queue_count`, `active_workers`, `busy_schedulers` | `sys.dm_os_schedulers WHERE status = N'VISIBLE ONLINE'`: `SUM(runnable_tasks_count)`, `SUM(work_queue_count)`, `SUM(active_workers_count)`, `SUM(CASE WHEN is_idle = 0 THEN 1 ELSE 0 END)` |
| `server_start_marker` | `DATEDIFF_BIG(SECOND, '19700101', sqlserver_start_time)` from `sys.dm_os_sys_info` (server-local clock; change detection only) |

At start, record `counter_type.<column>=<cntr_type>` for every counter column (S4). perfdiag uses 65792 as a gauge, 272696576 as a cumulative counter, 537003264 with its base as a ratio. Any other type: the column is read as a gauge and a note is added.

**W → `mssql_waits.csv`**: `ts,wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms`

```sql
SELECT 'W', N'"' + wait_type + N'"', waiting_tasks_count, wait_time_ms, signal_wait_time_ms
FROM sys.dm_os_wait_stats
WHERE wait_type IN (N'WRITELOG', N'LOGBUFFER', N'IO_COMPLETION', N'ASYNC_IO_COMPLETION', N'WRITE_COMPLETION',
                    N'BACKUPIO', N'SOS_SCHEDULER_YIELD', N'CXPACKET', N'CXCONSUMER', N'RESOURCE_SEMAPHORE',
                    N'RESOURCE_SEMAPHORE_QUERY_COMPILE', N'THREADPOOL', N'ASYNC_NETWORK_IO', N'HADR_SYNC_COMMIT',
                    N'CMEMTHREAD')
   OR wait_type LIKE N'PAGEIOLATCH[_]%'
   OR wait_type LIKE N'PAGELATCH[_]%'
   OR wait_type LIKE N'LCK[_]M[_]%';
```

**F → `mssql_files.csv`**: `ts,database,file_type,physical_name,num_of_reads,io_stall_read_ms,num_of_bytes_read,num_of_writes,io_stall_write_ms,num_of_bytes_written`

```sql
SELECT 'F', N'"' + REPLACE(DB_NAME(vfs.database_id), N'"', N'""') + N'"', N'"' + mf.type_desc + N'"',
       N'"' + REPLACE(CAST(mf.physical_name AS nvarchar(400)), N'"', N'""') + N'"',
       vfs.num_of_reads, vfs.io_stall_read_ms, vfs.num_of_bytes_read,
       vfs.num_of_writes, vfs.io_stall_write_ms, vfs.num_of_bytes_written
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
JOIN sys.master_files AS mf ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id;
```

### 6.3 Settings, identity and paths

- `SERVERPROPERTY`: `MachineName` (→ `server_hostname`), `InstanceName` (NULL → `MSSQLSERVER`, → `db_instance`), `ProductVersion`, `ProductMajorVersion`, `Edition`, `IsHadrEnabled`.
- `sys.dm_os_sys_info`: `cpu_count`, `scheduler_count`, `max_workers_count`, `physical_memory_kb`, `committed_target_kb`, `sqlserver_start_time`.
- `sys.configurations`: `max server memory (MB)`, `max degree of parallelism`, `cost threshold for parallelism`, `max worker threads`.
- Paths (local only): distinct directories of `sys.master_files.physical_name` by `type_desc` (`ROWS` → `data`, `LOG` → `log`) and the `tempdb` files → `temp`. In a container, paths are inside the container's mount namespace; record `path_namespace=container` when `MachineName` differs from the host name but the target is local by loopback.

### 6.4 Privileges (doc)

SQL Server 2019 and earlier: `GRANT VIEW SERVER STATE TO monitor;`. SQL Server 2022 and later: `GRANT VIEW SERVER PERFORMANCE STATE TO monitor;` (implied by `VIEW SERVER STATE`). `sys.master_files` also needs `VIEW ANY DEFINITION`; without it the files record is empty and the collector notes it.

---

## 7. Detection and bundling on the server

### 7.1 gather-perf-diag.sh

1. **Search bases.** Without `--base`, look for run directories in `/var/log/perf-diag` and in `<home>/perf-diag` for: the invoking user, `SUDO_USER`, and the service accounts `postgres`, `mysql`, `oracle`, `mssql` when they exist (`getent passwd NAME | cut -d: -f6`). A base that does not exist is skipped silently; one that exists but is unreadable gives `NOTE: cannot read <dir> (run with sudo to include it)`. `--base` becomes repeatable; when given, only those bases are used. Record `base.N=<dir>` in `gather_manifest.txt`.
2. **Database detection.** Run the signature check (section 7.4) at gather time. For each engine detected with no run directory of its tag in the selection: `NOTE: PostgreSQL is running on this host but the bundle has no postgresql run; perfdiag will use process data only. Run linux/db/collect-postgresql-metrics.sh alongside collect-os-metrics.sh next time.` Record `db_detected=postgresql,mysql` in `gather_manifest.txt`.
3. **Redaction.** No change; DB manifests already redact `--connect`.

### 7.2 collect-os-metrics.sh

At start, run the signature check and print one `NOTE:` per engine found, with the matching collector command line. Record `db_detected=` in `manifest.txt`. Do not start DB collectors: they need credentials and a login decision the OS collector cannot make.

### 7.3 collect-hw-inventory.sh

New keys, one block per database server process found (main process only):

```
dbproc.1.engine=postgresql
dbproc.1.pid=1234
dbproc.1.user=postgres
dbproc.1.exe=/usr/pgsql-16/bin/postgres
dbproc.1.cwd=/var/lib/pgsql/16/data
dbproc.1.args_datadir=/var/lib/pgsql/16/data
dbproc.1.instance=
```

- Main process: PostgreSQL — the `postgres`/`postmaster` process whose parent is not `postgres`; MySQL — `mysqld`/`mariadbd`; Oracle — `ora_pmon_<SID>` (`instance=<SID>`; `cwd` is not the data location and is left empty); SQL Server — the `sqlservr` process whose parent is also `sqlservr` (the child of the watchdog).
- `cwd` = `readlink /proc/PID/cwd`. PostgreSQL and MySQL change directory to their data directory, so this gives the data location without a DB login. It needs root or the same user; when unreadable, leave it empty and add `dbproc.N.cwd_note=unreadable (run as root)`.
- `args_datadir` = value of `-D` (PostgreSQL) or `--datadir=` (MySQL) from `/proc/PID/cmdline`, when present.
- Test hooks: honour `HW_INVENTORY_PROC` (default `/proc`) the way `HW_INVENTORY_SYSFS` works today, so `fakesys.py` can build process entries.

### 7.4 Signature table (identical in the three scripts and in perfdiag)

| Engine | Match on `comm` / first argument | Notes |
|--------|----------------------------------|-------|
| postgresql | `postgres`, `postmaster`; argument 0 starting `postgres:` (retitled backends) | |
| mysql | `mysqld`, `mariadbd` | not `mysqld_safe`, not `mysqld_exporter` |
| oracle | `ora_pmon_<SID>` (background), `oracle<SID>` (foreground) | `asm_pmon_+ASM` is an ASM instance: noted, not a database |
| mssql | `sqlservr` | |

Use `ps -eo pid=,ppid=,user=,args=` and match on the basename of the first argument. `comm` is cut at 15 characters (`ora_pmon_ORCLCDB` becomes `ora_pmon_ORCLCD`), so the SID always comes from `args`.

---

## 8. perfdiag: detection model and loading

### 8.1 Where a database comes from

| Source | Gives | Instance `source` |
|--------|-------|-------------------|
| DB collector run in the bundle | DB metrics, settings, paths, locality | `collector` |
| `dbproc.*` keys in `hardware.txt` | engine, data location | merged into the above, or `process` |
| pidstat rows matching the signature table | engine, CPU/IO/RSS of its processes, role breakdown | merged into the above, or `process` |

Rules:

1. A collector run always creates an instance.
2. A process match with no collector instance of that engine creates a `process` instance.
3. A process match with a collector instance of the same engine and `target_local=yes` attaches the process keys to it. Several local instances of one engine (two PostgreSQL clusters): match by data directory (`dbproc.*.cwd`, `-D` from the pidstat command line) against `path.data.1`; if that is not possible, attach to all and note `process data could not be split between the PostgreSQL instances on this host`.
4. Nothing matched → `host.databases` is empty. No section, chart, note, coverage line or JSON key changes for that host (the JSON `databases` list is present and empty, section 12.4).

Process detection threshold: a signature seen in any pidstat sample in the window. pidstat lists only tasks with activity, so an idle database can be missed; that is acceptable, because an idle database is not part of the problem.

### 8.2 Data model (`model.py`)

```python
@dataclass
class DatabaseInstance:
    id: str                       # "postgresql:localhost:5432" - stable across runs of one target
    short: str                    # "pg:5432" - used as the indicator device (fits the text tables)
    engine: str                   # postgresql | mysql | mariadb | oracle | mssql
    label: str                    # "PostgreSQL 16.4 (localhost:5432)"
    source: str                   # "collector" or "process"
    local: Optional[bool] = None  # None = unknown
    version: str = ""
    version_num: Optional[int] = None
    collector_schema: int = 0     # 0 for process-only instances
    runs: List[Run] = field(default_factory=list)
    series: Dict[Tuple[str, str], Series] = field(default_factory=dict)   # (metric, sub) -> derived series
    basis: Dict[str, str] = field(default_factory=dict)                   # metric -> "time-averaged" | "sampled"
    settings: Dict[str, str] = field(default_factory=dict)
    paths: Dict[str, List[str]] = field(default_factory=dict)             # role -> paths
    devices: Dict[str, List[str]] = field(default_factory=dict)           # role -> device names in the disk data
    process_keys: List[str] = field(default_factory=list)                 # attribution.process_key values
    process_roles: Dict[str, str] = field(default_factory=dict)           # command -> role ("checkpointer", "dbw0", ...)
    resets: List[int] = field(default_factory=list)                       # epochs of restarts or stats resets
    notes: List[str] = field(default_factory=list)

    def get(self, metric, sub=""):
        found = self.series.get((metric, sub))
        if found is not None and len(found) > 0:
            return found
        return None
```

`HostData` gains `databases: List[DatabaseInstance] = field(default_factory=list)`.

Database series do not go into `host.series`. `HostData.get` and `devices` keep returning OS data only, so no existing rule can pick up a DB series by accident.

### 8.3 New modules

| Module | Purpose |
|--------|---------|
| `readers/dbcsv.py` | Header-driven CSV readers for the DB collector files. Standard library `csv`. `ts` through `timeutil.parse_iso`. Empty, `NULL` or non-numeric → NaN. Rows with the wrong field count are skipped and counted (one note per file). Returns `WideTable(times, columns)` and `LongTable(key_columns, rows_by_key)` |
| `dbdetect.py` | The signature table (section 7.4), `instances_from_processes(host)`, `instances_from_inventory(hardware)`, process role tables |
| `dbengines.py` | One adapter per engine: raw tables → canonical series (section 9). Shared helpers: `counter_rates`, `delta_ratio`, `wait_sessions` |
| `dbload.py` | `load_databases(host, db_runs)`: reads runs, builds and merges instances, maps paths to devices, applies process detection |
| `dbrules.py` | DB indicators (section 10), called from `rules.build_indicators` |
| `dbhypotheses.py` | D1–D9 builders and the cross-links (section 11), called from `hypotheses.build_hypotheses` |
| `dbcharts.py` | DB charts, added to `charts.chart_catalogue` |
| `report_db.py` | Section data for the Databases part of both reports |

### 8.4 Loader changes (`loader.py`)

- `DB_TAGS = ("postgresql", "mysql", "oracle", "mssql")`. In `load_host`, `db_runs = [run for run in runs if run.tag in DB_TAGS]`.
- Guard: raise `LoadError` only when there is no `os`, `psar` or DB run.
- `reference_manifest = (os_runs or psar_runs or db_runs)[0].manifest`. DB manifests already carry `cpus`, `mem_total_kb` and `timezone`.
- After hardware and pidstat are loaded, call `dbload.load_databases(host, db_runs)`.
- `_fill_window`: when the host has OS series, the window comes from them only. DB series are clipped to it; if a DB run covers less than half of it, note `postgresql run covers 35 min of the 2 h window`. A host with only DB runs takes its window from the DB series.
- `apply_window`: also clip every instance's series and resets.
- `bundle._run_from_directory`: add the four DB scripts to the script-to-tag fallback (X4).

### 8.5 Path to device mapping (`dbload.map_path`)

1. Normalise the path (`os.path.normpath`; no filesystem access, the path belongs to the server).
2. Longest mount point in `hardware.filesystems` that is the path or a parent of it on a `/` boundary.
3. From that filesystem's `kname`: the kname itself, its display name, partition parents (`loader.partition_parent`) and every slave below it (`Hardware.sits_on` order). Keep only names present in the disk series (`host.devices("disk.tps")` or `disk.read_kBps`).
4. Raw device paths (`/dev/sdb`, `/dev/mapper/x`, `/dev/nvme1n1`) skip step 2 and resolve by basename or display name with `Hardware.find_disk`. `/dev/oracleasm/disks/*` and `AFD:*` cannot be resolved: note it.
5. No inventory → no mapping, one note per instance: `no hardware inventory: PostgreSQL files could not be placed on devices`.

Result goes into `instance.devices[role]`. The report shows the chain with `report_common.device_chain`.

### 8.6 Process roles (attribution detail)

The role tables let the report say which part of a database did the work, with or without a collector:

| Engine | Command (pidstat `-l`) | Role |
|--------|------------------------|------|
| postgresql | `postgres: checkpointer` | checkpointer |
| | `postgres: background writer` | background writer |
| | `postgres: walwriter` | WAL writer |
| | `postgres: autovacuum worker` / `autovacuum launcher` | autovacuum |
| | `postgres: walsender …` / `walreceiver` / `startup` | replication |
| | other `postgres: …` | client backend |
| | `postgres -D …` / `postmaster` | postmaster |
| oracle | `ora_dbw*_SID`, `ora_bw*_SID` | DB writer |
| | `ora_lgwr_SID`, `ora_lg0*_SID` | log writer |
| | `ora_ckpt_SID` | checkpoint |
| | `ora_arc*_SID` | archiver |
| | `oracleSID …` | foreground |
| | other `ora_*_SID` | background |
| mysql, mssql | single process | server (with `--threads`, thread rows are listed by their own names) |

PostgreSQL titles vary across versions (`postgres: checkpointer process` before 10). Match on the words after `postgres: `, ignoring a trailing `process`.

`attribution.process_key` is unchanged: existing output stays the same. Roles are an extra breakdown inside the database context (section 11.3).

---

## 9. Canonical DB metrics

`dbengines.py` turns raw columns into these series. All rates are per second over the sample interval, ending at the sample time, matching `Series` conventions.

### 9.1 Rate rules (shared helpers)

```python
def counter_rates(times, values, nominal_interval_s):
    """(times, rates, reset_times) from a cumulative counter.

    The rate for sample i covers (times[i-1], times[i]]. A negative difference is a
    restart or a statistics reset: that sample is NaN and its time is a reset.
    A gap over 2 x the nominal interval gives NaN, so a rate never spans a gap.
    """
```

- Ratios of deltas (`delta_ratio(numerator, denominator, minimum_denominator)`): NaN when the denominator delta is below the minimum, so a quiet interval does not produce a 0 % or 100 % spike.
- Wait time to sessions (`wait_sessions(time_values, unit_per_second)`): Δtime / Δt in seconds of waiting per second = average sessions waiting over the interval.
- Marker columns (`server_start_marker`, `stats_reset_marker`, MySQL `Uptime` decreasing): a change adds a reset at that sample even when every counter grew.
- The adapter records `basis[metric]` as `time-averaged` or `sampled` (snapshot at the sample instant).

### 9.2 Metrics

| Metric | PostgreSQL | MySQL / MariaDB | Oracle | SQL Server |
|--------|-----------|-----------------|--------|------------|
| `db.statements_ps` | — | Δ`Questions` | Δ`execute_count` | Δ`batch_requests_total` |
| `db.transactions_ps` | Δ(`xact_commit`+`xact_rollback`) | Δ(`Com_commit`+`Com_rollback`) | Δ(`user_commits`+`user_rollbacks`) | — |
| `db.sessions_active` (sampled) | `active_connections` | `Threads_running` − 1 | `user_active_sessions` | `active_requests` |
| `db.connections_pct` | `total_connections` / `max_connections` | `Threads_connected` / `setting.max_connections` | `max(processes, sessions)` current / limit | `active_workers` / `max_workers_count` |
| `db.wait.<category>` | `pg_waits.csv` counts (sampled) | `lock`: Δ`Innodb_row_lock_time` ms; `io_read`/`io_write`/`commit`: `mysql_file_io.csv` timers (time-averaged) | class and event times, foreground (time-averaged); `cpu` = Δ`db_cpu_us` | `mssql_waits.csv` wait time (time-averaged); `cpu` from signal waits of `SOS_SCHEDULER_YIELD` |
| `db.read_latency_ms` | Δ`blk_read_time_ms` / Δ`blks_read` | Δ`sum_timer_read_ps` / Δ`count_read` of the data-file events | `db file sequential read` foreground time / waits | per data file: Δ`io_stall_read_ms` / Δ`num_of_reads` (sub = device); instance value = read-weighted mean |
| `db.commit_latency_ms` | — (see `db.wait.commit`) | redo-file (write + misc) time / (write + misc) count | `log file sync` foreground time / waits | `WRITELOG` time / count; per log file stall / writes (sub = device) |
| `db.cache_hit_pct` | Δ`blks_hit` / Δ(`blks_hit`+`blks_read`) | 1 − Δ`Innodb_buffer_pool_reads` / Δ`Innodb_buffer_pool_read_requests` | 1 − Δ`physical_reads` / Δ`session_logical_reads` | `buffer_cache_hit_ratio` (gauge, already a ratio) |
| `db.read_kBps`, `db.write_kBps` | Δ`blks_read` × `block_size`; writes: Δ(`bgwriter_buffers_checkpoint`+`bgwriter_buffers_clean`+`backend_buffer_writes`) × `block_size` | Δ`Innodb_data_read`, Δ`Innodb_data_written` | Δ`physical_read_total_bytes`, Δ`physical_write_total_bytes` | Δ file-stats bytes (sub = device) |
| `db.temp_kBps` | Δ`temp_bytes` | — (`db.temp_tables_ps` = Δ`Created_tmp_disk_tables`) | — (`db.wait.temp` from direct path read/write temp) | Δ tempdb file writes bytes |
| `db.flush_stalls_ps` | Δ`backend_buffer_fsyncs` | Δ`Innodb_buffer_pool_wait_free` | Δ`free buffer waits` total_waits | Δ`free_list_stalls_total` |
| `db.backend_writes_pct` | Δ`backend_buffer_writes` / Δ(all buffer writes) | — | — | — |
| `db.dirty_pct` | — | `pages_dirty` / `pages_total` | — | — |
| `db.checkpoint_age_pct` | — | MariaDB: `Innodb_checkpoint_age` / `Innodb_checkpoint_max_age` | — | — |
| `db.lock_wait_avg_ms` | — | Δ`Innodb_row_lock_time` / Δ`Innodb_row_lock_waits` | — | Δ`lock_wait_time_ms_total` / Δ`lock_waits_total` |
| `db.deadlocks_ps` | Δ`deadlocks` | — | — | Δ`deadlocks_total` |
| `db.cpu_demand_per_cpu` | sampled `CPU` sessions / host CPUs | — | (`db.wait.cpu` + `resmgr:cpu quantum` sessions) / `cpu_count` | (`runnable_tasks` + `busy_schedulers`) / `scheduler_count` |
| `db.concurrency_per_cpu` | — | (`Threads_running` − 1) / host CPUs | — | — |
| `db.compile_pct` | — | — | Δ`parse_count_hard` / Δ`execute_count` | Δ`sql_compilations_total` / Δ`batch_requests_total` |
| `db.memory_grants_pending` | — | — | — | `memory_grants_pending` |
| `db.worker_queue` | — | — | — | `work_queue_count` |
| `db.xact_age_s` | `oldest_xact_age_s` | — | — | — |
| `db.history_list` | — | `history_list_length` (or `Innodb_history_list_length`) | — | — |
| `db.replication_lag_s` | `replication_lag_s` | `replica_lag_s` | — | — |
| `db.checkpoints` (counts, not rates) | Δ`bgwriter_checkpoints_timed`, Δ`bgwriter_checkpoints_req` | — | Δ`log file switch (checkpoint incomplete)` waits | — |

Minimum denominators (settings, section 10.2): reads for latency `db.min_reads_ps` (default 10/s), commits for commit latency `db.min_commits_ps` (1/s), logical reads for cache hit `db.cache_hit.min_reads_ps` (100/s).

Host CPU count for the per-CPU metrics is `host.cpus` when the instance is local; for a remote instance the engine's own count (`setting.cpu_count`, `scheduler_count`) or, for PostgreSQL and MySQL, no per-CPU metric.

### 9.3 Wait categories

`db.wait.<category>` with categories `cpu`, `io_read`, `io_write`, `commit`, `lock`, `latch`, `memory`, `workers`, `parallel`, `network`, `replica`, `temp`, `other`.

| Engine | Mapping |
|--------|---------|
| PostgreSQL | `CPU` → cpu. Event name starting `WAL` (any type, case-insensitive) → commit. Type `IO`: names ending `Read` → io_read, others → io_write. `Lock`, `BufferPin` → lock. `LWLock` → latch. `IPC` with name `SyncRep` → replica, other `IPC` → other. `Client` → network. `Timeout`, `Extension`, `Activity` → other |
| MySQL | data-file read → io_read; data-file write + misc → io_write; redo/log file write + misc → commit; temp file events → temp; row lock time → lock. Event name contains `data` → data file; `log` or `redo` → redo; `temp` → temp |
| Oracle | events first: `db file *`, `direct path read`, `read by other session` → io_read; `direct path * temp` → temp; `log file sync` → commit; `log buffer space`, `log file switch *` → commit; `free buffer waits` → io_write; `enq: TX - row lock contention`, `enq: TM - contention` → lock; `latch: *`, `library cache: *`, `cursor: *`, `buffer busy waits`, `enq: TX - index contention` → latch; `resmgr:cpu quantum` → cpu; `gc *` → replica. Then classes for the remainder: `User I/O` → io_read, `System I/O` → io_write, `Commit` → commit, `Application` → lock, `Concurrency` → latch, `Scheduler` → cpu, `Network` → network, `Cluster` → replica, others → other. Remainder = class time minus the events of that class already counted |
| SQL Server | `PAGEIOLATCH_*` → io_read; `IO_COMPLETION`, `ASYNC_IO_COMPLETION`, `WRITE_COMPLETION`, `BACKUPIO` → io_write; `WRITELOG`, `LOGBUFFER` → commit; `HADR_SYNC_COMMIT` → replica; `LCK_M_*` → lock; `PAGELATCH_*`, `CMEMTHREAD` → latch; `SOS_SCHEDULER_YIELD` → cpu; `CXPACKET`, `CXCONSUMER` → parallel; `RESOURCE_SEMAPHORE*` → memory; `THREADPOOL` → workers; `ASYNC_NETWORK_IO` → network |

SQL Server wait time is per task; a parallel query adds several tasks, so `parallel` and `io_read` sessions can exceed the number of sessions. The report states this beside the waits chart.

---

## 10. DB indicators and settings

### 10.1 Indicators (`dbrules.py`)

Each is an `rules.Indicator` with `device = instance.short` and `metric` set to the canonical metric, so `analysis._detect_all` handles episodes, time-above and transients with no change. `analysis.IMPACT_PAIRS` and `SELF_IMPACT` gain entries as listed.

| Indicator | Metric | Direction | Thresholds | Role | Impact |
|-----------|--------|-----------|-----------|------|--------|
| `db_read_latency` | `db.read_latency_ms` | above | class of the mapped data device (`thresholds.resolve_disk_profile`), else `db.read_latency.*` | primary | self |
| `db_commit_latency` | `db.commit_latency_ms` | above | class of the mapped log/WAL/redo device, else `db.commit_latency.*` | primary | self |
| `db_commit_wait` | `db.wait.commit` (PostgreSQL only, no latency source) | above | `db.commit_wait.*` sessions | primary | self |
| `db_lock_wait` | `db.wait.lock` | above | `db.lock_wait.*` sessions | primary | self |
| `db_latch_wait` | `db.wait.latch` | above | `db.latch_wait.*` sessions | primary | self |
| `db_io_wait` | `db.wait.io_read` + `io_write` | above | `db.io_wait.*` sessions | supporting | |
| `db_cpu_demand` | `db.cpu_demand_per_cpu` | above | `db.cpu_demand.*` | primary | self |
| `db_concurrency` | `db.concurrency_per_cpu` | above | `db.cpu_demand.*` | supporting | |
| `db_flush_stall` | `db.flush_stalls_ps` | above | `db.flush_stall.*` | primary | self |
| `db_memory_grants` | `db.memory_grants_pending` or `db.wait.memory` | above | `db.memory_grants.*` | primary | self |
| `db_connections` | `db.connections_pct` | above | `db.connections.*` | primary | self |
| `db_worker_queue` | `db.worker_queue` or `db.wait.workers` | above | `db.worker_queue.*` | primary | self |
| `db_repl_lag` | `db.replication_lag_s` | above | `db.replication_lag.*` | primary | self |
| `db_cache_hit` | `db.cache_hit_pct` | below | `db.cache_hit.*` | supporting | pairs with `db_read_latency`, `disk_r_await` |
| `db_temp` | `db.temp_kBps` | above | `db.temp.*` | supporting | pairs with `disk_w_await`, `disk_await` |
| `db_xact_age` | `db.xact_age_s` | above | `db.xact_age.*` | supporting | |
| `db_history_list` | `db.history_list` | above | `db.history_list.*` | supporting | |
| `db_compile` | `db.compile_pct` | above | `db.compile.*` | supporting | |
| `db_dirty` | `db.dirty_pct` / `db.checkpoint_age_pct` | above | `db.dirty.*` | supporting | |

Principle 3 of the method holds: load indicators (`db_io_wait`, `db_temp`, `db_concurrency`, `db_cache_hit`) are supporting only. A finding needs a latency, wait or exhaustion indicator.

For `sampled` metrics, the indicator gets the label `sampled every N s, not averaged`, which `analysis._detect_all` copies into episode notes.

Remote instances (`local is False`) get their indicators as usual. Section 11 decides what they may link to.

### 10.2 New settings (`thresholds.DEFAULTS`)

```
db.min_reads_ps = 10
db.min_commits_ps = 1
db.read_latency.warn_ms = 10        db.read_latency.crit_ms = 30
db.commit_latency.warn_ms = 10      db.commit_latency.crit_ms = 30
db.commit_wait.warn_sessions = 1    db.commit_wait.crit_sessions = 4
db.lock_wait.warn_sessions = 1      db.lock_wait.crit_sessions = 5
db.latch_wait.warn_sessions = 1     db.latch_wait.crit_sessions = 5
db.io_wait.warn_sessions = 1        db.io_wait.crit_sessions = 4
db.cpu_demand.warn_per_cpu = 0.9    db.cpu_demand.crit_per_cpu = 1.5
db.flush_stall.warn_ps = 0.01       db.flush_stall.crit_ps = 1
db.memory_grants.warn = 1           db.memory_grants.crit = 5
db.connections.warn_pct = 80        db.connections.crit_pct = 95
db.worker_queue.warn = 1            db.worker_queue.crit = 10
db.replication_lag.warn_s = 30      db.replication_lag.crit_s = 300
db.cache_hit.warn_pct = 95          db.cache_hit.crit_pct = 90       db.cache_hit.min_reads_ps = 100
db.temp.warn_kBps = 1024            db.temp.crit_kBps = 10240
db.xact_age.warn_s = 600            db.xact_age.crit_s = 3600
db.history_list.warn = 100000       db.history_list.crit = 1000000
db.compile.warn_pct = 10            db.compile.crit_pct = 25
db.dirty.warn_pct = 75              db.dirty.crit_pct = 90
db.checkpoint_req.warn_pct = 50
```

`db.checkpoint_req.warn_pct` is a window fact, not an indicator: PostgreSQL checkpoints come every few minutes, too sparse for episode detection. With at least two checkpoints in a hypothesis window and requested ≥ that share of them, the fact `N of M checkpoints were requested (WAL volume reached max_wal_size), not timed` is added.

They are overridable with `--set`, `--config` and `perfdiag.ini` like every other key. No dedicated flags.

---

## 11. Hypotheses

### 11.1 D codes (`dbhypotheses.py`)

Titles go into `hypotheses.TITLES`. Each is built with `Builder.best_primary` over its primary indicators per instance, `subject = instance.short`, so recurring transients work as for H codes. Ranking is shared with H codes (`rank_score` from the primary episode): a lock problem on an otherwise idle host ranks first.

| Code | Title | Primary | Supporting episodes and facts | Against |
|------|-------|---------|-------------------------------|---------|
| D1 | Database reads waiting on storage | `db_read_latency` | `disk_r_await`/`disk_await` on the data devices; `db_io_wait`; `db_cache_hit` below; `mem_avail`/H6 overlapping (less page cache means more physical reads) | read rate fell during the window (fewer reads, slower: points to contention, not volume) |
| D2 | Commit latency (log or WAL writes) | `db_commit_latency` or `db_commit_wait` | `disk_w_await` on the log/WAL/redo devices; H7; `db.wait.replica` sessions (synchronous replica); MySQL `Innodb_log_waits`, Oracle `log buffer space` | |
| D3 | Lock contention | `db_lock_wait` | `db_xact_age`, `db_history_list`; deadlocks in the window (fact); fact `host CPU idle averaged N % and no disk latency episode` when true | |
| D4 | Internal latch or buffer contention | `db_latch_wait` | `db_cpu_demand` near warn; PostgreSQL `LWLock` event names (fact, top 3); SQL Server `PAGELATCH_*` on tempdb when tempdb files are hot (fact) | |
| D5 | Database CPU demand above capacity | `db_cpu_demand` | H3 or H5 overlapping with a process of this instance in the top CPU list; `db_compile`; Oracle `resmgr:cpu quantum` (fact: `Resource Manager limited CPU; the plan, not the host, set the limit`) | host CPU idle above 40 % while demand is high on a local instance (fact; points at Resource Manager, a cgroup or CPU affinity) |
| D6 | Dirty-buffer flushing falling behind | `db_flush_stall` | `disk_w_await`/H1/H7 on the data devices; `db.backend_writes_pct` over 50 % (fact); requested checkpoints (fact); `db_dirty` | |
| D7 | Query memory grants queued | `db_memory_grants` | H6; `db_temp` | |
| D8 | Connection or worker exhaustion | `db_connections` or `db_worker_queue` | `Aborted_connects` rising (fact); `db_lock_wait` overlapping (blocked sessions pile up) | |
| D9 | Replication lag | `db_repl_lag` | `disk_*` latency on the data devices of a local standby; `db_io_wait` | |

D1 gets one extra note when the mapped data device has no disk latency episode in the window: `the device's own latency stayed under its warn threshold while the database waited; look above the block layer: filesystem locks, cgroup io.max throttling, a network filesystem, or memory pressure evicting the page cache`. This is a note, not "against": it moves the next steps, not the confidence.

### 11.2 Next steps (engine table)

`next_steps_for` gains D codes and replaces its two hardcoded PostgreSQL lines with lookups in `DB_NEXT_STEPS[(code, engine)]`. One or two steps per pair; examples:

| Code | Engine | Steps |
|------|--------|-------|
| D1 | postgresql | `Find the queries with the most read time: pg_stat_statements ordered by shared_blk_read_time (blk_read_time before 17).` `Check shared_buffers (now X) against the working set.` |
| D1 | mssql | `Compare the per-file read stall in sys.dm_io_virtual_file_stats with the device latency shown here.` `Find the top readers in Query Store or sys.dm_exec_query_stats ordered by total_physical_reads.` |
| D2 | postgresql | `Put pg_wal on a low-latency device; check wal_sync_method (now X) and whether synchronous_commit (now X) or synchronous_standby_names adds replica round-trips.` |
| D2 | mysql | `innodb_flush_log_at_trx_commit is X and sync_binlog is Y; values other than 1 trade durability for latency and need sign-off.` |
| D2 | oracle | `Compare log file sync with log file parallel write: close values mean the redo device is slow; a large gap means CPU or commit frequency.` |
| D2 | mssql | `Check WRITELOG against the LOG file stalls; consider delayed durability only with sign-off.` |
| D3 | postgresql | `Find blockers: SELECT pid, pg_blocking_pids(pid), state, xact_start, query FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;` |
| D3 | mysql | `Find blockers: sys.innodb_lock_waits (MySQL 8), or information_schema.INNODB_TRX for long transactions.` |
| D3 | oracle | `Find blockers: v$session where blocking_session is not null.` |
| D3 | mssql | `Find blockers: sys.dm_exec_requests where blocking_session_id <> 0; enable the blocked process report for history.` |
| D5 | oracle | `Top SQL by CPU in v$sqlstats; check resource_manager_plan (now X).` |
| D6 | postgresql | `Raise max_wal_size (now X) if checkpoints are requested; tune bgwriter_lru_maxpages if backends write their own buffers.` |
| D6 | mysql | `Check innodb_io_capacity (now X) and innodb_io_capacity_max (now Y) against what the device sustains.` |
| D8 | any | `Compare client pool sizes with max_connections (now X); check for sessions left open.` |

Settings come from `instance.settings`; a step that cites a setting the run did not record leaves out the `(now X)` part. Process-only instances get the generic steps of their engine plus: `Run linux/db/collect-<engine>-metrics.sh alongside collect-os-metrics.sh during the next occurrence for database-level evidence.`

### 11.3 Cross-links between H and D

`dbhypotheses.link_databases(builder, found)` runs after all H and D hypotheses are built and before `_attach_context` computes next steps.

For each H hypothesis in H1, H2, H3, H5, H6, H7, H8, H12 with a primary episode:

1. **Which instances.** Local or process instances whose `process_keys` appear in `hypothesis.top_processes`, or whose `devices` include the hypothesis subject (disk codes). Remote instances never link to H codes.
2. **Database context** (`hypothesis.database_context`, list of dicts, one per instance): over the primary window, the mean of `db.statements_ps`/`db.transactions_ps`, the three largest `db.wait.*` categories, `db.read_latency_ms`, `db.commit_latency_ms`, `db.temp_kBps`, and the database's share of the device's IO (`db.write_kBps` / device `write_kBps`, only when both exist and the instance is local). For process instances: the role breakdown (`checkpointer 60 MiB/s writes, client backends 10 MiB/s`).
3. **Supporting facts.** When a D hypothesis of a linked instance overlaps (`hypotheses.overlaps`), add to the H hypothesis: `PostgreSQL (pg:5432) sessions waiting on IO averaged 6.1 in this window (D1)`. Add the reverse to the D hypothesis: `nvme1n1 (/var/lib/pgsql) write await averaged 48 ms (H7)`. Both count as supporting for confidence.
4. **Links.** `hypothesis.links` (new field, list of `(code, subject)`) on both sides, so reports can render "See also".
5. **Next steps.** Engine steps for the H code from `DB_NEXT_STEPS[(code, engine)]` (the existing PostgreSQL checkpoint line becomes `("H7", "postgresql")` and `("H1", "postgresql")`).

For each D hypothesis of a local instance, add the overlapping OS episodes on its mapped devices (`disk_*`), CPU (`cpu_busy`, `runq`, `psi_cpu`) and memory (`mem_avail`, `psi_mem_*`) as supporting episodes, by the same overlap rule used inside `hypotheses.Builder.overlapping`. With no device mapping, disk episodes are added only as facts worded `on <device> (not confirmed as this database's device)`.

Remote instances: D hypotheses are built and reported; no OS evidence is attached, and each gets the note `the database server is remote (<db_instance>); the OS data in this bundle is from <host>, the collecting machine, and is not used as evidence`.

### 11.4 Changes to existing code

- `Hypothesis` gains `database_context: List[dict]` and `links: List[Tuple[str, str]]`.
- `findings.subject_label`: D codes → `instance.label`.
- `findings.hypothesis_sections`: new key `"database"` with one line per context entry; rendered after `"processes"` by text, Markdown and HTML. `"processes"` label for D codes: none (process lists belong to H codes).
- `findings.NOT_SEEN_CHECKS`: D1, D2, D6 use the storage group; D5 uses the cpu group.
- `hypotheses.ATTRIBUTION_KIND`: D5 → `cpu`, D1/D2/D6 → `disk` (device = the first mapped data or log device), so the top processes for the window are listed.
- `hypotheses.DB_RUN_TAGS` is removed; engine names come from `dbdetect`.

---

## 12. Output

### 12.1 Text (`perfdiag analyse`)

D hypotheses appear in the ranked list like H hypotheses. The sustained-episode table shows DB indicators with `device = instance.short`. One header line per instance after the window line, only when instances exist:

```
Database: PostgreSQL 16.4 (pg:5432), collector, local, data on nvme1n1, WAL on nvme1n1
Database: mysqld (process data only), seen in 96 % of pidstat samples, data on sdb
```

### 12.2 Report (`report_md.py`, `report_html.py`)

- Header facts (`report_common.header_facts`): add `Databases` with the labels, only when there are instances.
- Summary and Findings: unchanged mechanics; D codes flow through `finding_markdown` / `finding_block`; the `database` lines render under the process lines; links render as `See also: D1 pg:5432`.
- New section **Databases**, placed after Findings and before Transient events, present only when `host.databases` is non-empty. Per instance:
  - Facts: engine and version, source, locality and basis, collector and schema, samples and interval, window covered, `server_start`, resets in the window.
  - File placement: role, path, device chain (`report_common.device_chain`), media class.
  - Activity summary: median and p95 of `db.statements_ps`/`db.transactions_ps`, `db.sessions_active`, each `db.wait.*` category, `db.read_latency_ms`, `db.commit_latency_ms`, `db.cache_hit_pct`, with basis (time-averaged or sampled).
  - Recorded settings (the `setting.*` keys).
  - Charts (section 12.3).
  - Coverage lines for the instance (section 12.5).
- `report_common.charts_for`: D codes take their instance's DB charts; H codes with database context add the linked instance's `db_latency_` chart.

### 12.3 Charts (`dbcharts.py`)

| Name | Content | When |
|------|---------|------|
| `db_activity_<short>` | statements/s or transactions/s, active sessions on a second axis | instance has either |
| `db_waits_<short>` | stacked area of `db.wait.*` by category, with `cpu` at the bottom; host CPU count as a reference line for local instances | any wait series |
| `db_latency_<short>` | `db.read_latency_ms` and `db.commit_latency_ms`, with the mapped data device's `disk.r_await_ms` and log device's `disk.w_await_ms` on the same axis, threshold lines from the indicator | latency series exist; device overlay only for local, mapped instances |
| `db_cache_<short>` | `db.cache_hit_pct` and physical reads/s | cache series exists |
| `db_processes_<short>` | CPU % and IO kB/s of the instance's processes by role, from pidstat | process instances, or collector instances with process keys |

Same look as `charts.py` (`SERIES_COLORS`, `finish`, `episode_bands`, gaps broken with `with_gaps`). Stacked area uses `axes.stackplot`. The waits chart caption says when the series are sampled and, for SQL Server, that parallel tasks are counted individually.

### 12.4 findings.json

`SCHEMA = 2`. Additive:

```json
"databases": [
  {
    "id": "postgresql:localhost:5432",
    "short": "pg:5432",
    "engine": "postgresql",
    "label": "PostgreSQL 16.4 (localhost:5432)",
    "source": "collector",
    "local": true,
    "version_num": 160004,
    "collector_schema": 3,
    "devices": {"data": ["nvme1n1"], "wal": ["nvme1n1"]},
    "resets": [],
    "settings": {"track_io_timing": "on"},
    "notes": []
  }
]
```

Each hypothesis gains `database_context` and `links`. `databases` is `[]` when nothing was detected. `docs/perfdiag.md` documents the schema 2 additions.

### 12.5 Coverage lines

Per instance, only when they apply:

- `PostgreSQL pg:5432: track_io_timing is off, so read latency was not measured.`
- `PostgreSQL pg:5432: wait events are sampled every 15 s, not averaged; short waits between samples are not visible.`
- `MySQL my:3306: performance_schema is off, so InnoDB file IO latency was not measured.`
- `PostgreSQL pg:5432: collected with collect-postgresql-metrics.sh 2.0.0. Database counters cover the postgres database only; waits, IO timing and file placement are not available. Re-run with 3.0.0.`
- `Oracle ORCL/PDB1: connected to a PDB; instance-wide figures need a CDB$ROOT connection.`
- `SQL Server MSSQLSERVER: file paths are inside the container and were mapped to host devices through the mount table only when the paths matched.`
- `mysqld: process data only. Database-level findings need collect-mysql-metrics.sh.`
- `PostgreSQL pg:5432: statistics reset or server restart at 14:02; rates are blank for that sample.`

### 12.6 inspect

After `sadf activities`, per instance: label, source, local/basis, runs, samples and interval, files present (`pg_waits.csv: yes`), devices by role, and notes. Nothing when there are no instances.

---

## 13. Tests

Existing style: `unittest`, no pytest, synthetic data plus small real fixtures, matplotlib-dependent tests skipped without it.

### 13.1 Collector tests (`tests/test_db_collectors.py`)

Fake clients on `PATH` (`tests/fakeclients/psql`, `mysql`, `mariadb`, `sqlplus`, `sqlcmd`): small bash scripts that print canned output selected by environment variables (`FAKE_PG_VERSION=90300`, `FAKE_SQLPLUS_RELEASE=11.2`, `FAKE_FAIL=timeout`) and log their arguments.

- Header and field counts match for each engine and version gate (PostgreSQL 9.3, 9.6, 10, 14, 16, 17, 18; MySQL 5.5, 8.0.21, 8.0.22, 8.4; MariaDB 10.4, 11.4; SQL\*Plus 11.2 and 19; SQL Server 2019 and 2022).
- `psql` is called with `-X` and `ON_ERROR_STOP`; a fake `~/.psqlrc` with `\timing on` does not change the CSV.
- MariaDB: when `mysql` is a symlink to `mariadb`, `mariadb` is invoked and `errors.log` stays empty.
- `--host localhost --port 3307` adds `--protocol=TCP`.
- Oracle: a 15-digit counter arrives intact.
- Routing: an unexpected record letter and a wrong field count go to `errors.log`, not the CSV.
- Timeout: a fake client that sleeps past the timeout logs `sample timed out` and the next sample runs on schedule.
- Locality: socket, loopback, server hostname and remote cases.
- Manifest: `collector_schema=3`, `db_engine`, `target_local`, `setting.*`, no secrets.
- `shellcheck` on all four scripts.

### 13.2 gather and inventory

- `test_gather.py`: a DB run in a fake `~/perf-diag` of `SUDO_USER` is bundled; an unreadable base produces the note; `--base` twice uses exactly those two.
- `test_hw_inventory.py`: `HW_INVENTORY_PROC` fake entries for each engine produce the `dbproc.*` keys; unreadable `cwd` gives the note.

### 13.3 perfdiag

`tests/synth_db.py` writes v3 run directories for each engine on the same timeline as `synth.py`, plus a v2 PostgreSQL run.

| Test | Expectation |
|------|-------------|
| No database anywhere | `host.databases == []`; text, Markdown and HTML contain no `Database` heading or line; findings.json has `"databases": []`; existing report tests unchanged |
| pidstat with `postgres:` titles only | one process instance; H1 on a device gets database context with the checkpointer role; next steps include the collector hint |
| `dbproc.1.cwd` + inventory filesystems | data mapped to the right device, through dm and a partition |
| PostgreSQL checkpoint write bursts + `disk_w_await` on the data device | H7 and D6 both raised, linked both ways, requested-checkpoint fact present |
| MySQL lock waits on an idle host | D3 ranked first; no H hypothesis; idle-host fact present |
| Oracle `db file sequential read` high, device await normal | D1 with the "above the block layer" note |
| SQL Server file stats latency on one device | `db.read_latency_ms` per device matches; D1 links to H1 on that device only |
| Remote PostgreSQL target, OS data present | D hypotheses present, no OS evidence attached, remote note present |
| DB-only bundle (no os run) | host analysed; DB sections only; coverage says no OS data |
| v2 PostgreSQL run | loads; coverage line for 2.0.0 |
| Counter reset mid-run | NaN at that sample, reset listed, no negative rate, no episode spanning it |
| Quoted database name with a comma | read correctly |
| Readers: wrong field count rows | skipped and counted |

---

## 14. Documentation

- `docs/collect-*-metrics.md`: new columns and files, version gates, privileges, locality, settings, `db_paths.txt`, Oracle licensing statement, SQL Server permission by version, MySQL `sudo` note, PostgreSQL `track_io_timing`, the 3.0.0 change list (cluster-wide PostgreSQL counters, client-backend-only connection counts).
- `docs/linux-cli-standard.md`: manifest keys from section 2.1, `collector_schema`, record-prefix routing, per-sample timeout, DB output location and gather bases.
- `docs/gather-perf-diag.md`: search bases, repeatable `--base`, DB detection notes.
- `docs/collect-hw-inventory.md`: `dbproc.*` keys.
- `docs/perfdiag.md`: new "Databases" section (detection sources, what appears and when, locality), D1–D9 next to H1–H12, DB settings in Configuration, findings.json schema 2, DB charts.
- `linux/analysis/README.md`: new modules in the layout block.

---

## 15. Build order

| Phase | Deliverable | Depends on |
|-------|-------------|------------|
| 1 | Collector defect fixes that keep the v2 output contract: P2 (`-X`), P3 (version gates), P4 (client backends), P7 (socket default), M1 (client choice), M2 (`--protocol=TCP`), O1/O2 (`TO_CHAR` rows, no `MARKUP CSV`), A4 (timeouts), A7 (field count check). Release as 2.1.0 | — |
| 2 | Collector v3 output (sections 2–6), docs for the collectors | 1 |
| 3 | `gather-perf-diag.sh` bases and detection, `collect-os-metrics.sh` detection note, `collect-hw-inventory.sh` `dbproc.*` | — (parallel with 2) |
| 4 | perfdiag loading: `readers/dbcsv.py`, `dbdetect.py`, `dbengines.py`, `dbload.py`, model and loader changes, path mapping, `inspect` output, DB-only hosts | 2, 3 (can start on `synth_db.py` output) |
| 5 | `dbrules.py`, settings, `dbhypotheses.py`, cross-links, next-steps table, text output | 4 |
| 6 | `dbcharts.py`, `report_db.py`, report and JSON changes | 5 |
| 7 | `docs/perfdiag.md`, README, test matrix against live servers (section 16) | 6 |

Phase 1 is small and fixes data that is wrong today. Phase 4 alone already delivers detection and the "Databases" section with process data; phases 5 and 6 add findings.

---

## 16. Verify on live servers before release

Run against containers (`podman run`) for: PostgreSQL 9.6, 12, 16, 17, 18; MySQL 8.0, 8.4; MariaDB 10.6, 11.4; SQL Server 2019, 2022 and 2025 (`mcr.microsoft.com/mssql/server`); Oracle Database Free 23ai. Oracle 19c and an 11.2 SQL\*Plus client need a VM or an existing estate.

| Item | Check |
|------|-------|
| O1 | SQL\*Plus prints a bare `NUMBER` above 10 digits in scientific notation with the 2.0.0 settings (with and without `MARKUP CSV ON`). Confirms the severity of O1 |
| `TM9` | `TO_CHAR(12345678901234, 'TM9')` on 11.2, 19c, 23ai prints all digits |
| Oracle scope | `v$sysstat`, `v$sys_time_model`, `v$system_event` scope when connected to a PDB versus `CDB$ROOT` on 19c and 23ai |
| `v$system_wait_class` | `TIME_WAITED_FG` exists on 11.2 and 19c and is centiseconds |
| M1 | MariaDB 11.4 deprecation line goes to stderr (and not stdout) when invoked as `mysql` |
| MySQL redo events | `file_summary_by_event_name` event names for redo on 8.0.30+ (`#innodb_redo` files) and on MariaDB |
| PostgreSQL 18 | `pg_stat_io` `writes`/`fsyncs` columns for `client backend`/`relation`, and that `pg_stat_wal` has `wal_records, wal_fpi, wal_bytes, wal_buffers_full` |
| PostgreSQL 9.6 | `backend_type` absent; autovacuum filter by `query` works |
| SQL Server `cntr_type` | `Log Flush Wait Time` and `Lock Wait Time (ms)` are 272696576 (cumulative) |
| SQL Server on Linux | `MachineName` in a container; `sys.master_files.physical_name` form (`/var/opt/mssql/data/...`) |
| Collector overhead | each engine at `-i 1` for 10 minutes: collector CPU under 2 % of one core on the server |
