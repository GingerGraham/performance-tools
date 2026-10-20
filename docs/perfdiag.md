# perfdiag

Analyses a bundle from [gather-perf-diag.sh](gather-perf-diag.md) on a workstation. It ranks problems by how far over a threshold and for how long, groups them into hypotheses with next steps, and writes charts and a single-file HTML report.

Location: `linux/analysis/perfdiag.py` (the package is `linux/analysis/perfdiag/`)

## Requirements

- Python 3.9 or newer. Parsing, analysis, `findings.json` and text output use the standard library only.
- `matplotlib` for `plot` and `report`. Without it, `analyse` still runs and `plot` and `report` stop with install steps.
- `sadf` is optional. It is used only for older bundles that have a raw `sa` file but no `sadf/` exports. macOS has no `sysstat`, so re-run `gather-perf-diag.sh` on the server instead.

Install matplotlib:

| Platform | Command |
|----------|---------|
| Fedora, RHEL 9+, Rocky, Alma | `sudo dnf install python3-matplotlib` |
| Debian, Ubuntu, WSL Ubuntu | `sudo apt install python3-matplotlib` |
| macOS (Homebrew Python refuses global `pip install`) | `python3 -m venv ~/.venvs/perfdiag && ~/.venvs/perfdiag/bin/pip install matplotlib` |
| Any, when the distribution package is old | the same venv command |

`perfdiag check` prints what is installed and the command for anything missing.

## Quick start

```bash
cd linux/analysis
python3 perfdiag.py check
python3 perfdiag.py inspect  db01_gather_20261016_103000.tar.gz
python3 perfdiag.py analyse  db01_gather_20261016_103000.tar.gz
python3 perfdiag.py report   db01_gather_20261016_103000.tar.gz -o out/
```

Open `out/report.html` in any browser. It has no scripts and no external resources, so it also works offline and attaches to a ticket.

## Subcommands

```
perfdiag check
perfdiag inspect BUNDLE
perfdiag analyse BUNDLE [options] [--json FILE]
perfdiag plot    BUNDLE -o DIR [--format svg|png] [options]
perfdiag report  BUNDLE -o DIR [--format html|md|both] [options]
```

`BUNDLE` is a `.tar.gz`, a `.zip`, a gather directory or a single run directory. All subcommands also accept `--keep-extracted`, which keeps the temporary extraction directory and prints its path, and `--no-verify`, which skips the checksum check (see "Bundles are untrusted input").

| Subcommand | Output |
|------------|--------|
| `check` | Python version, matplotlib and `sadf`, with install hints. Exit 1 when a required item is missing |
| `inspect` | Hosts, runs, time range, interval, activities present, PSI, inventory summary and the `NOTE:` lines from the manifests |
| `analyse` | Ranked hypotheses and episodes as text. `--json FILE` writes `findings.json` (for several hosts the host name is added to the file name) |
| `plot` | One chart per indicator group, plus the indicator timeline |
| `report` | `report.html` and/or `report.md` with `charts/*.png`, and `findings.json`. Several hosts give one report each plus `index.html` |

### Options for `analyse`, `plot` and `report`

| Option | Meaning |
|--------|---------|
| `--config FILE` | INI file with overrides (below) |
| `--set KEY=VALUE` | Override any setting. Repeatable |
| `--from TIME`, `--to TIME` | Limit the analysis. `HH:MM`, `YYYY-mm-dd HH:MM` (host zone) or epoch seconds |
| `--tz ZONE` | Display zone: `UTC` or an offset such as `+01:00`. Default: the host's zone from its manifest |
| `--latency CLASS=WARN[,CRIT]` | Latency thresholds in ms for a media class, e.g. `--latency nvme=1,5` |
| `--device-limit DEV:iops=N,mibps=N` | Declared device limits. Enables limit-based throughput and IOPS rules. `mibps` is MiB/s, as AWS and Azure publish volume limits. `mbps` is refused: it reads as megabits |
| `--spindles DEV=N` | Parallelism of a hardware RAID device |
| `--class DEV=CLASS` | Override the detected media class of a device |
| `--sustain DURATION` | Sustain window, e.g. `10m` |
| `--include-partitions` | Analyse partitions (`sda1`) as separate devices, judged as their parent disk. Default: partitions are folded into the disk |

