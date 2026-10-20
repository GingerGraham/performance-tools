# collect-mssql-metrics.sh

Samples SQL Server performance counters, wait statistics and per-file IO at a fixed interval and writes them to CSV files. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load. The script runs on Linux and can collect from a SQL Server on Linux or Windows.

Location: `linux/db/collect-mssql-metrics.sh`

## Requirements

- `sqlcmd`. The script looks on `PATH`, then in `/opt/mssql-tools18/bin` and `/opt/mssql-tools/bin`. It is not in the standard Fedora or RHEL repositories. Install it from Microsoft's package repository (see [sqlcmd setup](https://learn.microsoft.com/sql/linux/sql-server-linux-setup-tools)); the script prints the commands if `sqlcmd` is missing.
- SQL Server 2012 or later, and a login with the permissions in [Permissions on the server](#permissions-on-the-server).
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

When the installed `sqlcmd` supports it (`mssql-tools18` and the Go `sqlcmd`), the script adds `-C -N`: the connection is encrypted and the server certificate is trusted without being validated. That protects against eavesdropping but not against a man in the middle, so the manifest records `tls_trust_server_certificate=yes`. The older `mssql-tools` `sqlcmd` does not accept these switches, so they are left off, a `NOTE:` is printed and the manifest says `tls_trust_server_certificate=no`.

### Limits on each call

Every `sqlcmd` call gives up on the login after 5 seconds (`-l 5`) and exits with a non-zero status when a statement fails (`-b`). A sample is also limited to a time just under the interval (`-t` and `timeout(1)`): the interval minus 2 seconds for intervals over 6 seconds, minus 1 second for 3 to 6 seconds, 1 second otherwise. One hung query cannot hold the schedule. The limit in force is `query_timeout_s` in the manifest. The start-up queries are not held to it: they get 30 seconds.

## Permissions on the server

| Server | Grant |
|--------|-------|
| SQL Server 2019 and earlier | `GRANT VIEW SERVER STATE TO monitor;` |
| SQL Server 2022 and later | `GRANT VIEW SERVER PERFORMANCE STATE TO monitor;` (`VIEW SERVER STATE` also works, because it implies it) |
| Any version, for `mssql_files.csv` and `db_paths.txt` | `GRANT VIEW ANY DEFINITION TO monitor;` |

The first grant covers the performance counters, the wait statistics, the scheduler and request figures and the file IO statistics. `sys.master_files`, which gives each file's type and path, shows no rows without `VIEW ANY DEFINITION`. Then `mssql_files.csv` stays empty and no paths are recorded, and the script adds a `NOTE:` to the manifest. The other records are not affected.

### Database

`--database` sets the database the connection starts in. The counters collected are server-wide, so the choice does not change the data. The database must exist and the login must be able to connect to it.

## Output

```
<outdir>/<hostname>_mssql_<YYYYmmdd_HHMMSS>/
    mssql_stats.csv    one summary row per sample, server-wide
    mssql_waits.csv    selected wait types per sample
    mssql_files.csv    IO statistics per database file per sample
    db_paths.txt       where the server keeps its files (server on this host only)
    manifest.txt       what was run and where, plus recorded server settings
    errors.log         query errors (empty when all is well)
    sample.sql         the statements run for each sample
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error.

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `target` (`[user@]server/database`), `client_version`, `server_version` (`ProductVersion`), `tls_trust_server_certificate`, the database keys below, `setting.*` and `counter_type.*` lines and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

| Key | Value |
|-----|-------|
| `collector_schema` | `3`. A manifest without it comes from a 2.x collector |
| `db_engine` | `mssql` |
| `db_instance` | `InstanceName`, or `MSSQLSERVER` for the default instance |
| `server_version_num` | Major version times 10000, from `ProductMajorVersion`: `150000` for SQL Server 2019, `160000` for 2022 |
| `server_hostname` | `MachineName`: the host name the server reports (on Linux, the host or the container id) |
| `target_local` | `yes`, `no` or `unknown`: whether the server runs on this host |
| `target_local_basis` | Why: `loopback`, `server hostname`, `local address` or `none` |
| `server_start` | `sqlserver_start_time`, for display (the server's clock) |
| `query_timeout_s` | The per-sample time limit in force |
| `files` | The data files this run wrote |
| `tls_trust_server_certificate` | `yes` when the script added `-C`, otherwise `no` |
| `path_namespace` | `container` when the server's file paths are inside a container (see `db_paths.txt`) |
| `setting.<name>` | `product_version`, `edition`, `is_hadr_enabled`, `max_server_memory_mb`, `max_degree_of_parallelism`, `cost_threshold_for_parallelism`, `max_worker_threads`, `cpu_count`, `scheduler_count`, `max_workers_count`, `physical_memory_kb`, `committed_target_kb`. The first group comes from `SERVERPROPERTY` and `sys.configurations`, the last five from `sys.dm_os_sys_info` |
| `counter_type.<column>` | The `cntr_type` the server reports for the counter behind each counter column (see Columns) |

The server is local when it is reached through a loopback name (the default `localhost`), when `MachineName` is this host's name, or when the host's own addresses match. Only the host part of `--host` counts: the port and the instance name are ignored. A server in a container on this host, reached through a published port on `localhost`, is local (same kernel and disks). One reached on its container IP is not, and `target_local_basis` shows why.

`db_paths.txt` is `key=value`: the directories of the database files, from `sys.master_files`, as `path.data.N` (data files), `path.log.N` (transaction logs) and `path.temp.N` (tempdb files). It is written only when the server is local, because a remote server's paths mean nothing on this host. A container reached over loopback reports a `MachineName` that differs from this host's name. The paths are then inside the container's mount namespace, which this host cannot map to devices. The file starts with `path_namespace=container`, the manifest repeats it and a `NOTE:` is added.

### Columns

#### mssql_stats.csv

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used.

Columns ending `_total` are **cumulative counters** since server start. SQL Server's own per-second counters are cumulative in this view, so the script keeps them as they are and names them `_total`. Work out the rate as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the server restarts, so a sample lower than the one before it marks that point. `server_start_marker` changes on a restart even when every counter has grown since.

All other columns are values at the moment of sampling. A counter that the server does not report is an **empty field**. The row always has the same number of fields.

The first ten columns are unchanged from 2.x. The counters come from `sys.dm_os_performance_counters`, matched by object suffix (`LIKE '%:Locks'`) so named instances (`MSSQL$NAME:Locks`) work. Counters with an instance are read from the `_Total` instance.

| Column | Type | Source and meaning |
|--------|------|--------------------|
| `buffer_cache_hit_ratio` | value | Buffer Manager: cache hit ratio as a percentage (counter divided by its base) |
| `page_life_expectancy` | value | Buffer Manager: seconds a page stays in the buffer pool |
| `checkpoint_pages_total` | counter | Buffer Manager: pages flushed by checkpoints |
| `lazy_writes_total` | counter | Buffer Manager: buffers written by the lazy writer |
| `memory_grants_pending` | value | Memory Manager: queries waiting for a memory grant |
| `target_server_memory_kb` | value | Memory Manager: memory SQL Server wants to use |
| `total_server_memory_kb` | value | Memory Manager: memory SQL Server is using |
| `batch_requests_total` | counter | SQL Statistics: batch requests received |
| `sql_compilations_total` | counter | SQL Statistics: compilations |
| `sql_recompilations_total` | counter | SQL Statistics: recompilations |
| `user_connections` | value | General Statistics: user connections |
| `processes_blocked` | value | General Statistics: processes blocked |
| `lock_waits_total` | counter | Locks: lock waits |
| `lock_wait_time_ms_total` | counter | Locks: time spent waiting for locks, in ms |
| `deadlocks_total` | counter | Locks: deadlocks |
| `log_flushes_total` | counter | Databases: log flushes |
| `log_flush_wait_time_ms_total` | counter | Databases: time spent waiting for log flushes, in ms |
| `log_bytes_flushed_total` | counter | Databases: bytes written by log flushes |
| `page_reads_total` | counter | Buffer Manager: physical page reads |
| `page_writes_total` | counter | Buffer Manager: physical page writes |
| `free_list_stalls_total` | counter | Buffer Manager: requests that waited for a free buffer |
| `full_scans_total` | counter | Access Methods: unrestricted full scans |
| `page_splits_total` | counter | Access Methods: page splits |
| `workfiles_created_total` | counter | Access Methods: work files created |
| `worktables_created_total` | counter | Access Methods: work tables created |
| `memory_grants_outstanding` | value | Memory Manager: memory grants held |
| `active_requests` | value | Requests running now from user sessions, without the collector's own |
| `runnable_tasks` | value | Tasks waiting for CPU, summed over the online schedulers |
| `work_queue_count` | value | Tasks waiting for a worker thread, summed over the online schedulers |
| `active_workers` | value | Worker threads in use, summed over the online schedulers |
| `busy_schedulers` | value | Online schedulers that are not idle |
| `server_start_marker` | marker | Server start as seconds since 1970 on the server's clock. Compared only with itself |

`counter_type.<column>` in the manifest is the `cntr_type` that the server reports for the counter behind each counter column, so the analysis does not have to guess from the name. `65792` is a value at the sample instant, `272696576` a cumulative counter. `buffer_cache_hit_ratio` has no key, because the column holds the finished percentage and not the raw counter.

#### mssql_waits.csv

`ts,wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms`. Cumulative since server start, from `sys.dm_os_wait_stats`, for the waits that explain most database time: `WRITELOG`, `LOGBUFFER`, `IO_COMPLETION`, `ASYNC_IO_COMPLETION`, `WRITE_COMPLETION`, `BACKUPIO`, `SOS_SCHEDULER_YIELD`, `CXPACKET`, `CXCONSUMER`, `RESOURCE_SEMAPHORE`, `RESOURCE_SEMAPHORE_QUERY_COMPILE`, `THREADPOOL`, `ASYNC_NETWORK_IO`, `HADR_SYNC_COMMIT`, `CMEMTHREAD`, and every `PAGEIOLATCH_*`, `PAGELATCH_*` and `LCK_M_*`. The name is double-quoted. Wait time is per task, so a parallel query adds one wait per task and the total can exceed the number of sessions. `signal_wait_time_ms` is the part of the wait spent queued for CPU after the resource was free.

#### mssql_files.csv

`ts,database,file_type,physical_name,num_of_reads,io_stall_read_ms,num_of_bytes_read,num_of_writes,io_stall_write_ms,num_of_bytes_written`. Cumulative since the database came online, one row per database file, from `sys.dm_io_virtual_file_stats`. `file_type` is `ROWS` or `LOG`. Average latency over an interval is the change in `io_stall_*_ms` divided by the change in the matching operation count. This is the figure that lines up with the OS per-device latency. The database name, the type and the path are double-quoted, so read the file with a CSV parser.

### Changes in 3.0.0

- Counters are matched by object suffix and by the `_Total` instance, so they are the server-wide figure and named instances work.
- New columns in `mssql_stats.csv` after the first ten: locks, log flush, buffer manager and access method counters, connections, requests and scheduler figures, and `server_start_marker`. The original ten keep their names and meaning.
- New files `mssql_waits.csv`, `mssql_files.csv` and `db_paths.txt`, and the manifest keys above.
- A counter the server does not report is an empty field. Before it was the word `NULL`.
- Each sample is one `sqlcmd` call, limited to a timeout just under the interval. A row with the wrong number of fields, and any error text that `sqlcmd` prints, goes to `errors.log` instead of the CSV. `NULL` warnings from aggregates are switched off, because `sqlcmd` prints them among the rows.
- SQL Server 2012 or later is needed. Before, the script ran on older servers.
- `-C` is still added whenever `sqlcmd` supports it. The help and the manifest now say so.

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the output goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. `sqlcmd` reports errors on standard output, so error text arrives as rows with an unknown first field. The script writes a row to a CSV only when it starts with a known record letter and has the right number of fields. Anything else goes to `errors.log`, followed by `client exited with status N` when `sqlcmd` failed. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or stopped by a signal |
| 1 | Runtime error: `sqlcmd` missing, connection failed, cannot read the server version, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `'sqlcmd' not found` | Install `mssql-tools18` from Microsoft's repository. SLES is not supported by Microsoft's packages; collect from a supported distro instead |
| `cannot connect to SQL Server at '...'` | The `sqlcmd` error is printed beneath it. Check the server name, port, firewall, login and certificate |
| `A user was given but SQLCMDPASSWORD is not set.` | Export `SQLCMDPASSWORD` before running |
| `queries are failing (see .../errors.log)` | The connection worked but the sample query failed. A missing `VIEW SERVER STATE` (`VIEW SERVER PERFORMANCE STATE` on 2022 and later) permission is the usual cause |
| `SQL Server 2012 or later is needed` | The server is older than the script supports |
| `sample timed out after N s` in `errors.log` | The server did not answer within the per-sample limit. Check for blocking or an overloaded server |
| `sys.master_files lists N of M database files (grant VIEW ANY DEFINITION)` in the manifest notes | `mssql_files.csv` is empty or incomplete. Grant `VIEW ANY DEFINITION` |
| `sys.dm_os_sys_info is not readable for this login` in the manifest notes | The server settings from `sys.dm_os_sys_info` are missing. Grant the permission named in the note |
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
