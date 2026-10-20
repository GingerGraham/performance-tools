# collect-os-metrics.sh

Records system-wide and per-process performance data with `sadc` and `pidstat` (both from the `sysstat` package) for a fixed period. Use it to capture a baseline, or to capture an incident window that you analyse afterwards.

Location: `linux/os/collect-os-metrics.sh`

## What it collects

| Tool | Data | Output file |
|------|------|-------------|
| `sadc` | CPU (per CPU), run queue and load, context switches, memory, paging, swap, block devices, network interfaces, and by default filesystems, TCP/UDP/IP counters and IPv6 counters | `sa_<timestamp>` (binary) |
| `pidstat` | Per-process CPU, memory, disk I/O and context switches, one line per process per interval, with the full command line. Kernel table usage (`-v`) and `-H` are added where the installed `pidstat` supports them | `pidstat_<timestamp>.log` (text) |

Both tools run for the same number of samples over the same window, so their data lines up.

## Requirements

- Linux with `/etc/os-release`.
- `sysstat` installed (provides `sadc`, `sar`, `pidstat`). On Fedora and RHEL family: `sudo dnf install sysstat`. The script prints the correct install command for the host if `sysstat` is missing.
- Root, or a user with `sudo`. The script runs `sudo -v` first and exits if sudo fails. It does not need the `sysstat` service to be enabled.
- `systemd` is used to run the collectors when it is the init system. Otherwise the script falls back to `setsid nohup`.

## Quick start

Collect for 2 hours at 30-second samples, with per-process data limited to PostgreSQL and PgBouncer:

```bash
./collect-os-metrics.sh -d 2h -i 30s -p postgres,pgbouncer
```

The script prints the output directory, then returns. Collection continues in the background and stops by itself.

Collect 20 samples at 5-second intervals and stay attached to the terminal:

```bash
./collect-os-metrics.sh -c 20 -i 5 --foreground
```

## Options

Long options accept `--opt value` and `--opt=value`. Short options are not bundled (`-d 2h -i 30s`, not `-di`).

| Option | Meaning | Default |
|--------|---------|---------|
| `-d`, `--duration` | Total run time. Units: `s`, `m`, `h`, `d`. A bare number is seconds | Prompted for |
| `-c`, `--count` | Number of samples. Cannot be combined with `-d` | Prompted for duration |
| `-i`, `--interval` | Time between samples. Units: `s`, `m`. A bare number is seconds. Minimum 1 second | Prompted for |
| `-o`, `--outdir` | Base directory for output | `/var/log/perf-diag` |
| `-p`, `--process` | Comma-separated process names. Limits `pidstat` to matching processes | All |
| `--extended` | Collect every `sadc` activity group (`-S XALL`) | Off |
| `--threads` | Add one row per thread to the `pidstat` log (`pidstat -t`) | Off |
| `--foreground` | Stay attached. Ctrl-C stops collection | Detached |
| `-h`, `--help` | Print usage and exit | |
| `-V`, `--version` | Print name and version and exit | |

### Timing

- Number of samples = `duration / interval`, rounded down. With `-c`, the run time is `count × interval`.
- Duration must be at least one interval.
- If `-d`/`-c` or `-i` is missing and the script is attached to a terminal, it asks for the value. When there is no terminal (cron, CI, a pipe), a missing value is a usage error.

### `-p`, `--process`

- Takes a list: `-p postgres,pgbouncer`. The names are joined with `|` and passed to `pidstat -C`, which matches them against the command name as a pattern.
- A name is therefore a match on part of the command name, not an exact match, and characters such as `.` act as pattern characters.
- Without `-p`, `pidstat` uses its default process selection (see `man pidstat`).
- `sadc` always records system-wide data. `-p` only affects the `pidstat` log.

### `--extended`

Replaces the default `sadc` groups (`XDISK`, `SNMP`, `IPV6`) with `XALL`. This adds per-interrupt and power counters. Output size grows with the CPU count, so use it only when you need interrupt data.

### `--threads`

Adds thread rows under each process. The `pidstat` log becomes much larger on hosts with many threads.

## Output

```
<outdir>/<hostname>_os_<YYYYmmdd_HHMMSS>/
    sa_<timestamp>            sadc binary data
    pidstat_<timestamp>.log   pidstat text log
    manifest.txt              what was run and where
    readback.txt              commands to read the data back
    sadc.pid, pidstat.pid     only when systemd is not init
    sadc.stderr, pidstat.stderr   only when systemd is not init
```