Exit codes: 0 success, 1 runtime error (bad bundle, missing matplotlib for `plot` and `report`), 2 usage or setting error.

## How the analysis works

### Duration before magnitude

A bottleneck is a resource that stays over its limit long enough to queue work. For every indicator, an **episode** is a run of samples past the warning threshold. Up to `gap_samples` samples that dip back under are allowed inside one run. A gap in the data longer than twice the sample interval always ends a run.

- **Sustained**: at least `sustain_s` long (default 300 s) and at least 3 samples.
- **Transient**: anything shorter.
- **Severity** is `crit` when the **median** of the run is past the critical threshold, so one extreme sample cannot make a run critical.
- **Score** is the sum over the run of the distance past warn, times the interval: how far over, for how long. Distance is measured in units of the warn-to-crit band and capped at `analysis.exceedance_cap` (default 3), so a value as far past warn as crit is counts 1.0. That puts rules that fire when a value is too low (idle CPU, available memory) on the same scale as rules that fire when it is too high, and stops one unbounded rule outranking the rest. With crit at twice warn, a 5-second burst to 2× the threshold scores 5 and forty-five seconds at 1.5× scores 22.5. Findings are ranked by score, not by peak.

If `sustain_s` is less than 3 sample intervals, the interval is too coarse to tell sustained from transient. The window is raised to 3 intervals, a `NOTE:` is printed and every finding is labelled interval-limited.

### Transients: absorbed or impactful

A transient is checked against its paired impact indicators for the same samples (plus or minus one interval).

| Transient on | Paired impact indicators |
|--------------|--------------------------|
| Disk throughput or IOPS | Same device: latency, and queue depth for HDD-class devices |
| CPU busy, run queue | CPU pressure (PSI), load per CPU |
| Available memory low | Swap-out, direct reclaim, major faults, memory pressure |
| Latency, queue, PSI, swap-out, steal | Themselves: always listed |

A transient with no paired breach is **absorbed**: it is counted ("12 throughput bursts, all absorbed") and not listed. Repeated impactful transients are summarised ("CPU idle fell below 20 % in 1514 bursts (total 5 h 2 min, longest 2 min) between 2026-10-16 15:14 and 2026-10-23 15:09 UTC") and can raise a hypothesis, followed by the worst burst with its peak and how many of its samples were past the threshold. At least 3 impactful bursts are needed, and together they must last at least the sustain window or `analysis.recurring_min_pct` (default 1) percent of the analysed window, whichever is longer. Three blips in a week are not a finding. Dates are shown when the window is longer than 20 hours.

### Disk rules depend on the media class

The class comes from the hardware inventory and can be overridden. It sets the latency thresholds:

| Class | Warn ms | Crit ms |
|-------|---------|---------|
| `nvme` | 2 | 10 |
| `ssd` | 5 | 20 |
| `hdd`, `hw-raid` | 20 | 50 |
| `virtual`, `san`, `cloud-ebs`, `cloud-azure`, `unknown` | 10 | 30 |
| `stacked` (LVM, LUKS, md) | the lowest warn and crit among its member disks |

