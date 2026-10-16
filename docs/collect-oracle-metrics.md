# collect-oracle-metrics.sh

Samples Oracle Database system statistics at a fixed interval and writes them to a CSV file. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load.

Location: `linux/db/collect-oracle-metrics.sh`

## Requirements

- SQL\*Plus (`sqlplus`) from Oracle Instant Client, with `sqlplus` on `PATH` and the Instant Client libraries on `LD_LIBRARY_PATH` if you installed from the zip files. It is not in the standard Fedora or RHEL repositories. The script prints the download page and distro-specific notes if it is missing.
- A connect string for a login that can read `v$sysstat`, `v$session` and `v$version`. The `SELECT_CATALOG_ROLE` role gives this, or grant `SELECT` on the three views.

The script does not need root.

## Quick start

```bash
export ORACLE_CONNECT_STRING='monitor/secret@//db1:1521/ORCL'
./collect-oracle-metrics.sh -d 2h -i 30s
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
| `--connect` | Connect string | `$ORACLE_CONNECT_STRING` |
| `--foreground` | Stay attached. Ctrl-C stops collection | Detach after the first sample |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`-c` is the sample count. Earlier versions used it for the connect string, so `-c 'user/pw@//h/s'` is a usage error. The error does not print the rejected value, because it may be a password.

### Timing

- Number of samples = `duration / interval`, rounded down. With `-c`, the run time is `count × interval`.
- Duration must be at least one interval.
- If duration/count or interval is missing and the script has a terminal, it asks. Without a terminal (cron, CI), a missing value is a usage error.
- Sample *n* is taken at `start + n × interval`. Time spent in a query does not push later samples back. The first sample is taken immediately.

### Connect string

Any string accepted by the SQL\*Plus `CONNECT` command works: `user/password@//host:1521/service_name`, or `user/password@tns_alias`.

Prefer the environment variable. An option value is visible to other users in the process list and stays in shell history.

The script gives the string to SQL\*Plus on standard input, not on the command line, so it does not appear in `ps` either way. It is also replaced with `<redacted>` in the `command_line=` entry of the manifest, and no `target=` entry is written.

## Output

```
<outdir>/<hostname>_oracle_<YYYYmmdd_HHMMSS>/
    oracle_stats.csv   the samples
    manifest.txt       what was run
    errors.log         query errors (empty when all is well)
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error.

`manifest.txt` is `key=value`: script and version, command line (with the connect string redacted), host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `client_version`, `server_version` (the `v$version` banner) and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used.

All columns except `active_sessions` are **cumulative counters** from `v$sysstat`: the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the instance restarts, so a sample lower than the one before it marks that point.

| Column | `v$sysstat` statistic | Type |
|--------|-----------------------|------|
| `physical_reads` | `physical reads` | counter |
| `physical_writes` | `physical writes` | counter |
| `session_logical_reads` | `session logical reads` | counter |
| `redo_size` | `redo size` (bytes) | counter |
| `user_calls` | `user calls` | counter |
| `db_block_gets` | `db block gets` | counter |
| `consistent_gets` | `consistent gets` | counter |
| `parse_count_total` | `parse count (total)` | counter |
| `parse_count_hard` | `parse count (hard)` | counter |
| `active_sessions` | Rows in `v$session` with `STATUS = 'ACTIVE'` | value |

`active_sessions` includes Oracle background sessions. All figures are for the instance you connect to: the `v$` views do not span RAC nodes.

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the output goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. SQL\*Plus reports errors on standard output. The script writes a line to the CSV only when the result is a row of numbers, and anything else goes to `errors.log`. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or stopped by a signal |
| 1 | Runtime error: `sqlplus` missing, no connect string, connection failed, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `'sqlplus' not found` | Install Instant Client Basic and SQL\*Plus and set `PATH` and `LD_LIBRARY_PATH` |
| `no connect string supplied` | Set `ORACLE_CONNECT_STRING` or pass `--connect` |
| `sqlplus could not connect` / `Oracle connection failed` | The SQL\*Plus output (`ORA-` or `SP2-` errors) is printed beneath it |
| `queries are failing (see .../errors.log)` | The connection worked but the sample query failed. A missing `SELECT` grant on a `v$` view is the usual cause |
| `-c/--count must be a positive integer; connect strings now use --connect` | An old-style `-c` connect string, or a bad count |

## Examples

```bash
# Environment variable, 30 minutes at 10-second samples
ORACLE_CONNECT_STRING='monitor/secret@//db1:1521/ORCL' ./collect-oracle-metrics.sh -d 30m -i 10s

# Explicit option (visible in ps and shell history)
./collect-oracle-metrics.sh --connect 'monitor/secret@//db1:1521/ORCL' -c 120 -i 5

# Quick test, attached
./collect-oracle-metrics.sh -c 5 -i 1 --foreground
```
