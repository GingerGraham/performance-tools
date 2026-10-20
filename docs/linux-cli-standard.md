# Linux scripts: CLI standard

Applies to every script under `linux/`: `os/collect-os-metrics.sh`, `os/psar.sh`, `os/collect-hw-inventory.sh`, `os/gather-perf-diag.sh` and `db/collect-*-metrics.sh`.

Each script has `SCRIPT_VERSION="2.0.0"`. Run any script with `-h` for its own usage.

This document is for people writing or changing the scripts. How to run each script is in its user guide: [collect-os-metrics](collect-os-metrics.md), [psar](psar.md), [postgresql](collect-postgresql-metrics.md), [mysql](collect-mysql-metrics.md), [oracle](collect-oracle-metrics.md), [mssql](collect-mssql-metrics.md), [hw inventory](collect-hw-inventory.md), [gather](gather-perf-diag.md).

## Rules

1. Core options (below) have the same short and long form and the same meaning in every script.
2. Short letters are reserved for core options, plus `-p` (OS collector) and psar's section letters `-C -M -D -N -L -A`. Everything else is long-only.
3. Long options accept `--opt value` and `--opt=value`.
4. Short flags are not bundled. Use `-C -M`, not `-CM`.
5. `--` ends option parsing.
6. Passwords are never an option value. They show in `ps` and shell history. Each engine uses its client's environment variable or config file.
7. A missing option value is a usage error naming the option.
8. Exit codes: `0` success, `1` runtime error (missing dependency, connection failed, I/O error), `2` usage error.
9. Messages go to stderr with a fixed prefix: `ERROR:`, `WARNING:`, `NOTE:`. `NOTE:` marks optional data that is unavailable on this host.
10. When stdin is not a terminal nothing prompts. A missing required value is a usage error.

## Core options

| Short | Long | Meaning |
|-------|------|---------|
| `-d` | `--duration` | Total run time. Units `s m h d`. A bare integer is seconds |
| `-c` | `--count` | Number of samples. Mutually exclusive with `-d`. psar only: `0` means until stopped |
| `-i` | `--interval` | Sample interval. Units `s m`. A bare integer is seconds |
| `-o` | `--outdir` | Base output directory |
| `-h` | `--help` | Usage, then exit 0 |
| `-V` | `--version` | Script name and version, then exit 0 |
| | `--foreground` | Stay attached; Ctrl-C stops collection. Collectors only |

Collectors prompt for duration (or count) and interval when run from a terminal without them. psar defaults to an interval of 2 seconds and runs until stopped.

## Per-script options

| Script | Options |
|--------|---------|
| `collect-os-metrics.sh` | `-p, --process LIST` (pidstat `-C` filter), `--extended` (sadc `-S XALL`), `--threads` (pidstat `-t`) |
| `psar.sh` | `-C -M -D -N -L -A` sections, `--dev LIST`, `--iface LIST`, `--header-once`, `-o DIR` writes CSV files |
| `collect-postgresql-metrics.sh` | `--host` (localhost), `--port` (5432), `--user` (current user), `--database` (postgres). Password: `PGPASSWORD` or `~/.pgpass` |
| `collect-mysql-metrics.sh` | `--host` (localhost), `--port` (3306), `--user` (root). Password: `MYSQL_PWD` or `~/.my.cnf` |
| `collect-oracle-metrics.sh` | `--connect STRING` (default `$ORACLE_CONNECT_STRING`). Sent to SQL*Plus on stdin, not argv |
| `collect-hw-inventory.sh` | `--imds` (ask the cloud metadata service for the instance type). Does not sample, so `-d`, `-c`, `-i` do not apply. `-o` defaults to `/var/log/perf-diag` |
| `gather-perf-diag.sh` | `--since` (7d), `--run DIR` (repeatable), `--base DIR` (repeatable; default `/var/log/perf-diag` plus the `~/perf-diag` of the invoking user, `SUDO_USER` and the `postgres`, `mysql`, `oracle` and `mssql` accounts), `--format tar.gz\|zip`, `--no-kernel-log`, `--no-redact`, `--no-inventory`. Does not sample. `-o` is where the bundle is written and defaults to the current directory |
| `collect-mssql-metrics.sh` | `--host` (localhost; accepts `host`, `host,port`, `host\instance`), `--port`, `--user` (omit for trusted/AD auth), `--database` (master). Password: `SQLCMDPASSWORD` |