- **Partitions** (`sda1`, `nvme0n1p3`) are folded into their parent disk and not analysed on their own: the inventory lists them, or the name decides when there is no inventory. A host note lists what was left out. `--include-partitions` keeps them. A partition whose parent disk has no data of its own is kept.
- Samples with fewer than `analysis.disk_min_tps` IO/s (default 5) are ignored for latency.
- Queue depth counts only for `hdd` and `hw-raid`: warn above 2 × parallelism with latency at its warn value, crit at 4 × parallelism with latency at its crit value. `aqu-sz` on NVMe and cloud volumes is normal under concurrency.
- `%util` counts only for `hdd` (warn 80, crit 95). On other media it is not a saturation signal.
- Throughput and IOPS have no threshold unless a limit is declared (`--device-limit`, warn at 80 % and crit at 95 % of it) or an absolute threshold is set (`disk.read_kBps.warn`, `disk.write_kBps.warn`, `disk.throughput_kBps.warn`, `disk.iops.warn`, each with a `.crit`). Those keys are kB/s as sar reports it, where 1 kB = 1024 bytes. Output and charts use MiB/s and GiB/s.
- **Plateau detection** looks for a limit nobody told you about. A sustained window counts when throughput (or IOPS) is flat (coefficient of variation below `analysis.plateau_cv`, default 0.10), the window mean is at least 90 % of the run's p99, and latency in the window is at least twice the device's median latency and at least the class warn value. A steady workload is flat too, so the latency condition is what separates a cap from a busy disk. For cloud volumes the same test runs on the sum across all of them (`cloud-sum`). A plateau on a `cloud-ebs`, `cloud-azure`, `virtual`, `san` or `unknown` device can raise H2. On other classes (`hdd`, `ssd`, `nvme`, `hw-raid`) it is only evidence for H1 ("throughput was flat at 78 MiB/s while latency rose: the device was working at its limit") and appears in the timeline only when a hypothesis refers to it.
- A stacked device whose latency exceeds its slowest member's by more than 2× and 5 ms raises a stacking-layer indicator.
- **One hypothesis per physical problem.** Latency on `sda` also shows on the LVM volume above it. When a stacked device and exactly one of its members have an H1 (or H7) over the same window, only the member is listed, with "Also seen on: rhel-root (/), await averaged 35.0 ms over the same window". When several members are slow (a RAID array), the array is listed and the members are folded into it. When the members are normal, the stacked device stays: the latency was added above them.
- **SAN LUNs** (`san`) are iSCSI or FC disks and disks from storage arrays. Like cloud volumes they get no `%util` or queue rule, and the next steps point at the array and the multipath state.

### CPU, memory and pressure rules

| Rule | Indicator | Warn | Crit |
|------|-----------|------|------|
| `cpu_busy` | all-CPU `%idle` below | 20 | 5 |
| `cpu_single` | one CPU's `%idle` below, while all-CPU `%idle` is above 40 | 5 | 1 |
| `cpu_steal` | `%steal` above | 5 | 15 |
| `cpu_soft_single` | one CPU's `%soft` above | 30 | 60 |
| `runq` | run queue per CPU above | 1.0 | 2.0 |
| `load` (supporting) | 1-minute load per CPU above | 1.0 | 2.0 |
| `psi_cpu` | CPU pressure `%scpu` above | 20 | 50 |
| `mem_avail` (supporting) | available memory as % of RAM below | 10 | 5 |
| `swap_out` | `pswpout/s` above | 10 | 100 |
| `direct_reclaim` | `pgscand/s` above | 100 | 1000 |
| `major_faults` (supporting) | `majflt/s` above | 50 | 500 |
| `psi_mem_some`, `psi_mem_full` | memory pressure above | 5, 1 | 20, 5 |
| `dirty` (supporting) | dirty pages as % of RAM above | 10 | 20 |
| `psi_io_some`, `psi_io_full` | IO pressure above | 10, 5 | 30, 15 |

Supporting indicators are reported and used as evidence but never raise a hypothesis alone. `iowait`, `blocked` tasks, context switches (twice the run median), swap-in and `commit` are also supporting only. `%memused` is not used because page cache makes it near 100 % on a healthy host.

A load indicator without a latency indicator is not a problem. High throughput, IOPS or CPU with normal latency, queue and run queue is a busy system, not a bottleneck.

All numbers are engineering starting points, not vendor figures, except where noted. Every one is a setting (below).

## Hypotheses

A hypothesis is raised when its primary indicator has a sustained episode, or repeated impactful transients. Supporting and contradicting indicators count when they overlap the primary window by at least half of the shorter of the two windows, with one interval of tolerance at each end.

| Confidence | Condition |
|------------|-----------|
| high | at least 2 supporting, none contradicting |
| medium | 1 supporting, or at least 2 supporting with 1 contradicting |
| low | primary only, or contradicting at least equal to supporting |

