# collect-postgresql-metrics.sh

Samples PostgreSQL statistics at a fixed interval and writes them to CSV files. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load. [gather-perf-diag.sh](gather-perf-diag.md) bundles both, and [perfdiag](perfdiag.md) analyses them together.

Location: `linux/db/collect-postgresql-metrics.sh`

## Requirements

- The `psql` client. On Fedora and RHEL family: `sudo dnf install postgresql`. On RHEL, Rocky and Alma the package can be versioned, for example `postgresql16`.
- Network access to the server (or a local socket) and a login that can read the statistics views. See [Permissions on the server](#permissions-on-the-server).
- A way to authenticate without a prompt: `PGPASSWORD`, `~/.pgpass`, or a `trust`/`peer`/certificate rule on the server. See [Authentication](#authentication).
- PostgreSQL 9.2 or later. Older servers are refused. Version differences are handled automatically (see [Version gates](#version-gates)).

The script does not need root. Run it as the user whose `~/.pgpass` you want it to read. Do not run it with `sudo`: that changes `$USER` and the home directory used for `~/.pgpass`. A run started this way writes to `~/perf-diag`, and `gather-perf-diag.sh` finds it there (see [Output](#output)).

## Quick start

```bash
./collect-postgresql-metrics.sh -d 2h -i 30s --host db1.example.com --user monitor
```

The script checks the connection, takes the first sample while attached to your terminal (so a permission or query error is visible straight away), then detaches and continues in the background.

## Options

Long options accept `--opt value` and `--opt=value`. Short options are not bundled.

| Option | Meaning | Default |
|--------|---------|---------|
| `-d`, `--duration` | Total run time. Units `s`, `m`, `h`, `d`. A bare number is seconds | Prompted for |
| `-c`, `--count` | Number of samples. Cannot be combined with `-d` | Prompted for duration |
| `-i`, `--interval` | Time between samples. Units `s`, `m`. Minimum 1 second | Prompted for |
| `-o`, `--outdir` | Base directory for output | `/var/log/perf-diag`, or `~/perf-diag` if that is not writable |
| `--host` | Server host name, address or socket directory | The local Unix socket |
| `--port` | Server port, 1 to 65535 | `5432` |
| `--user` | Login name | Current OS user |
| `--database` | Database to connect to. Counters cover every database | `postgres` |
| `--foreground` | Stay attached. Ctrl-C stops collection | Detach after the first sample |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`-h` is help, not host. `-h db1` is refused with a pointer to `--host`.

### Timing

- Number of samples = `duration / interval`, rounded down. With `-c`, the run time is `count × interval`.
- Duration must be at least one interval.
- If duration/count or interval is missing and the script has a terminal, it asks. Without a terminal (cron, CI), a missing value is a usage error.
- Sample *n* is taken at `start + n × interval`. Time spent in a query does not push later samples back. The first sample is taken immediately.

### Host and connection

- With no `--host` the script connects over the local Unix socket, so `peer` authentication works for the login that matches your OS user. Pass `--host localhost` for TCP, in which case `pg_hba.conf` needs a matching `host` line. A directory such as `--host /var/run/postgresql` selects a socket directory.
- Connection attempts time out after 5 seconds.
- Every sample has a time limit just under the interval (interval minus 2 seconds, minus 1 for intervals of 3 to 6 seconds, 1 second for shorter ones). A query that runs longer is cancelled by the server (`statement_timeout`) and by `timeout(1)` on the client side, and `errors.log` records `sample timed out after N s`. A hung server therefore leaves a gap instead of stalling the schedule.
- The script runs `psql -X`, so a `~/.psqlrc` with `\timing` or `\x` cannot change the output.

### Authentication

A password is never accepted as an option, because it would show in `ps` and shell history. Use one of the standard `libpq` methods:

```bash
# Environment variable (this shell only)
PGPASSWORD='secret' ./collect-postgresql-metrics.sh -d 1h -i 30s --user monitor

# or ~/.pgpass, mode 0600:  host:port:database:user:password
echo 'db1.example.com:5432:postgres:monitor:secret' >> ~/.pgpass
chmod 600 ~/.pgpass
```

## Permissions on the server

On PostgreSQL 10 and later one grant covers every query the script runs:

```sql
CREATE ROLE monitor LOGIN PASSWORD '...';
GRANT pg_monitor TO monitor;
```

On 9.2 to 9.6 there is no `pg_monitor`. Use a superuser, or accept that `pg_stat_activity` hides the `state` and wait columns of other users' sessions. The connection and wait figures then under-report, and the script adds a `NOTE:` when the login is neither a superuser nor a member of `pg_monitor`.

`pg_monitor` also includes `pg_read_all_settings`, which the script needs for `data_directory`. Without it the file locations cannot be recorded: `NOTE: data_directory is not readable for this login (grant pg_monitor); database files cannot be placed on devices`.

Read and write latency come from `blk_read_time` and `blk_write_time`, which stay at 0 while `track_io_timing` is off. The script notes that case. To turn it on after checking the timer cost with `pg_test_timing`:

```sql
ALTER SYSTEM SET track_io_timing = on;
SELECT pg_reload_conf();
```

## Output

```
<outdir>/<hostname>_postgresql_<YYYYmmdd_HHMMSS>/
    pg_stats.csv       one summary row per sample, cluster-wide
    pg_database.csv    one row per database per sample
    pg_waits.csv       active sessions by wait event (9.6 and later)
    db_paths.txt       where the server keeps its files (server on this host only)
    manifest.txt       what was run and where, plus recorded server settings
    errors.log         query errors (empty when all is well)
    sample.sql         the statements run for each sample
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error. `gather-perf-diag.sh` looks in `/var/log/perf-diag` and in `~/perf-diag` of the invoking user, `SUDO_USER` and the `postgres`, `mysql`, `oracle` and `mssql` accounts, so a run in the home directory is still bundled.

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `target` (`user@host:port/database`), `client_version`, `server_version`, the database keys below, `setting.*` lines and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

| Key | Value |
|-----|-------|
| `collector_schema` | `3`. A manifest without it comes from a 2.x collector |
| `db_engine` | `postgresql` |
| `db_instance` | `<socket directory>:<port>` or `<host>:<port>`, no secrets |
| `server_version_num` | `server_version_num`, for example `160004` |
| `server_hostname` | `inet_server_addr()`; empty on a socket connection |
| `target_local` | `yes`, `no` or `unknown`: whether the server runs on this host |
| `target_local_basis` | Why: `socket`, `loopback`, `server hostname`, `local address` or `none` |
| `server_start` | `pg_postmaster_start_time()`, for display |
| `query_timeout_s` | The per-sample time limit in force |
| `files` | The data files this run wrote |
| `setting.<name>` | `block_size`, `shared_buffers`, `max_connections`, `track_io_timing`, `checkpoint_timeout`, `max_wal_size` (`checkpoint_segments` before 9.5), `checkpoint_completion_target`, `synchronous_commit`, `synchronous_standby_names`, `wal_sync_method`, `full_page_writes`, `effective_io_concurrency`, `work_mem`, `temp_tablespaces`, `data_directory`. Memory values are shown as sizes, for example `128 MB` |

A server counts as local when the connection uses the socket or a loopback address, or when one of the host's own addresses matches. A database in a container on this host, reached through a published port on `localhost`, is local (same kernel and disks). One reached on its container IP is not, and `target_local_basis` shows why. When the host cannot be matched and PostgreSQL gives no host name, the result is `unknown`.

`db_paths.txt` is `key=value`: `path.data.1`, `path.wal.1`, `path.temp.1` and `path.tablespace.N`. The data directory is mode 0700, so a monitoring login usually cannot resolve the `pg_wal` symlink. The unresolved path is then written with a `path.wal.1.note=` line. The file is written only when the server is local, because a remote server's paths mean nothing on this host.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601 (`date -Iseconds`). The server clock is not used.

Most columns are a **cumulative counter**: it only goes up, so the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. Gauges are values at the moment of sampling. Markers identify a server restart or a statistics reset.

Counters reset when the server restarts or statistics are reset. A sample lower than the one before it marks that point. A reset between two samples with net growth is invisible in the counters, which is why the two marker columns exist: a change in either value marks a restart or reset.

#### pg_stats.csv

The `xact_*`, `blks_*`, `tup_*`, `temp_*`, `deadlocks` and `blk_*_time_ms` columns are totals over every database in the cluster, whichever database `--database` connects to. `pg_database.csv` has the same counters per database.

| Column | Type | Meaning |
|--------|------|---------|
| `xact_commit`, `xact_rollback` | counter | Transactions committed and rolled back |
| `blks_read` | counter | Disk blocks read (not found in shared buffers) |
| `blks_hit` | counter | Blocks found in shared buffers |
| `tup_returned`, `tup_fetched` | counter | Rows returned by scans, rows fetched by queries |
| `tup_inserted`, `tup_updated`, `tup_deleted` | counter | Rows written |
| `temp_files`, `temp_bytes` | counter | Temporary files created and their size |
| `deadlocks` | counter | Deadlocks detected |
| `bgwriter_checkpoints_timed` | counter | Scheduled checkpoints |
| `bgwriter_checkpoints_req` | counter | Requested checkpoints, for example when WAL reaches `max_wal_size` |
| `bgwriter_buffers_checkpoint` | counter | Buffers written by checkpoints |
| `bgwriter_buffers_clean` | counter | Buffers written by the background writer |
| `active_connections` | gauge | Client sessions in state `active` |
| `idle_connections` | gauge | Client sessions in state `idle` |
| `waiting_connections` | gauge | Active client sessions that have a wait event |
| `blk_read_time_ms`, `blk_write_time_ms` | counter (ms) | Time spent reading and writing blocks. Zero while `track_io_timing` is off |
| `checkpoint_write_time_ms`, `checkpoint_sync_time_ms` | counter (ms) | Checkpoint write and sync time |
| `backend_buffer_writes`, `backend_buffer_fsyncs` | counter | Buffers and fsyncs done by client backends themselves, because the background writer and checkpointer did not keep up |
| `maxwritten_clean` | counter | Times the background writer stopped because it had written too many buffers |
| `buffers_alloc` | counter | Buffers allocated |
| `idle_in_transaction_connections` | gauge | Sessions `idle in transaction`, which hold locks and block vacuum |
| `total_connections` | gauge | All client sessions |
| `max_connections` | gauge | The server setting |
| `oldest_xact_age_s` | gauge | Age of the oldest open client transaction |
| `oldest_active_query_age_s` | gauge | Age of the longest running active query |
| `xid_age_max` | gauge | Largest transaction ID age of any database |
| `autovacuum_workers` | gauge | Running autovacuum workers |
| `wal_records`, `wal_fpi`, `wal_bytes`, `wal_buffers_full` | counter | WAL activity from `pg_stat_wal` (14 and later; empty before) |
| `wal_lsn_bytes` | counter | WAL write position in bytes. Empty during recovery |
| `in_recovery` | gauge | 1 on a standby |
| `replication_lag_s` | gauge | Standby: seconds since the last replayed transaction. Primary (10 and later): the largest `replay_lag`. Empty when neither applies |
| `server_start_marker` | marker | Server start time as epoch seconds |
| `stats_reset_marker` | marker | Latest statistics reset, as epoch seconds. Empty if never reset |

Connection columns count client backends only, excluding the collector's own session (it sets `application_name` to `perf-diag-collector`). Autovacuum workers and walsenders are not counted as connections.

#### pg_database.csv

`ts,datname,numbackends,xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,temp_files,temp_bytes,deadlocks,blk_read_time_ms,blk_write_time_ms`. One row per database. The name is double-quoted, so read the file with a CSV parser.

#### pg_waits.csv

`ts,wait_event_type,wait_event,sessions`. The number of active client sessions in each wait event at the sample instant. `CPU` means active with no wait event: on CPU, or in code that is not instrumented. A wait event with no row in a sample had no sessions. These are snapshots, not averages: a wait shorter than the interval can fall between samples.

### Version gates

Column names stay the same on every server version. Sources change underneath:

| Version | Change |
|---------|--------|
| below 9.6 | No `wait_event`: waiting uses the `waiting` flag and `pg_waits.csv` is not written |
| below 10 | No `backend_type`: autovacuum is recognised by its query text. WAL position comes from `pg_xlog_location_diff`. No replay lag on a primary |
| 14 | `pg_stat_wal` fills the `wal_*` columns |
| 16 | Backend buffer writes come from `pg_stat_io` |
| 17 | Checkpoint counters come from `pg_stat_checkpointer` |

### Changes in 3.0.0

- The `xact_*`, `blks_*`, `tup_*`, `temp_*` and `deadlocks` columns are cluster totals. Before they described the `--database` only, which with the default meant the `postgres` maintenance database.
- Connection columns count client backends only. Before they included the collector's own session and, on 10 and later, walsenders and autovacuum workers.
- The default host is the local Unix socket instead of `localhost`.
- `psql` runs with `-X`, so `~/.psqlrc` cannot add lines to the CSV. A row with the wrong number of fields, or any other unexpected output, goes to `errors.log` instead of the CSV.
- PostgreSQL 9.2 to 9.5 work. Before every sample query failed there.
- New files `pg_database.csv`, `pg_waits.csv` and `db_paths.txt`, the extra columns above and the manifest keys.

## Turning the counters into rates

With `pandas`:

```python
import pandas as pd

samples = pd.read_csv("pg_stats.csv", parse_dates=["ts"], index_col="ts")

seconds_between_samples = samples.index.to_series().diff().dt.total_seconds()
gauge_columns = [
    column for column in samples.columns
    if column.endswith("_connections") or column.endswith("_age_s") or column.endswith("_marker")
    or column in ("max_connections", "xid_age_max", "autovacuum_workers", "in_recovery", "replication_lag_s")
]
counter_columns = [column for column in samples.columns if column not in gauge_columns]

rates_per_second = samples[counter_columns].diff().div(seconds_between_samples, axis=0)
rates_per_second["cache_hit_pct"] = 100 * samples["blks_hit"].diff() / (
    samples["blks_hit"].diff() + samples["blks_read"].diff()
)
print(rates_per_second.round(2))
```

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the error goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or stopped by a signal |
| 1 | Runtime error: `psql` missing, connection failed, cannot read the server version, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `'psql' not found` | Install the client. The script prints the command for your distro |
| `cannot connect to PostgreSQL at host:port/database as user` | The `psql` error is printed beneath it. Check host, port, user, database, `pg_hba.conf` and the password source |
| `queries are failing (see .../errors.log)` | The connection worked but the sample query failed. The first error line is shown |
| `PostgreSQL 9.2 or later is needed` | The server is older than the script supports |
| `sample timed out after N s` in `errors.log` | The server did not answer within the per-sample limit. Check for locks or an overloaded server |
| `data_directory is not readable for this login` | Grant `pg_monitor` (10 and later) or use a superuser |
| `... is not writable; run with sudo or pass a different -o <dir>` | Choose a directory you own with `-o` |
| `-p was removed; use --port` (also `-U`, `-w`) | Options from earlier versions. `-w` is now `--database`, `-U` is `--user` |

## Examples

```bash
# Local server, 30 minutes at 10-second samples (prompts nothing)
./collect-postgresql-metrics.sh -d 30m -i 10s

# Remote server with a specific database, output to a chosen directory
./collect-postgresql-metrics.sh -d 2h -i 30s --host db1.example.com --database orders --user monitor -o ~/runs

# Local server over TCP instead of the default Unix socket
./collect-postgresql-metrics.sh -c 120 -i 5 --host localhost

# A specific socket directory
./collect-postgresql-metrics.sh -c 120 -i 5 --host /var/run/postgresql

# Quick test, attached
./collect-postgresql-metrics.sh -c 5 -i 1 --foreground
```