`--host`, `--port`, `--user` and `--database` are the same names across the engines that have them.

## Migration from the earlier options

| Script | Old | New |
|--------|-----|-----|
| psar | `-d LIST` | `--dev LIST` (`-d` is now duration) |
| psar | `-e LIST` | `--iface LIST` |
| psar | `-n` | `--header-once` |
| psar | `-w DIR` | `-o DIR` (writes to `DIR/<host>_psar_<ts>/`) |
| postgresql | `-h -p -U -w` | `--host --port --user --database` |
| mysql | `-h -P -u` | `--host --port --user` |
| oracle | `-c CONNSTR` | `--connect CONNSTR` (`-c` is now count) |
| mssql | `-S -U -n` | `--host --user --database` |
| mssql | `-P PASSWORD` | removed; set `SQLCMDPASSWORD` |
| all DB | `--help` only | `-h` and `--help` |

Removed options exit 2 and name their replacement. Where a letter changed meaning, the old usage fails instead of doing something different: `psar.sh -d vda` and `collect-oracle-metrics.sh -c 'user/pw@//h/s'` are usage errors. The Oracle error does not print the rejected value, because it may be a password. In the postgresql and mysql scripts, which used `-h` for the host, `-h <value>` is refused with a pointer to `--host` rather than printing help.

## Shared behaviour

### Output directory

```
<outdir>/<short-hostname>_<tag>_<YYYYmmdd_HHMMSS>/
    manifest.txt
    readback.txt        (OS collector)
    <data files>
    db_paths.txt        (DB collectors, server on this host)
    errors.log          (DB collectors)
    *.pid               (detached runs)
```

Tags: `os`, `psar`, `postgresql`, `mysql`, `oracle`, `mssql`, `hw` (hardware inventory) and `gather` (a bundle written by `gather-perf-diag.sh`; the bundle is a `.tar.gz` or `.zip` named `<host>_gather_<ts>`, not a directory).

The default base is `/var/log/perf-diag`. The OS collector creates it with root privileges and then gives the run directory (only) to the invoking user. DB collectors run as the invoking user. When no `-o` is given and `/var/log/perf-diag` is not writable they use `~/perf-diag` and print a `NOTE:`. An explicit `-o` that is not writable is an error. The DB collectors' `NOTE:` for the home fallback says where gather looks. Running a DB collector under `sudo` is avoided on purpose (`collect-mysql-metrics.sh` is the exception: a local `root` login on Debian and Ubuntu authenticates through `unix_socket`, which needs the OS user `root`): it changes `$USER` and where `~/.pgpass` and `~/.my.cnf` are read from. Because of that, DB runs usually end up in a home directory, so `gather-perf-diag.sh` searches `/var/log/perf-diag` and the `~/perf-diag` of the invoking user, `SUDO_USER` and the database service accounts (`getent passwd` gives the home). `--base` replaces that list. A base that does not exist is skipped silently; one that exists but cannot be read gives `NOTE: cannot read <dir> (run with sudo to include it)`. The bases searched are recorded as `base.<n>` in `gather_manifest.txt`.

### manifest.txt

