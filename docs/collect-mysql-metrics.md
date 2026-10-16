# collect-mysql-metrics.sh

Samples MySQL or MariaDB status counters at a fixed interval and writes them to a CSV file. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load.

Location: `linux/db/collect-mysql-metrics.sh`

## Requirements

- A MySQL or MariaDB command-line client. The script uses `mysql` if present, otherwise `mariadb`. On Fedora: `sudo dnf install mysql` (or `mariadb`).
- A login that can run `SHOW GLOBAL STATUS`.
- A way to authenticate without a prompt: `~/.my.cnf` or `MYSQL_PWD`. See [Authentication](#authentication).

The script does not need root. Do not run it with `sudo`: that changes the home directory used for `~/.my.cnf`.

## Quick start

```bash
./collect-mysql-metrics.sh -d 2h -i 30s --host db1.example.com --user monitor
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
| `--host` | Server host name or address | `localhost` |
| `--port` | Server port, 1 to 65535 | `3306` |
| `--user` | Login name | `root` |
| `--foreground` | Stay attached. Ctrl-C stops collection | Detach after the first sample |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`-h` is help, not host. `-h db1` is refused with a pointer to `--host`. `-P` and `-u` were removed; use `--port` and `--user`.

### Timing

- Number of samples = `duration / interval`, rounded down. With `-c`, the run time is `count × interval`.
- Duration must be at least one interval.
- If duration/count or interval is missing and the script has a terminal, it asks. Without a terminal (cron, CI), a missing value is a usage error.
- Sample *n* is taken at `start + n × interval`. Time spent in a query does not push later samples back. The first sample is taken immediately.

### Host and connection

The MySQL client treats the host name `localhost` as a request for the Unix socket, and ignores `--port` when it does. To force TCP to the local server, use `--host 127.0.0.1`.

### Authentication

A password is never accepted as an option, because it would show in `ps` and shell history. Use a client option file (preferred) or the environment:

```ini
# ~/.my.cnf  (chmod 600)
[client]
user=monitor
password=secret
```

```bash
MYSQL_PWD='secret' ./collect-mysql-metrics.sh -d 1h -i 30s --user monitor
```

## Output

```
<outdir>/<hostname>_mysql_<YYYYmmdd_HHMMSS>/
    mysql_stats.csv    the samples
    manifest.txt       what was run and where
    errors.log         query errors (empty when all is well)
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error.

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `target` (`user@host:port`), `client_version`, `server_version` and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used. Each other column is the `SHOW GLOBAL STATUS` variable of the same name. A variable the server does not report is left empty.

Most columns are **cumulative counters** since server start: the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the server restarts, so a sample lower than the one before it marks that point.

| Column | Type | Meaning |
|--------|------|---------|
| `Threads_connected` | value | Open client connections |
| `Threads_running` | value | Connections currently executing |
| `Open_files` | value | Files currently open |
| `Open_tables` | value | Tables currently open |
| `Innodb_row_lock_time_avg` | value | Average row lock wait since start, ms |
| `Questions` | counter | Statements sent by clients |
| `Slow_queries` | counter | Statements slower than `long_query_time` |
| `Com_select`, `Com_insert`, `Com_update`, `Com_delete` | counter | Statements by type |
| `Innodb_buffer_pool_read_requests` | counter | Logical reads from the buffer pool |
| `Innodb_buffer_pool_reads` | counter | Reads that had to go to disk |
| `Innodb_row_lock_waits` | counter | Times a row lock had to be waited for |
| `Innodb_row_lock_time` | counter | Total time spent waiting for row locks, ms |
| `Created_tmp_tables` | counter | Internal temporary tables created |
| `Created_tmp_disk_tables` | counter | Internal temporary tables created on disk |
| `Aborted_connects` | counter | Failed connection attempts |
| `Aborted_clients` | counter | Connections closed without a proper close |

Buffer pool hit ratio over an interval = `1 - (change in Innodb_buffer_pool_reads / change in Innodb_buffer_pool_read_requests)`.

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
| 1 | Runtime error: client missing, connection failed, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `no MySQL/MariaDB client ('mysql' or 'mariadb') found` | Install a client. The script prints the command for your distro |
| `cannot connect to MySQL/MariaDB at host:port as user` | The client error is printed beneath it. Check host, port, user, grants and the password source |
| `queries are failing (see .../errors.log)` | The connection worked but `SHOW GLOBAL STATUS` failed. The first error line is shown |
| `... is not writable; run with sudo or pass a different -o <dir>` | Choose a directory you own with `-o` |

## Examples

```bash
# Local server over the socket, 30 minutes at 10-second samples
./collect-mysql-metrics.sh -d 30m -i 10s

# Remote server
./collect-mysql-metrics.sh -d 2h -i 30s --host db1.example.com --user monitor

# Local server over TCP on a non-default port
./collect-mysql-metrics.sh -c 120 -i 5 --host 127.0.0.1 --port 3307

# Quick test, attached
./collect-mysql-metrics.sh -c 5 -i 1 --foreground
```
