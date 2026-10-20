# psar.sh

A `sar`-like live sampler that needs no `sysstat` package, no root and no Python. It reads `/proc` directly and prints CPU, memory, disk, network and load figures to the terminal. It can also write the same figures to CSV files.

Use it on hosts where you cannot install `sysstat`, or when you want a quick live view. For long unattended captures use [collect-os-metrics.sh](collect-os-metrics.md), which runs detached.

Location: `linux/os/psar.sh`

## Requirements

- Linux with `/proc` and `/sys/block`.
- `bash` 4.1 or later and a POSIX `awk` (`mawk` and `gawk` both work).
- No root is needed to read the figures. `-o` needs a directory you can write to.

## Quick start

```bash
./psar.sh                      # every section, every 2 seconds, until Ctrl-C
./psar.sh -D -c 5              # disks only, 5 samples
./psar.sh -A --dev vda -d 30s  # every section for 30 seconds, disk section limited to vda
```

The first line appears one interval after start. `psar.sh` takes a baseline snapshot, waits, takes a second snapshot, and reports the difference.

## Options

Long options accept `--opt value` and `--opt=value`. Short flags are not bundled: use `-C -M`, not `-CM`.

### Timing

| Option | Meaning | Default |
|--------|---------|---------|
| `-i`, `--interval` | Sample interval. Units `s`, `m`. A bare number is seconds. Minimum 1 second | 2 seconds |
| `-d`, `--duration` | Total run time. Units `s`, `m`, `h`, `d`. Cannot be combined with `-c` | Until stopped |
| `-c`, `--count` | Number of samples. `0` means until stopped | `0` |

`-d` is converted to a sample count (`duration / interval`, rounded down). Duration must be at least one interval.

### Sections

| Option | Section |
|--------|---------|
| `-C` | CPU |
| `-M` | Memory |
| `-D` | Disk |
| `-N` | Network |
| `-L` | Load average |
| `-A` | All sections |

- With no section flag, all sections are shown.
- Section flags add to each other: `-C -M` shows CPU and memory.
- `-A` always shows every section.
- `--dev` implies `-D`. `--iface` implies `-N`. They add their section to any others you chose.
- `--dev vda` on its own shows only the disk section. `-A --dev vda` shows every section with the disk section limited to `vda`.

### Selecting devices

| Option | Meaning |
|--------|---------|
| `--dev LIST` | Disks to report, by kernel name, comma separated: `vda`, `sda`, `nvme0n1`, `dm-0` |
| `--iface LIST` | Network interfaces to report, comma separated |

**Default disks:** every whole disk listed in `/sys/block`, excluding `loop*`, `ram*`, `zram*`, `sr*` and `fd*`. Partitions are never listed.

**Display names:** LVM, LUKS and multipath devices (`dm-N`) are shown by their mapper name. You still select them by kernel name (`--dev dm-0`).

**Default interfaces:** every interface except `lo`, `veth*`, `docker*`, `virbr*`, `cni*` and `br-*`. Use `--iface all` for every interface except `lo`. Name `lo` explicitly to include it. `all` can be mixed with names.

A name that does not exist is skipped with a `WARNING:`. If nothing is left to report, the script exits with an error.

### Output