- `<hostname>` is the short hostname of the host being measured.
- The base directory is created with root privileges if it does not exist. The run directory is then handed to the invoking user. A relative `-o` is converted to an absolute path.
- `manifest.txt` is `key=value`, one per line: script and version, the command line, host, distro, kernel, CPU count, memory, timezone (a fixed offset such as `BST +0100`), `tz_name` (the IANA zone name such as `Europe/London`, empty when it cannot be found: `perfdiag` uses it to follow a daylight-saving change in a multi-day run), start time, interval, samples, duration, whether pressure stall information (`psi`) exists, sysstat version, the `sadc` keywords and `pidstat` flags actually used, the launcher (`systemd`, `nohup` or `foreground`), and `notes=`.
- `end=` is added to the manifest only for `--foreground` runs. A detached run cannot write it.

## Optional features and `NOTE:` messages

The script tests each optional feature on the host and skips it with a `NOTE:` on stderr if it is not supported. Skipped features are listed in `notes=` in the manifest.

| Feature | Skipped when |
|---------|--------------|
| `sadc -S XDISK`, `SNMP`, `IPV6` (or `XALL`) | The installed `sadc` does not accept the keyword |
| `pidstat -v`, `pidstat -H` | The installed `pidstat` does not support the flag |

If none of the keywords are accepted, the script falls back to `-S DISK`.

## Running and stopping

**systemd hosts (default).** Two transient units run the collectors:

```bash
systemctl status perf-diag-sadc-<timestamp> perf-diag-pidstat-<timestamp>
sudo systemctl stop perf-diag-sadc-<timestamp> perf-diag-pidstat-<timestamp>   # stop early
```

The exact commands are printed when the script starts.

**Hosts without systemd as init** (for example containers and Ubuntu 14.04). Collection uses `setsid nohup` and PID files:

```bash
ps -p $(cat <outdir>/sadc.pid),$(cat <outdir>/pidstat.pid)
sudo kill $(cat <outdir>/sadc.pid) $(cat <outdir>/pidstat.pid)
```

Omit `sudo` if you started the script as root.

**`--foreground`.** Ctrl-C stops both collectors and writes `end=` to the manifest.

A detached run keeps going after you log out.

## Reading the data back

The commands are in `readback.txt` in the run directory, with the file paths filled in. They include:

```bash
LC_ALL=C sar -f <outdir>/sa_<timestamp> -u ALL -P ALL    # CPU, per CPU
LC_ALL=C sar -f <outdir>/sa_<timestamp> -r ALL           # memory
LC_ALL=C sar -f <outdir>/sa_<timestamp> -d -p            # block devices
LC_ALL=C sar -f <outdir>/sa_<timestamp> -n DEV,EDEV      # network interfaces
less <outdir>/pidstat_<timestamp>.log
```

`readback.txt` lists only the reports that match what was collected. For example, filesystem, TCP/IP and IPv6 reports appear only when their groups were collected, and the interrupt report (`-I ALL`) appears only with `--extended`.

To export CSV for analysis, `readback.txt` also gives `sadf -d` commands (semicolon-separated output, one activity group per file). For analysis away from the host, [gather-perf-diag.sh](gather-perf-diag.md) bundles the run directory with all the CSV exports, and [perfdiag](perfdiag.md) analyses the bundle on a workstation. `readback.txt` ends with a line pointing at it.

The binary file can be read on any host with the same or a newer `sysstat` version.

The `pidstat` log is plain text. The flags that were actually used are recorded in `pidstat_flags=` in the manifest, which tells you which column set to expect.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (the collectors were started, or a foreground run finished) |
| 1 | Runtime error: no sudo, `sysstat` missing or incomplete, no `/etc/os-release` |
| 2 | Usage error: unknown option, missing value, bad duration/interval, `-d` with `-c` |

## Troubleshooting

| Message | Cause and fix |
|---------|---------------|
| `sysstat is not installed (sadc binary not found)` | Install `sysstat` using the command printed below the message |
| `sysstat appears partially installed` | `sar` or `pidstat` is missing from `PATH`. Reinstall `sysstat` |
| `this script needs root to run sadc and pidstat` | Run as root, or as a user with sudo rights |
| `cannot read /etc/os-release` | Hosts without this file (RHEL/CentOS 6 and older) are not supported |
| `unrecognised distro` (warning) | The script continues. Package and path detection may be wrong |
| `no -d/--duration or -c/--count given and no terminal to prompt on` | Pass `-d` or `-c`, and `-i`, when running from cron or a pipe |

## Examples

```bash
# Two-hour baseline, all processes
./collect-os-metrics.sh -d 2h -i 30s

# One hour at 10 seconds, with interrupt counters, to a custom directory
./collect-os-metrics.sh -d 1h -i 10s --extended -o /data/perf

# Per-thread data for the database during a 10-minute test
./collect-os-metrics.sh -d 10m -i 5s -p postgres --threads

# Fixed number of samples
./collect-os-metrics.sh -c 60 -i 1m
```