`key=value`, one per line, written at start: script, version, redacted command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi=yes|no`, then script-specific keys (sysstat version, sadc keywords and pidstat flags, launcher; client and server versions and target for DB collectors), then `db_detected=` (`collect-os-metrics.sh` and `gather-perf-diag.sh` only, when a database server is running: `postgresql,mysql,oracle,mssql` in that order, key absent otherwise), then `notes=`. An `end=` line is appended when a DB collector, a `--foreground` OS run or a psar `-o` run finishes or is stopped. A detached sadc/pidstat run cannot append one. The value of `--connect` is replaced with `<redacted>`. `perfdiag` reads `cpus`, `mem_total_kb`, `timezone` and `interval_s`, so they stay mandatory in every OS and psar manifest.

### DB collector manifest keys

Every DB collector at `collector_schema=3` adds these after the common keys. `perfdiag` treats a manifest without `collector_schema` as schema 2.

| Key | Value |
|-----|-------|
| `collector_schema` | `3` |
| `db_engine` | `postgresql`, `mysql`, `mariadb`, `oracle` or `mssql`. The run tag stays `mysql` for MariaDB |
| `db_instance` | A short display id without secrets: `localhost:5432`, `/var/run/postgresql:5432`, `ORCL/PDB1`, `MSSQLSERVER` |
| `server_version_num` | The server version as an integer, so `perfdiag` can gate on a number instead of text |
| `server_hostname` | The host name the server reports about itself |
| `target_local` | `yes`, `no` or `unknown`: whether the server runs on this host |
| `target_local_basis` | Why: `socket`, `loopback`, `server hostname`, `local address` or `none` |
| `server_start` | Server start time as the server reports it (display only) |
| `setting.<name>` | One line per recorded server setting |
| `files` | Space-separated list of the data files this run wrote |
| `query_timeout_s` | The per-sample timeout in force |

`db_paths.txt` is `key=value` with `path.<role>.<n>=<directory>` (roles `data`, `wal`, `redo`, `log`, `temp`, `tablespace`, `asm_disk`; at most 50 per role) and optional `path.<role>.<n>.note=` lines. It is written only when `target_local=yes`.

### Record-prefix routing

A DB collector makes one client call per sample. The call returns several result sets, and every row starts with a one-letter record type (`S` summary, `W` waits, `E` events, `F` files, `D` databases, `P` file IO, `A` active-session snapshot). `route_sample_output` in block D splits the output by that letter and appends each row, with the sample timestamp in front, to the file registered with `register_record`. The expected field count comes from the file's header. A row with an unknown letter or the wrong field count is written to `errors.log` and not to the CSV. Strings (database names, wait names, file paths) are emitted already CSV-quoted by the SQL; numbers are never quoted. The `S` record gives exactly one row per sample, so a sample without one failed.

### Per-sample timeout

A sample may take at most `QUERY_TIMEOUT_S`: interval minus 2 seconds above 6 seconds, interval minus 1 above 2 seconds, otherwise 1 second. `run_with_timeout` wraps the client in `timeout --kill-after=2`. Exit status 124 is logged to `errors.log` as `sample timed out after N s`, and the next sample runs on schedule. Engine-level limits are added where they are simple: `statement_timeout` for PostgreSQL, `--connect-timeout` for MySQL, `-l` and `-t` for sqlcmd.

### Timestamps

The first CSV column is `ts`, from `date -Iseconds` on the collecting host. Server clocks are not used.

### Sample timing

DB collectors schedule sample *n* at `start + n × interval` using `/proc/uptime`. Time spent in a query does not push later samples back.

### Detaching (DB collectors)

The first sample is taken attached to the terminal, so a bad query or permission error is visible. The rest then run detached (`collector.pid`, `collector.log`) unless `--foreground` is given. A detached run ignores `HUP`, so it survives SSH logout. It does not survive systemd-logind `KillUserProcesses=yes`, which is off by default on Fedora, RHEL, Debian and Ubuntu. Stop it with `kill $(cat <outdir>/collector.pid)`.

## OS collector specifics

- sadc collects `XDISK,SNMP,IPV6` by default and `XALL` with `--extended`. Each keyword is probed on the host and skipped with a `NOTE:` when unsupported.
- sadc is given `samples + 1` records, because its first record is a baseline. Both tools then cover the same window.
- pidstat runs with `LC_ALL=C S_TIME_FORMAT=ISO` and `-u -r -d -w -h -l`, plus `-v` and `-H` when supported.
- Without systemd as init (Ubuntu 14.04, containers) collection starts with `setsid nohup` and PID files instead of `systemd-run`.
- A quote in a `-p` value is safe: arguments reach `bash -c` as positional parameters, not through string interpolation.

## Maintaining the scripts

Each script is standalone. Block D (output location, sample timing, detaching, timeout, record routing, locality, settings and paths) is identical in the four DB collectors, and `tests/test_db_blockd.py` fails when the copies differ. The option-parsing, manifest and (for DB collectors) run-control helpers are copied into every script that uses them, between the "Shared block" comments. When you change one, change every copy. Block A (messages, `take_value`, `parse_to_seconds`) and block B (manifest) are identical in every script, including `collect-hw-inventory.sh` and `gather-perf-diag.sh`. Block E (the database server signature table: `classify_db_process`, `db_scan`, `db_engines_detected`) is identical in `gather-perf-diag.sh`, `collect-os-metrics.sh` and `collect-hw-inventory.sh`, and `perfdiag` carries the same table; `tests/test_collector_manifest.py` fails when the three copies differ. Change the table in all four places.

When you add, remove or change an option, a default, an output file or a CSV column, update the script's user guide in the same change.
