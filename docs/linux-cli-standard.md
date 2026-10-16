# Linux scripts: CLI standard

Applies to every script under `linux/`: `os/collect-os-metrics.sh`, `os/psar.sh` and `db/collect-*-metrics.sh`.

Each script has `SCRIPT_VERSION="2.0.0"`. Run any script with `-h` for its own usage.

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
    errors.log          (DB collectors)
    *.pid               (detached runs)
```

Tags: `os`, `psar`, `postgresql`, `mysql`, `oracle`, `mssql`.

The default base is `/var/log/perf-diag`. The OS collector creates it with root privileges and then gives the run directory (only) to the invoking user. DB collectors run as the invoking user. When no `-o` is given and `/var/log/perf-diag` is not writable they use `~/perf-diag` and print a `NOTE:`. An explicit `-o` that is not writable is an error. Running a DB collector under `sudo` is avoided on purpose: it changes `$USER` and where `~/.pgpass` and `~/.my.cnf` are read from.

### manifest.txt

`key=value`, one per line, written at start: script, version, redacted command line, host, distro, kernel, CPU count, memory, timezone, start time, interval, samples, duration, `psi=yes|no`, then script-specific keys (sysstat version, sadc keywords and pidstat flags, launcher; client and server versions and target for DB collectors), then `notes=`. An `end=` line is appended when a DB collector, a `--foreground` OS run or a psar `-o` run finishes or is stopped. A detached sadc/pidstat run cannot append one. The value of `--connect` is replaced with `<redacted>`.

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

Each script is standalone. The option-parsing, manifest and (for DB collectors) run-control helpers are copied into every script that uses them, between the "Shared block" comments. When you change one, change every copy. Block A (messages, `take_value`, `parse_to_seconds`) is identical in all six scripts and block B (manifest) in all six.