| # | Hypothesis | Primary | Contradicted by |
|---|------------|---------|-----------------|
| H1 | Storage device saturated | disk latency on a device | |
| H2 | Cloud volume or instance storage cap | plateau on a cloud volume or on the sum of them | no latency rise or IO pressure |
| H3 | CPU saturation | `cpu_busy` or `runq` | steal |
| H4 | CPU steal or burst credit exhaustion | `cpu_steal` | |
| H5 | Single-threaded bottleneck | `cpu_single` | `cpu_busy` |
| H6 | Memory pressure | `swap_out`, `direct_reclaim` or memory PSI | |
| H7 | Writeback stall | write latency, with dirty pages or full IO stall | read-dominant IO |
| H8 | Load high from blocked tasks, not CPU | `load` | `cpu_busy` |
| H9 | Softirq or interrupt concentration | `cpu_soft_single` | |
| H10 | Stacking-layer overhead | stacked-device latency over its members | |
| H11 | Storage errors or timeouts | kernel log only (below) | |
| H12 | Cgroup memory limit reached | kernel log only (below) | |

### Kernel log findings

Kernel log events are not scored, so they are never ranked against measured hypotheses. A hypothesis raised by events alone has no primary episode, `origin` `kernel` in `findings.json`, and is listed in its own "Kernel log findings" block after the ranked ones. That block does not count toward the 5.

| Events | Hypothesis | Subject |
|--------|------------|---------|
| IO error, NVMe/SCSI timeout or reset, filesystem error, md RAID or multipath failure | H11, one per disk | The disk. A partition counts as its disk, an NVMe controller (`nvme0`) as its namespaces. `unattributed device` when the line names none (confidence medium, otherwise high) |
| Memory cgroup OOM kill | H12 | The killed process name, else `cgroup` |
| Global OOM kill | H6 | host |
| Hung task | H8 | host |

When a measured hypothesis covers the event's time, the event is added to its evidence instead and confidence is high: an IO error inside a latency episode makes H1 high confidence, and H11 is still listed. A cgroup OOM kill inside a measured H6 is noted on it as a supporting fact.

Messages about `sr*`, `loop*` and `fd*` devices (virtual CD-ROM, loop mounts, floppy) raise nothing; a host note counts them. The kernel prints several lines per OOM kill; lines with the same PID, or identical text within 5 seconds, are one event with a count.

Each hypothesis carries fixed next-step text (CloudWatch and Azure Monitor metric names for H2 and H4, `iostat`, `pidstat`, `/proc/mdstat`, `vm.dirty_*` settings and so on). PostgreSQL-specific lines appear only when a PostgreSQL run directory is in the bundle.

### Process attribution

For each sustained episode, `pidstat` rows inside the window are summed per program and the top 5 are listed. The program is the executable name, so the 200 backends of a PostgreSQL server that retitle themselves (`postgres: app db 10.0.0.5(51234) idle`) are one entry, `postgres`. For interpreters it is the interpreter and the script or class (`python3 worker.py`, `java com.acme.Main`). With more than one process the line gives the count and the largest member: "postgres 82 MiB/s writes (61 % of device writes; 212 processes, largest postgres: checkpointer 60 MiB/s)". Thread rows (`pidstat -t`) repeat their process's totals and are not counted; H5 uses them to name the busy thread. The list is by: by IO then `iodelay` for disk, `%CPU` for CPU (at least 5 % of one CPU), RSS growth then major faults for memory. The share of device writes uses the device's own throughput as the denominator when only one physical device is active; otherwise the line says "of total process IO". Without a `pidstat` log the report says "No process data for this window".

## Report

`report.html` sections: header, summary (up to 5 measured hypotheses, then the kernel log findings), indicator timeline, findings with evidence tables and charts, transient events, time above threshold for every indicator, host and storage layout, data coverage and limits, analysis settings.

- Charts are inline SVG with selectable text. Colours follow the system light or dark setting; the charts always sit on a light surface. A print stylesheet makes "Print to PDF" usable.
- `report.md` plus `charts/*.png` suit tickets and wikis.
- Times are shown in the host's zone, taken from its manifest, and every chart axis states the zone. `--tz` overrides it. When the manifest has an IANA name (`tz_name=Europe/London`, written by `collect-os-metrics.sh` and `psar.sh`) the zone follows daylight-saving changes: times show `BST` or `GMT` as they were, and a note names the day of the change. Without a name, or when this machine has no time zone database for it (`pip install tzdata`; Windows and minimal containers), the fixed offset at collection start is used.
- Several runs from one host are joined. A gap longer than twice the interval is drawn as a break and no episode spans it.

