# collect-oracle-metrics.sh

Samples Oracle Database statistics at a fixed interval and writes them to CSV files: system statistics, the time model, wait events and what the working sessions are doing. Run it alongside [collect-os-metrics.sh](collect-os-metrics.md) to line up database activity with host load. [gather-perf-diag.sh](gather-perf-diag.md) bundles both, and [perfdiag](perfdiag.md) analyses them together.

Location: `linux/db/collect-oracle-metrics.sh`

> **Licensing.** The script reads only these views: `v$sysstat`, `v$sys_time_model`, `v$system_event`, `v$system_wait_class`, `v$session`, `v$resource_limit`, `v$instance`, `v$database`, `v$version`, `v$parameter`, `v$datafile`, `v$tempfile`, `v$logfile` and `v$asm_disk_stat`. It never reads `v$active_session_history`, `DBA_HIST_*` or AWR, which need the Diagnostics Pack licence. Nothing it runs needs the Diagnostics Pack or the Tuning Pack.

## Requirements

- SQL\*Plus (`sqlplus`) from Oracle Instant Client, with `sqlplus` on `PATH` and the Instant Client libraries on `LD_LIBRARY_PATH` if you installed from the zip files. It is not in the standard Fedora or RHEL repositories. The script prints the download page and distro-specific notes if it is missing. SQL\*Plus 11.2 and later work: version 3.0.0 no longer uses `SET MARKUP CSV`, which needs 12.2.
- Oracle Database 11.2 or later. Earlier releases are refused.
- A connect string for a login with the privileges below.

The script does not need root.

## Privileges

```sql
GRANT CREATE SESSION, SELECT_CATALOG_ROLE TO monitor;
```

`SELECT ANY DICTIONARY` works instead of `SELECT_CATALOG_ROLE`. The login needs to read the views in the licensing note above, including `v$database` and, for ASM, `v$asm_disk_stat`.

If the version cannot be read from `v$instance` the script stops. If a view used only for the manifest cannot be read (`v$database`, `v$parameter`, the file views), the run goes on without those values and the manifest gets a `NOTE:` with the first Oracle error.

