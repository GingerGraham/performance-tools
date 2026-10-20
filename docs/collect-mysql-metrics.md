# collect-mysql-metrics.sh

Samples MySQL or MariaDB status counters at a fixed interval and writes them to CSV files, with a manifest that says which server was sampled and whether it runs on this host. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load. [gather-perf-diag.sh](gather-perf-diag.md) bundles the run and `perfdiag` analyses it with the OS data.

Location: `linux/db/collect-mysql-metrics.sh`

## Requirements

- A MySQL or MariaDB command-line client. See [Client choice](#client-choice). On Fedora: `sudo dnf install mysql` (or `mariadb`).
- A login that can run `SHOW GLOBAL STATUS`. For every column, see [Privileges](#privileges).
- A way to authenticate without a prompt: `~/.my.cnf` or `MYSQL_PWD`. See [Authentication](#authentication).

### Running as root, and sudo

The default login is `root`. On Debian and Ubuntu the MariaDB `root` account authenticates through `unix_socket`, which accepts only the operating system user `root`. For a local root login, run the script with `sudo`:

```bash
sudo ./collect-mysql-metrics.sh -d 2h -i 30s
```

This is the one database collector where `sudo` is the normal way to run it. Two things follow from it:

- `sudo` often changes `$HOME` to `/root`, so `~/.my.cnf` is read from there. To keep your own password, pass `MYSQL_PWD` with `sudo -E`, or use a dedicated monitoring login (below) and run without `sudo`.
- Under `sudo` the output goes to `/var/log/perf-diag`, where `gather-perf-diag.sh` looks.

A dedicated login with the [privileges](#privileges) below needs no `sudo`.

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
| `--socket` | Path of the server's Unix socket. Cannot be combined with `--host` or `--port` | Not used |
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
- Each client call is limited to a timeout just under the interval (interval minus 2 s above 6 s, minus 1 s above 2 s, otherwise 1 s). A call that exceeds it is stopped and `errors.log` gets `sample timed out after N s`, so one hung server cannot hold the schedule. The limit in force is `query_timeout_s` in the manifest. Connecting is limited to 5 seconds (`--connect-timeout=5`).

### Host, port and socket

The MySQL client treats the host name `localhost` as a request for the Unix socket. How the script passes your options on:

| You give | The client gets | Result |
|----------|-----------------|--------|
| Nothing, or `--host localhost` | `-h localhost` | The default Unix socket |
| `--host localhost --port 3307` | `-h localhost -P 3307 --protocol=TCP` | TCP to that port, with a `NOTE:` |
| `--host db1.example.com` | `-h db1.example.com -P 3306` | TCP |
| `--socket /run/mysqld/other.sock` | `--socket /run/mysqld/other.sock` | That socket |

The `--protocol=TCP` row matters on a host with several instances. Without it, the client would sample the instance behind the default socket whatever `--port` says. For a second instance, `--socket` is the simplest way to reach it. A `root` login through TCP does not work on Debian and Ubuntu (see above), so use a monitoring login for the TCP rows.

Giving `--socket` together with `--host` or `--port` is a usage error, because the client would ignore one of them.

### Client choice

The script runs `mariadb` when the `mysql` command is MariaDB's client (the usual case on MariaDB hosts, where `mysql` is a link to `mariadb`). Otherwise it runs `mysql`, and if only `mariadb` exists it runs that. MariaDB 11 prints `Deprecated program name` on every call when its client is started under the name `mysql`, and the script no longer triggers that message. `client_version` in the manifest names the program used.

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

No password is written to the manifest, `errors.log` or the output.

### Privileges

`SHOW GLOBAL STATUS` and `SHOW GLOBAL VARIABLES` need no privilege. A login that can read everything the collector can use:

```sql
CREATE USER 'monitor'@'localhost' IDENTIFIED BY '...';
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'monitor'@'localhost';
GRANT SELECT ON performance_schema.* TO 'monitor'@'localhost';
```

On MariaDB 10.5.9 and later, `SHOW REPLICA STATUS` needs `REPLICA MONITOR`, which `REPLICATION CLIENT` does not include: `GRANT REPLICA MONITOR ON *.* TO 'monitor'@'localhost';`.

What happens without a privilege. Each case is one `NOTE:` at the start and in the manifest; the run carries on.

| Missing | Effect |
|---------|--------|
| `PROCESS` | `history_list_length` is empty (`information_schema.INNODB_METRICS` is not readable) |
| `SELECT` on `performance_schema` | `mysql_file_io.csv` is not written |
| `REPLICATION CLIENT` (MySQL), `REPLICA MONITOR` (MariaDB 10.5.9+) | Replica lag is not recorded, even on a replica |

## Output

```
<outdir>/<hostname>_mysql_<YYYYmmdd_HHMMSS>/
    mysql_stats.csv    the samples
    mysql_file_io.csv  InnoDB file IO timers (performance_schema on only)
    db_paths.txt       where the server keeps its files (server on this host only)
    manifest.txt       what was run and where
    errors.log         query errors (empty when all is well)
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server. The tag stays `mysql` for MariaDB; the manifest says which it is.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:` that also says where `gather-perf-diag.sh` looks for runs. An explicit `-o` that is not writable is an error.

### manifest.txt

`key=value`. The keys every collector writes: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`. `end=` is added when collection finishes or is stopped, including detached runs. The keys specific to this script and the database collectors:

| Key | Meaning |
|-----|---------|
| `target` | `user@host:port` or `user@socket path` |
| `client_version`, `server_version` | Client program and `VERSION()` as reported |
| `server_version_num` | `major×10000 + minor×100 + patch`, for example `80036` for 8.0.36 and `101114` for MariaDB 10.11.14 |
| `collector_schema` | `3`. A manifest without it is from an older script |
| `db_engine` | `mysql` or `mariadb`, from `VERSION()` |
| `db_instance` | Display name: `localhost:3306`, `db1.example.com:3307`, or the socket path |
| `server_hostname` | `@@hostname`: the host name the server reports about itself |
| `target_local` | `yes`, `no` or `unknown`: whether the server runs on the host that ran the script |
| `target_local_basis` | Why: `socket`, `loopback`, `server hostname`, `local address` or `none` |
| `server_start` | Server start time (its current time minus `Uptime`), as text |
| `query_timeout_s` | The per-sample timeout in force |
| `setting.<name>` | One line per recorded server setting |
| `files` | The data files this run wrote |
| `notes` | Every `NOTE:` printed, separated by ` \| ` |

### Locality

`target_local` tells the analysis whether the OS data in the same bundle belongs to the database server or to a client machine. It is `yes` for a socket, for `localhost`, `127.*` and `::1`, when `@@hostname` equals this host's short name, and when the host name resolves to an address of this host. A database in a container on this host reached through a published port on `localhost` counts as local, because it shares the kernel and disks. One reached on its container IP does not; `target_local_basis` makes the reason visible. It is `unknown` when the host is not an obvious match and the server reports no host name.

### Settings

These server variables are recorded as `setting.<name>` when the server has them and the value is not empty: `hostname`, `version_comment` (names Percona and other builds), `max_connections`, `innodb_buffer_pool_size`, `innodb_flush_log_at_trx_commit`, `sync_binlog`, `innodb_flush_method`, `innodb_io_capacity`, `innodb_io_capacity_max`, `innodb_log_file_size`, `innodb_log_files_in_group`, `innodb_redo_log_capacity`, `innodb_doublewrite`, `performance_schema`, `datadir`, `tmpdir`, `innodb_data_home_dir`, `innodb_log_group_home_dir`, `innodb_tmpdir`, `log_bin`, `thread_cache_size` and `long_query_time`. Values are as the server reports them (sizes in bytes). They are the settings that decide what a counter means.

### db_paths.txt

Written only when `target_local` is `yes`, because the paths of a remote server mean nothing on this host. One `key=value` per line, at most 50 per role:

```
path.data.1=/var/lib/mysql
path.redo.1=/var/lib/mysql
path.redo.2=/var/lib/mysql/#innodb_redo
path.temp.1=/tmp
```

| Role | Source |
|------|--------|
| `data` | `datadir` |
| `redo` | `innodb_log_group_home_dir`, resolved against `datadir` when relative (the default `./` is the data directory); on MySQL 8.0.30 and later also its `#innodb_redo` directory |
| `temp` | Each entry of `tmpdir` (colon-separated), and `innodb_tmpdir` when set |

Trailing slashes are removed. `perfdiag` uses these to tie database activity to the disk devices it analyses.

## mysql_stats.csv

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used. Each column up to `Bytes_sent` is the `SHOW GLOBAL STATUS` variable of the same name. A variable the server does not report is left empty; a short row is never written. Each sample is one row.

Most columns are **cumulative counters** since server start: the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the server restarts, so a sample lower than the one before it marks that point; `Uptime` going down says the same thing even when the counters have grown back.

The 19 columns of earlier versions come first, with unchanged names and meaning:

| Column | Type | Meaning |
|--------|------|---------|
| `Threads_connected` | value | Open client connections |
| `Threads_running` | value | Connections currently executing. Includes the collector's own session |
| `Open_files` | value | Files currently open |
| `Open_tables` | value | Tables currently open |
| `Innodb_row_lock_time_avg` | value | Average row lock wait **since server start**, ms. Over an interval, use the next two columns instead |
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

Version 3.0.0 adds these, in this order:

| Column | Type | Meaning |
|--------|------|---------|
| `Uptime` | marker | Seconds since server start. A drop marks a restart |
| `Threads_created` | counter | Threads created because the thread cache had none free |
| `Connections` | counter | Connection attempts |
| `Max_used_connections` | value | Highest `Threads_connected` since start |
| `Com_commit`, `Com_rollback` | counter | Transactions ended |
| `Select_full_join` | counter | Joins that scanned a table with no usable index |
| `Select_scan` | counter | Selects that scanned a whole table |
| `Sort_merge_passes` | counter | Sorts that had to spill to disk |
| `Table_locks_waited` | counter | Table lock requests that had to wait |
| `Innodb_buffer_pool_wait_free` | counter | Waits for a free buffer pool page: the pool is full of dirty pages |
| `Innodb_buffer_pool_pages_total`, `_dirty`, `_free` | value | Buffer pool size and state, in pages |
| `Innodb_buffer_pool_pages_flushed` | counter | Pages written from the pool |
| `Innodb_data_reads`, `Innodb_data_writes` | counter | InnoDB file reads and writes |
| `Innodb_data_read`, `Innodb_data_written` | counter | Bytes read and written |
| `Innodb_data_fsyncs` | counter | InnoDB `fsync` calls |
| `Innodb_os_log_written` | counter | Bytes written to the redo log |
| `Innodb_os_log_fsyncs` | counter | `fsync` calls on the redo log |
| `Innodb_log_waits` | counter | Waits because the redo log buffer was full |
| `Innodb_row_lock_current_waits` | value | Row locks being waited for now |
| `Innodb_rows_read`, `_inserted`, `_updated`, `_deleted` | counter | InnoDB row operations |
| `Innodb_checkpoint_age`, `Innodb_checkpoint_max_age` | value | MariaDB only: redo log used since the last checkpoint, and the limit |
| `Innodb_history_list_length` | value | MariaDB only: undo history length |
| `Bytes_received`, `Bytes_sent` | counter | Network traffic |

The last four columns are not status variables:

| Column | Type | Meaning |
|--------|------|---------|
| `history_list_length` | value | Undo history length from `information_schema.INNODB_METRICS` (`trx_rseg_history_len`). A long list means old transactions are holding back purge. Empty without `PROCESS` and on MySQL before 5.6 |
| `replica_lag_s` | value | `Seconds_Behind_Source` (`_Master` before the rename), the largest across replication channels. Empty on a server that was not a replica when the run started, and while the lag is `NULL` |
| `replica_io_running` | value | `1` when the replica's IO thread runs (on every channel), otherwise `0` |
| `replica_sql_running` | value | `1` when the replica's SQL thread runs (on every channel), otherwise `0` |

Some servers do not report some variables. MariaDB 10.11, for example, has no `Innodb_rows_*` or `Innodb_os_log_fsyncs`, and MySQL has no `Innodb_checkpoint_*`. Those columns stay empty.

Buffer pool hit ratio over an interval = `1 - (change in Innodb_buffer_pool_reads / change in Innodb_buffer_pool_read_requests)`. Average row lock wait over an interval = `change in Innodb_row_lock_time / change in Innodb_row_lock_waits`.

## mysql_file_io.csv

InnoDB file IO from `performance_schema.file_summary_by_event_name`, one row per InnoDB file event per sample. Only events with activity are listed. All counts and timers are cumulative since server start; timers are in picoseconds. `misc` includes `fsync`.

| Column | Meaning |
|--------|---------|
| `event_name` | For example `wait/io/file/innodb/innodb_data_file`, `wait/io/file/innodb/innodb_log_file` |
| `count_read`, `sum_timer_read_ps` | Reads and time spent in them |
| `count_write`, `sum_timer_write_ps` | Writes and time spent in them |
| `count_misc`, `sum_timer_misc_ps` | Other operations (including `fsync`) and time spent in them |

Average latency over an interval = change in the timer / change in the count. The file is written only when `performance_schema` is on and readable. It is **off by default on MariaDB**. Turn it on with `performance_schema=ON` in the server configuration and restart; the script cannot do that for you and says so in a `NOTE:` when it is off.

## Version gates

| Feature | MySQL | MariaDB |
|---------|-------|---------|
| `SHOW REPLICA STATUS` (otherwise `SHOW SLAVE STATUS`) | 8.0.22 | 10.5.1 |
| `history_list_length` (`INNODB_METRICS`) | 5.6 | 10.0 |
| `mysql_file_io.csv` (`performance_schema`) | 5.5, if on | 5.5, if on (off by default) |
| `Innodb_checkpoint_*`, `Innodb_history_list_length` columns | not reported | reported |
| `#innodb_redo` directory in `db_paths.txt` | 8.0.30 | not applicable |

Flavour is taken from `VERSION()`: `db_engine` is `mariadb` when it contains `MariaDB`, otherwise `mysql`. A leading `5.5.5-` that older MariaDB servers add for replication compatibility is ignored. Percona and other builds report as `mysql`; `setting.version_comment` names them.

### Replicas

The script checks once at the start whether the server is a replica. Only then does it make a second client call per sample for the replica columns. A server that becomes a replica during the run is not picked up. On MariaDB with several replication connections only the default connection is recorded, and a `NOTE:` says so.

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the error goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

Every row is checked before it is written. A row with an unknown record letter or the wrong number of fields goes to `errors.log` instead of the CSV. A failed sample leaves no row in any file.

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
| `cannot connect to MySQL/MariaDB at ... as root` with `Access denied for user 'root'@'localhost'` | Debian and Ubuntu: run with `sudo`, or use a monitoring login. Over TCP a `unix_socket` root login never works |
| `cannot connect to MySQL/MariaDB at host:port as user` | The client error is printed beneath it. Check host, port, socket, user, grants and the password source |
| `--socket cannot be combined with --host or --port` | Give one or the other |
| `queries are failing (see .../errors.log)` | The connection worked but a sample query failed. The first error line is shown |
| `sample timed out after N s` in `errors.log` | The server did not answer within the per-sample limit. The sample is missing; the schedule continues |
| `NOTE: performance_schema is off ...` | Expected on MariaDB. `mysql_file_io.csv` is not written. See [mysql_file_io.csv](#mysql_file_iocsv) |
| `NOTE: ... not readable for this login (grant ...)` | The login lacks a privilege listed under [Privileges](#privileges). The column or file it feeds is empty or missing |
| `... is not writable; run with sudo or pass a different -o <dir>` | Choose a directory you own with `-o` |

## Changes in 3.0.0

- **MariaDB 11 deprecation warning.** The script runs `mariadb` when `mysql` is MariaDB's client, so the `Deprecated program name` line no longer lands in `errors.log` and trips the first-sample warning.
- **`--host localhost --port N`.** It now connects over TCP to that port instead of silently sampling the instance behind the default socket. `--socket` is new.
- **Per-sample timeout and connect timeout.** A hung server no longer holds the sample loop.
- **One client call per sample, one `awk` pass.** Replaces one `awk` process per variable per sample. Replica status is a second call, only on replicas.
- **More columns.** 33 status variables and 4 derived columns are added after the 19 existing ones, which keep their names and meaning. `Innodb_row_lock_time_avg` is still an average since start; the manifest and this page say to use the change in `Innodb_row_lock_time` over the change in `Innodb_row_lock_waits` instead.
- **`mysql_file_io.csv`.** InnoDB file IO timers when `performance_schema` is on.
- **Manifest.** `collector_schema=3`, `db_engine`, `db_instance`, `server_hostname`, `server_version_num`, `target_local` and its basis, `server_start`, `query_timeout_s`, `setting.*` and `files`.
- **`db_paths.txt`.** Data, redo and temp locations for a server on this host.
- **Row checks.** A malformed row is logged instead of written.
- **Debian root login.** Documented: use `sudo` for a local `root` login.
- **Fallback note.** The `NOTE:` that announces the switch to `~/perf-diag` also says where `gather-perf-diag.sh` finds runs.

## Examples

```bash
# Local server over the socket as root on Debian or Ubuntu, 30 minutes at 10-second samples
sudo ./collect-mysql-metrics.sh -d 30m -i 10s

# Remote server
./collect-mysql-metrics.sh -d 2h -i 30s --host db1.example.com --user monitor

# Second instance on the same host, by socket
./collect-mysql-metrics.sh -d 1h -i 15s --socket /run/mysqld-3307/mysqld.sock --user monitor

# Local server over TCP on a non-default port
./collect-mysql-metrics.sh -c 120 -i 5 --host localhost --port 3307 --user monitor

# Quick test, attached
./collect-mysql-metrics.sh -c 5 -i 1 --foreground
```
