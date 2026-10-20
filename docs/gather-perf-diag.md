# gather-perf-diag.sh

Bundles recent run directories (OS metrics, psar, hardware inventory, database collectors) into one `.tar.gz` or `.zip` that you copy to a workstation and analyse with [perfdiag](perfdiag.md). It samples nothing and makes no network call.

Location: `linux/os/gather-perf-diag.sh`

## What it does

1. Finds run directories in the search bases (below) and selects them by the timestamp in their name.
2. Includes the newest hardware inventory, or runs `collect-hw-inventory.sh` if there is none that is recent enough.
3. Converts every `sa_<timestamp>` file to CSV with `sadf -d -U`, using the `sadf` on the host. The raw `sa` file stays in the bundle.
4. Replaces each `pidstat` log in the bundle with a redacted copy. The original is not changed.
5. Extracts kernel log lines that matter for performance problems.
6. Checks whether a database server is running and, if its collector left no run in the selection, says so with a `NOTE:`.
7. Writes `gather_manifest.txt` with a SHA-256 for every file, and creates the bundle.

The CSV export happens on the server because `sysstat` does not build on macOS, and an older `sadf` cannot read a file written by a newer `sadc`. Exporting with the `sadf` that matches the `sadc` removes the question.

## Requirements

- `bash`, `tar` and `gzip`. These are present on every target distribution.
- `sadf` (from `sysstat`) to produce the CSV exports. Without it the raw `sa` file is bundled and a `NOTE:` says so.
- GNU `sed` for redaction. If redaction fails, the `pidstat` log is left out of the bundle rather than copied unredacted.
- `zip`, only for `--format zip`. Minimal RHEL, Amazon Linux, Azure Linux and Ubuntu cloud images do not have it, so the default is `tar.gz`.
- Root is optional. It is needed only for the kernel log on hosts with `kernel.dmesg_restrict=1` and for run directories created by other users. Without it those parts are skipped with a `NOTE:`. A search directory that exists but cannot be read is named in a `NOTE:` too.

## Quick start

```bash
sudo ./gather-perf-diag.sh                      # last 7 days, written to the current directory
sudo ./gather-perf-diag.sh --since 6h -o /tmp
sudo ./gather-perf-diag.sh --run /var/log/perf-diag/db01_os_20261016_093000
```

Copy the printed `.tar.gz` to your workstation.

## Options

| Option | Meaning | Default |
|--------|---------|---------|
| `--since` | Include run directories whose name timestamp is within this period: `2h`, `3d`, or seconds | `7d` |
| `--run DIR` | Include this run directory. Repeatable. When given, `--since` is ignored | |
| `--base DIR` | Where run directories are found. Repeatable. When given, only the named directories are searched | The default search bases, below |
| `-o`, `--outdir` | Where the bundle is written | Current directory |
| `--format` | `tar.gz` or `zip` | `tar.gz` |
| `--no-kernel-log` | Skip the kernel log excerpt | Off |
| `--no-redact` | Copy `pidstat` logs without redaction | Off |
| `--no-inventory` | Do not run or require a hardware inventory | Off |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

`-d`, `-c` and `-i` do not apply and are usage errors.

Staging happens in a hidden directory inside `--outdir`, not in `/tmp`, because bundles can be large and `/tmp` is often a small tmpfs. It is removed on exit.

## Search bases

Without `--base`, run directories are looked for in:

| Directory | Why |
|-----------|-----|
| `/var/log/perf-diag` | Where `collect-os-metrics.sh`, `psar.sh` and `collect-hw-inventory.sh` write by default |
| `~/perf-diag` of the invoking user | Where a collector run without root falls back to |
| `~/perf-diag` of `SUDO_USER` | The user who started this script with `sudo`; under `sudo` the home directory is often root's |
| `~/perf-diag` of the `postgres`, `mysql`, `oracle` and `mssql` accounts | The database collectors refuse to run under `sudo`, so they are usually started as the service account, and `/var/log/perf-diag` is root-owned. The run lands in that account's home |