| Option | Meaning |
|--------|---------|
| `--header-once` | Print the column header once, like `vmstat`. Ignored with a warning when more than one section is active |
| `-o`, `--outdir DIR` | Also write CSV files and a manifest. See [CSV output](#csv-output) |
| `-h`, `--help` | Print usage and exit |
| `-V`, `--version` | Print name and version and exit |

## Console output

Each sample prints one block per active section, with a header line, a data line (one per disk or interface for those sections) and a blank line. The timestamp is the local time, `HH:MM:SS`.

```
14:57:49 CPU       %usr    %sys    %irq   %soft %iowait  %steal   %idle   run  blkd      cs/s
14:57:49 all        1.0     0.2     0.0     0.0     0.0     0.2    98.5     1     0     415.8

14:57:49 MEM        usedkB    availkB  %used   dirtykB   wbackkB  swusedkB  swpin/s swpout/s majflt/s
14:57:49 -          479416   16000008    2.9       172         0         0      0.0      0.0      1.0

14:57:49 DEV         r/s      w/s      rkB/s      wkB/s rareq-sz wareq-sz  r_await  w_await  aqu-sz  %util      d/s      dkB/s      f/s
14:57:49 vda         2.0      0.0       35.6        0.0     18.0      0.0     0.00     0.00    0.00    0.0      0.0        0.0      0.0

14:57:49 IFACE      rxkB/s     txkB/s   rxpck/s   txpck/s   rxerr/s   txerr/s  rxdrop/s  txdrop/s
14:57:49 eth0          2.2        1.7      25.7      25.7       0.0       0.0       0.0       0.0

14:57:49 LOAD       1m     5m    15m
14:57:49 -        0.00   0.00   0.00
```

Rates are the change between two snapshots divided by the real time that passed (from `/proc/uptime`), not by the requested interval. A counter that goes backwards (reset or wrap) is reported as 0 for that sample.

### CPU

| Column | Meaning |
|--------|---------|
| `%usr` | User time including nice |
| `%sys` | Kernel time |
| `%irq`, `%soft` | Hardware and software interrupt time |
| `%iowait` | Idle time with I/O outstanding |
| `%steal` | Time taken by the hypervisor |
| `%idle` | Idle time |
| `run`, `blkd` | Runnable and blocked tasks at the moment of sampling (`vmstat` `r` and `b`) |
| `cs/s` | Context switches per second |

The CPU row is labelled `all`. There is no per-CPU breakdown. For that, use `collect-os-metrics.sh`.

### Memory

| Column | Meaning |
|--------|---------|
| `usedkB` | `MemTotal - MemAvailable` |
| `availkB` | `MemAvailable` |
| `%used` | `usedkB` as a percentage of `MemTotal` |
| `dirtykB`, `wbackkB` | `Dirty` and `Writeback` from `/proc/meminfo` |
| `swusedkB` | Swap in use |
| `swpin/s`, `swpout/s` | Pages swapped in and out per second |
| `majflt/s` | Major page faults per second |

On kernels older than 3.14 (no `MemAvailable`), the script uses `MemFree + Buffers + Cached` and prints a `NOTE:`.

### Disk

| Column | Meaning |
|--------|---------|
| `r/s`, `w/s` | Completed read and write requests per second |
| `rkB/s`, `wkB/s` | Throughput |
| `rareq-sz`, `wareq-sz` | Average request size, kB |
| `r_await`, `w_await` | Average time per request including queue time, ms |
| `aqu-sz` | Average queue depth |
| `%util` | Share of time with at least one request in flight (capped at 100) |
| `d/s`, `dkB/s` | Discards per second and discarded kB per second. Kernel 4.18+ only |
| `f/s` | Flushes per second. Kernel 5.5+ only |

On older kernels the missing columns are left out and a `NOTE:` is printed.

Read `%util` with care. It indicates saturation only on single-queue devices such as spinning disks. NVMe, SSD arrays and cloud block storage handle many requests in parallel and can be far from saturated at 100%. For those, look at `aqu-sz` and the `await` columns.

Stacked devices are counted at every layer. A write to an LVM volume on `vda3` appears on the mapper device and on `vda`, as in `sar -d` and `iostat`.

### Network

Per interface: receive and transmit throughput (`rxkB/s`, `txkB/s`), packets (`rxpck/s`, `txpck/s`), errors (`rxerr/s`, `txerr/s`) and drops (`rxdrop/s`, `txdrop/s`).

### Load

The 1, 5 and 15 minute load averages from `/proc/loadavg`. These are values, not rates.

## CSV output

`-o DIR` writes files in addition to the console output. The console output is unchanged.

```
DIR/<hostname>_psar_<YYYYmmdd_HHMMSS>/
    cpu.csv  mem.csv  disk.csv  net.csv  load.csv     active sections only
    manifest.txt
```

- Each run has its own directory, so runs do not overwrite each other.
- `DIR` is created if it does not exist. If it is not writable the script exits with an error. Pass another directory or run with `sudo`.
- The first column is `ts`, an ISO 8601 timestamp (`2026-10-16T14:57:49+00:00`).
- `disk.csv` has a `device` column after `ts`. `net.csv` has an `iface` column. They have one row per device per sample.
- Column names follow the console headers, with `/` replaced by `_` and `%` replaced by an `_pct` suffix: `r/s` becomes `r_s`, `%util` becomes `util_pct`.

| File | Columns |
|------|---------|
| `cpu.csv` | `ts,usr_pct,sys_pct,irq_pct,soft_pct,iowait_pct,steal_pct,idle_pct,run,blkd,cs_s` |
| `mem.csv` | `ts,usedkB,availkB,used_pct,dirtykB,wbackkB,swusedkB,swpin_s,swpout_s,majflt_s` |
| `disk.csv` | `ts,device,r_s,w_s,rkB_s,wkB_s,rareq-sz,wareq-sz,r_await,w_await,aqu-sz,util_pct` plus `d_s,dkB_s` (kernel 4.18+) and `f_s` (kernel 5.5+) |
| `net.csv` | `ts,iface,rxkB_s,txkB_s,rxpck_s,txpck_s,rxerr_s,txerr_s,rxdrop_s,txdrop_s` |
| `load.csv` | `ts,1m,5m,15m` |

`manifest.txt` is `key=value`: script and version, command line, host, distro, kernel, CPU count, memory, timezone (a fixed offset such as `BST +0100`), `tz_name` (the IANA zone name such as `Europe/London`, empty when it cannot be found), start time, interval, samples and duration (`unlimited` when run until stopped), `psi`, the sections, the disks (`kernel=display` pairs), the interfaces and `notes=`. `end=` is added when sampling finishes or is stopped.

## Stopping

`psar.sh` always runs in the foreground. Press Ctrl-C to stop. With `-o`, the manifest receives its `end=` line.

For a long unattended capture, use `collect-os-metrics.sh`, or start `psar.sh` inside `tmux` or `screen`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Normal exit |
| 1 | Runtime error: no reportable disks or interfaces, cannot create the output directory |
| 2 | Usage error |

## Options from earlier versions

These now fail with exit code 2 and name their replacement.

| Old | Now |
|-----|-----|
| `-d LIST` (disks) | `--dev LIST` |
| `-e LIST` | `--iface LIST` |
| `-n` | `--header-once` |
| `-w DIR` | `-o DIR` |

`./psar.sh -d vda` is a usage error because `-d` is now the duration.

## Examples

```bash
./psar.sh -C -M -i 5                  # CPU and memory every 5 seconds
./psar.sh --dev vda,vdb -i 1          # two disks, every second
./psar.sh --iface eth0 --header-once  # one interface, header printed once
./psar.sh -A -o ./metrics -d 10m      # all sections for 10 minutes, with CSV files
./psar.sh -N --iface all -c 10        # every interface except lo, 10 samples
```