Each sample is one logon: SQL\*Plus connects, runs the statements and exits. Every sample therefore adds to the `logons cumulative` statistic and writes a line to the listener log. At an interval of 5 seconds or more that is acceptable. The script does not stop you from using 1 second, but a logon plus four statements can then be slower than the per-sample time limit (see [Timing](#timing)).

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
- Each sample is limited to a time just under the interval (interval − 2 s above 6 s, interval − 1 s above 2 s, otherwise 1 s). A sample that runs longer is stopped and `errors.log` gets `sample timed out after N s`, so one hung query cannot hold the schedule. The limit is recorded as `query_timeout_s`.

### Connect string

Any string accepted by the SQL\*Plus `CONNECT` command works: `user/password@//host:1521/service_name`, `user/password@host/service_name`, `user/password@tns_alias`, a full `(DESCRIPTION=...)`, or `user/password` for a local instance.

Prefer the environment variable. An option value is visible to other users in the process list and stays in shell history.

The script gives the string to SQL\*Plus on standard input, not on the command line, so it does not appear in `ps` either way. It is replaced with `<redacted>` in the `command_line=` entry of the manifest, and no `target=` entry is written. The password does not appear in `errors.log`, `sample.sql` or any other output file.

## Output

```
<outdir>/<hostname>_oracle_<YYYYmmdd_HHMMSS>/
    oracle_stats.csv   one summary row per sample
    oracle_waits.csv   wait classes and a fixed list of wait events, per sample
    oracle_active.csv  what the working user sessions are doing, per sample
    db_paths.txt       where the database keeps its files (server on this host only)
    manifest.txt       what was run and where, plus recorded server settings
    errors.log         query errors (empty when all is well)
    sample.sql         the statements run for each sample
    collector.pid      detached runs only
    collector.log      detached runs only
```

`<hostname>` is the short hostname of the machine running the script, not the database server.

If `/var/log/perf-diag` is not writable and you did not pass `-o`, the script uses `~/perf-diag` and prints a `NOTE:`. An explicit `-o` that is not writable is an error. `gather-perf-diag.sh` looks in `/var/log/perf-diag` and in `~/perf-diag` of the invoking user, `SUDO_USER` and the `postgres`, `mysql`, `oracle` and `mssql` accounts, so a run in the home directory is still bundled.

`manifest.txt` is `key=value`: script and version, command line (with the connect string redacted), host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi`, `client_version`, `server_version` (the `v$version` banner), the database keys below, `setting.*` lines and `notes=`. `end=` is added when collection finishes or is stopped, including detached runs.

| Key | Value |
|-----|-------|
| `collector_schema` | `3`. A manifest without it comes from a 2.x collector |
| `db_engine` | `oracle` |
| `db_instance` | `<instance_name>/<container name>`, for example `ORCL/PDB1` or `ORCL/CDB$ROOT`; on 11.2 just the instance name |
| `server_version_num` | `major × 10000 + second × 100 + third` of `version_full` (18 and later) or `version`: `19.21.0.0.0` is `192100`, `11.2.0.4.0` is `110200` |
| `server_hostname` | `v$instance.host_name` |
| `target_local` | `yes`, `no` or `unknown`: whether the database runs on this host |
| `target_local_basis` | Why: `socket`, `loopback`, `server hostname`, `local address` or `none` |
| `server_start` | `v$instance.startup_time`, for display |
| `query_timeout_s` | The per-sample time limit in force |
| `files` | The data files this run wrote |
| `setting.<name>` | `database_role`, `log_mode`, `cdb` and `con_name` (12 and later), and from `v$parameter`: `sga_target`, `sga_max_size`, `memory_target`, `pga_aggregate_target`, `db_cache_size`, `processes`, `sessions`, `filesystemio_options`, `disk_asynch_io`, `db_writer_processes`, `resource_manager_plan`, `cpu_count`, `db_block_size`, `cluster_database`. Sizes are in bytes. A parameter with no value is not written |

### Does the database run on this host?

The host comes from the connect string. A string with no `@` (or `/ as sysdba`) is a local instance. `localhost` and the loopback addresses are `loopback`. Otherwise the instance's own `host_name` is compared with this host's short name, then the addresses of the host in the connect string are compared with this host's addresses. A database in a container on this host, reached through a published port on `localhost`, is local (same kernel and disks); one reached on its container IP is not.

A TNS alias hides the host, so only the `host_name` comparison can show a local database; if it does not match, the result is `unknown`, not `no`. Use an Easy Connect string (`//host:port/service`) when you want the result to rest on the address. In a container `host_name` is often the container ID, so a local container reached through an alias also reads as `unknown`.

### db_paths.txt

`key=value` lines, written only when the database is local, because a remote server's paths mean nothing on this host:

| Key | Source |
|-----|--------|
| `path.data.N` | Directory of each `v$datafile.name` |
| `path.temp.N` | Directory of each `v$tempfile.name` |
| `path.redo.N` | Directory of each `v$logfile.member` |
| `path.asm_disk.N` | `v$asm_disk_stat.path` of every mounted disk group |

For a file in ASM (`+DATA/...`) the entry is the disk group, for example `path.data.1=+DATA`, and the disks of all mounted disk groups are added as `path.asm_disk.N`. An ASM Filter Driver label (`AFD:...`) or ASMLib label (`ORCL:...`) cannot be turned into a block device here: it is written with a `path.asm_disk.N.note=` line and the manifest gets a `NOTE:`. At most 50 entries are written per role.

### Columns

The first column `ts` is the collecting host's clock in ISO 8601. The server clock is not used.

Most columns are a **cumulative counter**: the value for one sample on its own says little. Work out activity per second as the difference between two samples divided by the seconds between them. The method is shown in [collect-postgresql-metrics.md](collect-postgresql-metrics.md#turning-the-counters-into-rates). Counters reset when the instance restarts, so a sample lower than the one before it marks that point. `server_start_marker` changes on a restart even when every counter grew.

Every number is converted in SQL with `TO_CHAR(x, 'TM9')`, so a counter of any size arrives with all its digits. A value the instance does not report is an empty field, not 0.

#### oracle_stats.csv

The first ten columns are the 2.0.0 columns, with the same names and meaning.

| Column | Source | Type |
|--------|--------|------|
| `physical_reads` | `v$sysstat` `physical reads` | counter |
| `physical_writes` | `physical writes` | counter |
| `session_logical_reads` | `session logical reads` | counter |
| `redo_size` | `redo size` (bytes) | counter |
| `user_calls` | `user calls` | counter |
| `db_block_gets` | `db block gets` | counter |
| `consistent_gets` | `consistent gets` | counter |
| `parse_count_total` | `parse count (total)` | counter |
| `parse_count_hard` | `parse count (hard)` | counter |
| `active_sessions` | Rows in `v$session` with `STATUS = 'ACTIVE'` | gauge |
| `physical_read_total_io_requests`, `physical_write_total_io_requests` | `physical read total IO requests`, `physical write total IO requests` | counter |
| `physical_read_total_bytes`, `physical_write_total_bytes` | `physical read total bytes`, `physical write total bytes`: all I/O, including redo and archiving | counter |
| `user_commits`, `user_rollbacks` | `user commits`, `user rollbacks` | counter |
| `execute_count` | `execute count` | counter |
| `sorts_disk`, `sorts_memory` | `sorts (disk)`, `sorts (memory)` | counter |
| `redo_log_space_requests` | `redo log space requests` | counter |
| `logons_current` | `logons current` | gauge |
| `db_time_us`, `db_cpu_us` | `v$sys_time_model` `DB time`, `DB CPU` (microseconds) | counter |
| `user_sessions` | `v$session` with `TYPE = 'USER'` | gauge |
| `user_active_sessions` | User sessions that are `ACTIVE`, not idle-waiting (`STATE = 'WAITING'` and `WAIT_CLASS = 'Idle'`) and not the collector's own session | gauge |
| `processes_current`, `processes_limit`, `sessions_current`, `sessions_limit` | `v$resource_limit`. A limit of `UNLIMITED` is an empty field | gauge |
| `server_start_marker` | Instance startup time as seconds since 1970 on the database server's clock. Used only to detect a restart | marker |

`active_sessions` keeps its old meaning and includes Oracle background sessions and sessions that are `ACTIVE` but idle-waiting, so it is not the number of sessions doing work. Use `user_active_sessions` for that.

#### oracle_waits.csv

`ts,kind,name,total_waits,time_waited_us,total_waits_fg,time_waited_us_fg`. All four figures are cumulative; the `_fg` columns count foreground sessions only. Names are quoted.

- `kind = class`: every wait class in `v$system_wait_class` except `Idle`. The view reports centiseconds; the script multiplies by 10000 so the column is microseconds like the others.
- `kind = event`: these events from `v$system_event`, when the instance has had any waits for them: `db file sequential read`, `db file scattered read`, `db file parallel read`, `direct path read`, `direct path read temp`, `direct path write temp`, `log file sync`, `log file parallel write`, `log buffer space`, `log file switch (checkpoint incomplete)`, `log file switch completion`, `free buffer waits`, `buffer busy waits`, `read by other session`, `enq: TX - row lock contention`, `enq: TX - index contention`, `enq: TM - contention`, `latch: cache buffers chains`, `library cache: mutex X`, `cursor: pin S wait on X`, `resmgr:cpu quantum`, `gc buffer busy acquire`, `gc cr block busy`.

Divide the change in `time_waited_us` by the interval in microseconds to get the average number of sessions waiting. An event also counts in its class, so do not add the two kinds together.

#### oracle_active.csv

`ts,state_class,sessions`. The user sessions that are working at the sample instant, grouped by what they are doing: the wait class they are waiting in, or `ON CPU` when they are not waiting. A class with no row in a sample had no sessions. These are snapshots, not averages: a wait shorter than the interval can fall between samples.

### Pluggable databases and RAC

Connected to a pluggable database, the `v$` views can describe that container only. `db_instance` shows the container (`ORCL/PDB1`), `setting.con_name` holds its name, and the manifest gets a `NOTE:`. Connect to `CDB$ROOT` for instance-wide figures. How each view is scoped in a pluggable database has not been checked on every release, so treat a per-container reading with care.

The `v$` views do not span RAC nodes. With `cluster_database=TRUE` the figures are for the instance you connect to, and the manifest says so. Run one collector per node.

## Changes in 3.0.0

- Counters are converted with `TO_CHAR(x, 'TM9')`. Before, SQL\*Plus formatted a bare number to `NUMWIDTH` (10 digits) and showed larger values in scientific notation; the row then failed the number check and was dropped into `errors.log`. A busy instance reaches 10 digits in `redo size` and `session logical reads` within days.
- `SET MARKUP CSV ON` is no longer used, so SQL\*Plus 11.2 and 12.1 work. Before, every sample failed there with an `SP2-` error.
- `user_active_sessions` is new and counts user sessions that are working. `active_sessions` is unchanged.
- New time model, I/O, commit, sort, session-limit and restart-marker columns in `oracle_stats.csv`, and the new files `oracle_waits.csv`, `oracle_active.csv` and `db_paths.txt`.
- Identity, multitenant context (`db_instance`, `setting.con_name`, `setting.cdb`) and settings in the manifest, and whether the database is on this host.
- Each sample has a time limit. A row with the wrong number of fields, or any other unexpected output such as an `ORA-` message, goes to `errors.log` instead of the CSV.
- Oracle 11.2 is the minimum. Before there was no version check.

## Running and stopping

- Default: after the first sample the script prints the output path and the stop command, then returns to your prompt. The collector keeps running after you log out of SSH.
- Stop early: `kill $(cat <outdir>/collector.pid)`. The manifest gets its `end=` line.
- `--foreground`: the script stays attached. Ctrl-C stops it.
- A detached run does not survive `systemd-logind` setting `KillUserProcesses=yes`. That setting is off by default on Fedora, RHEL, Debian and Ubuntu.

If a query fails, the output goes to `errors.log` and collection continues, so a temporary outage leaves gaps instead of ending the run. SQL\*Plus reports errors on standard output. Every output row starts with a letter (`S`, `E` or `A`) that names its file. A row with an unknown letter, such as an `ORA-` message, or with the wrong number of fields is written to `errors.log` and not to a CSV. A sample that fails entirely has no row for its timestamp in any file. After the first sample the script warns if `errors.log` already has content. Later errors appear only in the log, so check it after a long run.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, or stopped by a signal |
| 1 | Runtime error: `sqlplus` missing, no connect string, connection failed, server older than 11.2, version unreadable, output directory not writable |
| 2 | Usage error |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `'sqlplus' not found` | Install Instant Client Basic and SQL\*Plus and set `PATH` and `LD_LIBRARY_PATH` |
| `no connect string supplied` | Set `ORACLE_CONNECT_STRING` or pass `--connect` |
| `sqlplus could not connect` / `Oracle connection failed` | The SQL\*Plus output (`ORA-` or `SP2-` errors) is printed beneath it |
| `could not read the version from v$instance` | The login cannot read `v$instance`. Grant `SELECT_CATALOG_ROLE` |
| `Oracle 11.2 or later is needed` | The server is older than 11.2 |
| `queries are failing (see .../errors.log)` | The connection worked but the sample query failed. A missing `SELECT` grant on a `v$` view is the usual cause |
| `sample timed out after N s` in `errors.log` | The logon or a statement took longer than the limit. Use a longer interval, or look at listener and server load |
| `S row has N fields, expected 30` in `errors.log` | The row was damaged or an error message was mixed in. The row is not in the CSV |
| `NOTE: N setup query error(s)` | A view used for the manifest could not be read. The run is fine; grant `SELECT_CATALOG_ROLE` for the missing identity, settings or paths |
| `-c/--count must be a positive integer; connect strings now use --connect` | An old-style `-c` connect string, or a bad count |

## Examples

```bash
# Environment variable, 30 minutes at 10-second samples
ORACLE_CONNECT_STRING='monitor/secret@//db1:1521/ORCL' ./collect-oracle-metrics.sh -d 30m -i 10s

# Explicit option (visible in ps and shell history)
./collect-oracle-metrics.sh --connect 'monitor/secret@//db1:1521/ORCL' -c 120 -i 5

# Instance-wide figures on a multitenant database: connect to the root container
ORACLE_CONNECT_STRING='c##monitor/secret@//db1:1521/ORCL_ROOT' ./collect-oracle-metrics.sh -d 1h -i 15s

# Quick test, attached
./collect-oracle-metrics.sh -c 5 -i 5 --foreground
```