The home directory of an account comes from `getent passwd`. An account that does not exist, and a base directory that does not exist, are skipped without a message. Two entries that are the same directory are searched once.

A base that exists but cannot be read gives `NOTE: cannot read <dir> (run with sudo to include it)`. The same note appears when the account's home directory cannot be entered (`/var/lib/pgsql` is usually mode 700), because the base may exist behind it. Run the script with `sudo` to include those.

`--base` can be given more than once. When it is given, only the named directories are searched and the defaults are not added. A named directory that does not exist is an error. The directories searched are recorded in `gather_manifest.txt` as `base.1`, `base.2` and so on. With `--run` nothing is searched for runs, and no `base.<n>` keys are written.

## Run directory selection

Directories in every search base are matched by name, `<host>_<tag>_<YYYYmmdd_HHMMSS>`, not by modification time: copying files changes `mtime`, the name does not. Earlier `gather` bundles are never included. Hardware inventory directories are not copied as ordinary runs. Only the newest one is bundled (see below).

## Database runs and detection

The database collectors (`collect-postgresql-metrics.sh`, `collect-mysql-metrics.sh`, `collect-oracle-metrics.sh`, `collect-mssql-metrics.sh`) write `<host>_<tag>_<ts>` directories with the tags `postgresql`, `mysql`, `oracle` and `mssql`. They are selected, window-checked and copied like any other run, whole: the long-format CSV files (`pg_*.csv`, `mysql_*.csv`, `oracle_*.csv`, `mssql_*.csv`), `db_paths.txt`, `manifest.txt` and `errors.log` are all in the bundle. Only `*.pid` files are dropped. Their manifests already replace the value of `--connect` with `<redacted>`, so gather adds no redaction.

A database collector writes to `/var/log/perf-diag` when it can, and otherwise to `~/perf-diag`, with a `NOTE:` that says this script finds runs there. `collect-os-metrics.sh` creates `/var/log/perf-diag` root-owned and hands only the run directory to the invoking user, so a collector started without `sudo` normally lands in the home directory. The search bases above are the other half of that note: without them a database run would never reach the bundle.

At gather time the script lists processes with `ps -eo pid=,ppid=,user=,args=` and matches the base name of the first argument, not `comm` (which is cut at 15 characters):

| Engine | Process |
|--------|---------|
| `postgresql` | `postgres` or `postmaster`, including retitled backends (`postgres: ...`) |
| `mysql` | `mysqld`, `mariadbd`. Not `mysqld_safe` or `mysqld_exporter` |
| `oracle` | `ora_pmon_<SID>` or `oracle<SID>`. An ASM instance (`asm_pmon_+ASM`) is not a database |
| `mssql` | `sqlservr` |

For each engine found with no run of its tag in the selection, a `NOTE:` is printed and kept in `gather_manifest.txt`:

```
NOTE: PostgreSQL is running on this host but the bundle has no postgresql run; perfdiag will use process data only. Run linux/db/collect-postgresql-metrics.sh alongside collect-os-metrics.sh next time.
```

`gather_manifest.txt` also records `db_detected=postgresql,mysql` (the engines found, whether or not they have a run). On a host with no database server nothing is printed and the key is absent, so the output is the same as before this feature.

Detection is only a pointer. It starts nothing and logs in nowhere.

## Hardware inventory

The bundle should hold one `<host>_hw_<ts>` directory no older than the oldest selected run.

- If one exists among the selected directories (or in a search base, when `--run` is used), the newest is included.
- If none exists or the newest is older than the oldest run, and `collect-hw-inventory.sh` is in the same directory as this script or on `PATH`, it is run and its output is bundled.
- If it cannot be run, an older inventory is still bundled with a `NOTE:`. With none at all the script prints `WARNING: no hardware inventory; disk thresholds will use defaults.`

## Bundle layout

