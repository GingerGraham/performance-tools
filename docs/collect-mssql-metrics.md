# collect-mssql-metrics.sh

Samples SQL Server performance counters at a fixed interval and writes them to a CSV file. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load. The script runs on Linux and can collect from a SQL Server on Linux or Windows.

Location: `linux/db/collect-mssql-metrics.sh`

## Requirements

- `sqlcmd`. The script looks on `PATH`, then in `/opt/mssql-tools18/bin` and `/opt/mssql-tools/bin`. It is not in the standard Fedora or RHEL repositories. Install it from Microsoft's package repository (see [sqlcmd setup](https://learn.microsoft.com/sql/linux/sql-server-linux-setup-tools)); the script prints the commands if `sqlcmd` is missing.
- A login with `VIEW SERVER STATE` (`VIEW SERVER PERFORMANCE STATE` on SQL Server 2022 and later), which is needed to read `sys.dm_os_performance_counters`.
- For SQL logins, the password in `SQLCMDPASSWORD`. For trusted (AD) authentication, omit `--user`.

The script does not need root.

## Quick start

```bash
SQLCMDPASSWORD='secret' ./collect-mssql-metrics.sh -d 2h -i 30s --host db1 --user monitor
```

The script checks the connection, takes the first sample while attached to your terminal (so an error is visible straight away), then detaches and continues in the background.

## Options

Long options accept `--opt value` and `--opt=value`. Short options are not bundled.

| Option | Meaning | Default |
|--------|---------|---------|
| `-d`, `--duration` | Total run time. Units `s`, `m`, `h`, `d`. A bare number is seconds | Prompted for |
| `-c`, `--count` | Number of samples. Cannot be combined with `-d` | Prompted for duration |
| `-i`, `--interval` | Time between samples. Units `s`, `m`. Minimum 1 second | Prompted for |
| `-o`, `--outdir` | Base directory for output | `/var/log/perf-diag`, or `~/perf-diag` if that is not writable |
| `--host` | Server: `host`, `host,port` or `host\instance` | `localhost` |
| `--port` | Port, 1 to 65535. Added to `--host` as `host,port` | Not set |
| `--user` | SQL login. Omit to use trusted / AD authentication | Not set |
| `--database` | Database to connect to | `master` |
| `--foreground` | Stay attached. Ctrl-C stops collection | Detach after the first sample |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`--port` cannot be combined with a `--host` that already has a port or an instance name. Put the port in `--host` instead (`--host 'db1,1434'`).

The removed options `-S`, `-U` and `-n` name their replacements. `-P` is refused: set `SQLCMDPASSWORD` instead.

### Timing

- Number of samples = `duration / interval`, rounded down. With `-c`, the run time is `count × interval`.
- Duration must be at least one interval.
- If duration/count or interval is missing and the script has a terminal, it asks. Without a terminal (cron, CI), a missing value is a usage error.
- Sample *n* is taken at `start + n × interval`. Time spent in a query does not push later samples back. The first sample is taken immediately.

### Authentication

A password is never accepted as an option, because it would show in `ps` and shell history.

| Case | What to do |
|------|------------|
| SQL login | Pass `--user` and set `SQLCMDPASSWORD` in the environment |
| Trusted / AD | Omit `--user`. `sqlcmd` then uses integrated authentication, which on Linux needs a valid Kerberos ticket (`kinit`) |

```bash
read -rs SQLCMDPASSWORD && export SQLCMDPASSWORD     # type it without echo
./collect-mssql-metrics.sh -d 1h -i 30s --host db1 --user monitor
```

### Encryption

When the installed `sqlcmd` supports it (`mssql-tools18` and the Go `sqlcmd`), the script adds `-C -N`: the connection is encrypted and the server certificate is trusted without being validated. The older `mssql-tools` `sqlcmd` does not accept these switches, so they are left off and a `NOTE:` is printed.

### Database

`--database` sets the database the connection starts in. The counters collected are server-wide, so the choice does not change the data. The database must exist and the login must be able to connect to it.

## Output

```
<outdir>/<hostname>_mssql_<YYYYmmdd_HHMMSS>/
    mssql_stats.csv    the samples
    manifest.txt       what was run and where
    errors.log         query errors (empty when all is well)
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error.

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `target` (`[user@]server/database`), `client_version`, `server_version` (`ProductVersion`) and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used.

Columns ending `_total` are **cumulative counters** since server start. SQL Server's own per-second counters are cumulative in this view, so the script keeps them as they are and names them `_total`. Work out the rate as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the server restarts, so a sample lower than the one before it marks that point.

All other columns are values at the moment of sampling.

| Column | Type | Meaning |
|--------|------|---------|
| `buffer_cache_hit_ratio` | value | Buffer cache hit ratio as a percentage (counter divided by its base) |
| `page_life_expectancy` | value | Seconds a page stays in the buffer pool |
| `memory_grants_pending` | value | Queries waiting for a memory grant |
| `target_server_memory_kb` | value | Memory SQL Server wants to use |
| `total_server_memory_kb` | value | Memory SQL Server is using |
| `checkpoint_pages_total` | counter | Pages flushed by checkpoints |
| `lazy_writes_total` | counter | Buffers written by the lazy writer |
| `batch_requests_total` | counter | Batch requests received |
| `sql_compilations_total` | counter | Compilations |
| `sql_recompilations_total` | counter | Recompilations |

A counter that the server does not report is written as `NULL`.

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the output goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. `sqlcmd` reports errors on standard output. The script writes a line to the CSV only when the result is a row of numbers (or `NULL`), and anything else goes to `errors.log`. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or stopped by a signal |
| 1 | Runtime error: `sqlcmd` missing, connection failed, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `'sqlcmd' not found` | Install `mssql-tools18` from Microsoft's repository. SLES is not supported by Microsoft's packages; collect from a supported distro instead |
| `cannot connect to SQL Server at '...'` | The `sqlcmd` error is printed beneath it. Check the server name, port, firewall, login and certificate |
| `A user was given but SQLCMDPASSWORD is not set.` | Export `SQLCMDPASSWORD` before running |
| `queries are failing (see .../errors.log)` | The connection worked but the sample query failed. A missing `VIEW SERVER STATE` permission is the usual cause |
| `--port cannot be combined with a --host that already has a port or instance name` | Remove `--port` and use `--host 'host,port'` |

## Examples

```bash
# Default instance on a remote host, SQL login
SQLCMDPASSWORD='secret' ./collect-mssql-metrics.sh -d 2h -i 30s --host db1 --user monitor

# Non-default port
SQLCMDPASSWORD='secret' ./collect-mssql-metrics.sh -d 1h -i 15s --host db1 --port 1434 --user monitor

# Named instance with AD authentication (valid Kerberos ticket required)
./collect-mssql-metrics.sh -d 1h -i 15s --host 'db1\PROD'

# Quick test, attached
./collect-mssql-metrics.sh -c 5 -i 1 --foreground --host localhost --user sa
```
