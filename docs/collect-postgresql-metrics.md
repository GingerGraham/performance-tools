# collect-postgresql-metrics.sh

Samples PostgreSQL statistics at a fixed interval and writes them to a CSV file. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load.

Location: `linux/db/collect-postgresql-metrics.sh`

## Requirements

- The `psql` client. On Fedora and RHEL family: `sudo dnf install postgresql`. On RHEL, Rocky and Alma the package can be versioned, for example `postgresql16`.
- Network access to the server and a login that can run `SELECT` on `pg_stat_database` and `pg_stat_activity`.
- A way to authenticate without a prompt: `PGPASSWORD`, `~/.pgpass`, or a `trust`/certificate rule on the server. See [Authentication](#authentication).
- Any server version. PostgreSQL 17 and later is handled automatically (see [Columns](#columns)).

The script does not need root. Run it as the user whose `~/.pgpass` you want it to read. Do not run it with `sudo`: that changes `$USER` and the home directory used for `~/.pgpass`.

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
| `--host` | Server host name or address | `localhost` |
| `--port` | Server port, 1 to 65535 | `5432` |
| `--user` | Login name | Current OS user |
| `--database` | Database to connect to and report on | `postgres` |
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

- The default host `localhost` makes `psql` connect over TCP, so `pg_hba.conf` needs a matching `host` line. To use the Unix socket instead, pass the socket directory as the host: `--host /var/run/postgresql`.
- Connection attempts time out after 5 seconds.

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

The counters come from `pg_stat_database` for the database you connect to, and are readable by any login. The connection counts come from `pg_stat_activity`. A login without `pg_read_all_stats` cannot see the `state` of other users' sessions, so their sessions are not counted and the connection columns under-report.

For a dedicated monitoring login, grant the built-in role:

```sql
CREATE ROLE monitor LOGIN PASSWORD '...';
GRANT pg_monitor TO monitor;
```

## Output

```
<outdir>/<hostname>_postgresql_<YYYYmmdd_HHMMSS>/
    pg_stats.csv       the samples
    manifest.txt       what was run and where
    errors.log         query errors (empty when all is well)
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error.

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `target` (`user@host:port/database`), `client_version`, `server_version` and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601 (`date -Iseconds`). The server clock is not used.

Almost every column is a **cumulative counter**: it only goes up, so the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. The exceptions are the three connection columns, which are values at the moment of sampling.

| Column | Type | Meaning |
|--------|------|---------|
| `xact_commit`, `xact_rollback` | counter | Transactions committed and rolled back |
| `blks_read` | counter | Disk blocks read (not found in shared buffers) |
| `blks_hit` | counter | Blocks found in shared buffers |
| `tup_returned`, `tup_fetched` | counter | Rows returned by scans, rows fetched by queries |
| `tup_inserted`, `tup_updated`, `tup_deleted` | counter | Rows written |
| `temp_files`, `temp_bytes` | counter | Temporary files created and their size |
| `deadlocks` | counter | Deadlocks detected |
| `bgwriter_checkpoints_timed` | counter | Scheduled checkpoints (cluster-wide) |
| `bgwriter_checkpoints_req` | counter | Requested checkpoints, for example when WAL fills (cluster-wide) |
| `bgwriter_buffers_checkpoint` | counter | Buffers written by checkpoints (cluster-wide) |
| `bgwriter_buffers_clean` | counter | Buffers written by the background writer (cluster-wide) |
| `active_connections` | value | Sessions in state `active` |
| `idle_connections` | value | Sessions in state `idle` |
| `waiting_connections` | value | Active sessions that have a wait event |

- The `xact_*`, `blks_*`, `tup_*`, `temp_*` and `deadlocks` columns are for the `--database` you connected to only. The `bgwriter_*` columns and the three connection columns cover the whole server, all databases.
- The column names stay the same on every server version. On PostgreSQL 17 and later the checkpoint figures are read from `pg_stat_checkpointer` instead of `pg_stat_bgwriter`.
- Counters reset when the server restarts or statistics are reset. A sample lower than the one before it marks that point.

## Turning the counters into rates

With `pandas`:

```python
import pandas as pd

samples = pd.read_csv("pg_stats.csv", parse_dates=["ts"], index_col="ts")

seconds_between_samples = samples.index.to_series().diff().dt.total_seconds()
counter_columns = [column for column in samples.columns if not column.endswith("_connections")]

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
| `... is not writable; run with sudo or pass a different -o <dir>` | Choose a directory you own with `-o` |
| `-p was removed; use --port` (also `-U`, `-w`) | Options from earlier versions. `-w` is now `--database`, `-U` is `--user` |

## Examples

```bash
# Local server, 30 minutes at 10-second samples (prompts nothing)
./collect-postgresql-metrics.sh -d 30m -i 10s

# Remote server with a specific database, output to a chosen directory
./collect-postgresql-metrics.sh -d 2h -i 30s --host db1.example.com --database orders --user monitor -o ~/runs

# Local Unix socket
./collect-postgresql-metrics.sh -c 120 -i 5 --host /var/run/postgresql

# Quick test, attached
./collect-postgresql-metrics.sh -c 5 -i 1 --foreground
```