```
<host>_gather_<ts>/
    gather_manifest.txt
    kernel_events.log
    <host>_hw_<ts>/              copied as-is
    <host>_os_<ts>/
        manifest.txt  readback.txt
        sa_<ts>                  raw sadc file, unchanged
        pidstat_<ts>.log         redacted copy
        sadf/
            cpu.csv  load.csv  psi_cpu.csv  psi_io.csv  psi_mem.csv  cswch.csv
            memory.csv  paging.csv  swap_space.csv  swap_rate.csv
            disk.csv  filesystem.csv
            net_dev.csv  net_edev.csv  net_tcp.csv  net_etcp.csv  net_sock.csv  net_soft.csv
            interrupts.csv  fchost.csv
    <host>_psar_<ts>/            copied as-is
    <host>_<db>_<ts>/            database collector run (tag postgresql, mysql, oracle or mssql), copied as-is
        manifest.txt  errors.log  db_paths.txt
        pg_*.csv / mysql_*.csv / oracle_*.csv / mssql_*.csv
```

Memory is exported with `sadf -r ALL`. sysstat 10.x (RHEL 7, Ubuntu 14.04) does not accept `ALL` there and treats it as a second file name, so the script retries with plain `-r` and records a `NOTE:` saying which form worked. `perfdiag` makes the same retry when it converts an `sa` file itself.

A CSV exists only when the activity is in the `sa` file and has data. `interrupts.csv` needs `collect-os-metrics.sh --extended`, `fchost.csv` needs Fibre Channel HBAs, and `psi_*.csv` need `sysstat` 12.3.3 and a kernel with pressure stall information. Each missing one produces a `NOTE:` and no empty file.

## Kernel log excerpt

`kernel_events.log` covers the earliest run start to the latest run end, plus 5 minutes each side. Sources are tried in order and the first that works is used:

1. `journalctl -k -o short-iso` with `--since` and `--until`, when systemd is init.
2. `/var/log/kern.log`.
3. `/var/log/messages`.
4. `dmesg`, with timestamps relative to boot. The file records a `boot_epoch` so `perfdiag` can convert them.

Only lines matching these patterns are kept (case-insensitive): out of memory, OOM kill, killed process, hung task, blocked for more than, soft or hard lockup, I/O error, `blk_update_request`, NVMe or SCSI timeout, abort or reset, ext4 or XFS errors, md RAID failures, multipath failures, link down, `nf_conntrack` table full, page allocation failure, CPU clock throttled. The two plain-file sources are not filtered by time; the newest 2000 matching lines are kept and `perfdiag` applies the window.

## Redaction

`pidstat -l` records full command lines, which can contain credentials. The copies in the bundle have these replaced with `<redacted>`:

- the value of `--password`, `--passwd`, `--pwd`, `--secret`, `--token` and `--api-key` (also with one dash, and `=` or a space);
- `-p<value>` arguments, such as `mysql -psecret`;
- the value of environment assignments whose name contains `PASSWORD`, `SECRET` or `TOKEN`, such as `PGPASSWORD=abc`.

The `-p` rule is aggressive and also rewrites harmless values such as `-p5432`. The process name and other arguments still identify the process.

**Host names, IP addresses and file paths are not redacted.** Read the `pidstat` log before sending a bundle outside your organisation. `--no-redact` keeps everything and prints a warning.

## gather_manifest.txt

`key=value`, the same shape as `manifest.txt`.

| Key | Value |
|-----|-------|
| `script`, `script_version`, `hostname`, `start` and the other run keys | As for the collectors |
| `base.<n>` | Each search base that was searched, in order (not written with `--run`) |
| `run.<n>` | Directory name of each included run (the inventory counts as one) |
| `run.<n>.bytes` | Size of that directory |
| `window_start`, `window_end` | ISO 8601, from the run manifests |
| `db_detected` | Database engines found running on the host, comma separated. Absent when none |
| `kernel_log_source` | `journalctl`, `kern.log`, `messages`, `dmesg` or `none` |
| `redacted` | `yes` or `no` |
| `sha256.<relative path>` | SHA-256 of every file in the bundle except this one, so a damaged transfer is detectable |
| `notes` | Every `NOTE:` printed |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Bundle written |
| 1 | Runtime error: no run directories selected, a `--base` directory that does not exist, `--format zip` without `zip`, unwritable output directory |
| 2 | Usage error |