### findings.json

`schema` 1. Epoch seconds throughout. The schema version increments on a breaking change.

| Key | Content |
|-----|---------|
| `host`, `window` | Host name, start and end epoch, `interval_s`, `timezone` |
| `settings` | Effective `sustain_s`, `gap_samples`, `interval_limited`, and every non-default setting with its source |
| `episodes` | Every sustained episode, every impactful transient and every episode a hypothesis refers to: `id`, `rule`, `device`, `kind`, `severity`, `start`, `end`, `duration_s`, `samples`, `exceeding_samples`, `mean`, `p95`, `peak`, `score`, `threshold`, and `role` when it is `supporting` |
| `transients` | `absorbed` counts per indicator, `impactful` episode ids, repeat summaries |
| `hypotheses` | `id`, `subject`, `confidence`, `origin` (`measured` or `kernel`), `primary`, `supporting`, `contradicting`, `related` (`[{device, episode}]`, stacked devices that show the same problem), `kernel_events`, `top_processes` (with `processes`, `example`, `example_value` for a group), `next_steps` |
| `time_above` | Per `indicator:device`: `warn_pct`, `crit_pct`, `p50`, `p95` and `max` (for rules that fire when low: `p5` and `min`) |
| `notes` | The `NOTE:` lines |

## Configuration

INI, read with `configparser`. Precedence, lowest first: built-in defaults, `~/.config/perfdiag/perfdiag.ini`, `--config FILE`, then `--set` and the dedicated flags. A section name and an option name are joined with a dot, so `[cpu]` `runq.warn_per_cpu` is the setting `cpu.runq.warn_per_cpu`. An unknown setting is an error with a suggestion.

```ini
[analysis]
sustain_s = 300
gap_samples = auto
disk_min_tps = 5

[disk.latency]
nvme.warn_ms = 2
nvme.crit_ms = 10
cloud-ebs.warn_ms = 8

[device.nvme1n1]
class = cloud-ebs
iops_limit = 3000
throughput_mibps = 125

[device.sdb]
class = hw-raid
spindles = 8

[cpu]
busy.warn_idle_pct = 20
runq.warn_per_cpu = 1.0
```

`analysis.exceedance_cap` (default 3) and `analysis.recurring_min_pct` (default 1) are described above. `gap_samples = auto` means `max(1, min(5, round(0.2 × sustain_s / interval_s)))`. The report's "Analysis settings" section lists every value that differs from the default and where it came from. The full list of keys is `DEFAULTS` in `perfdiag/thresholds.py`.

## Bundles are untrusted input

A bundle comes from a customer server. Before anything is extracted, every member is checked: absolute paths, `..` components, links, devices and FIFOs are refused, and the unpacked size is capped. Everything from the bundle that is shown in the HTML report is escaped.

A truncated or corrupt archive stops with `ERROR: archive … is damaged or truncated` and exit 1; copy it again from the server. `gather_manifest.txt` records a SHA-256 for every file, and perfdiag checks them on opening. A mismatch or missing file is a `NOTE:` on stderr and in the report's data coverage section, naming up to 10 files, and the analysis still runs. `inspect` prints `checksums: 214 verified, 0 mismatched, 0 missing`, or `none recorded` for a run directory or an older bundle. `--no-verify` skips the check.

## Performance

Measured on a 4 vCPU VM: 7 days at a 10 s interval with 16 CPUs and 3 disks took about 100 s and 1.5 GB of RAM for `analyse`. `report` adds chart rendering. Use `--from` and `--to` to analyse a shorter window.

## Limits

- Samples are interval averages. At a 10-minute interval a 30-second stall is invisible, and the report says so.
- The plateau test compares latency with the device's median over the whole run, so a plateau that covers most of the run is not detected.
- The text names indicators, not causes. A hypothesis is a place to look.
- Network rules and database-metric correlation are not implemented.
