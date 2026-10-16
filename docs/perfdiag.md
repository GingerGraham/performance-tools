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

`BUNDLE` is a `.tar.gz`, a `.zip`, a gather directory or a single run directory. All subcommands also accept `--keep-extracted`, which keeps the temporary extraction directory and prints its path.

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
| `--device-limit DEV:iops=N,mbps=N` | Declared device limits. Enables limit-based throughput and IOPS rules. `mbps` is MiB/s |
| `--spindles DEV=N` | Parallelism of a hardware RAID device |
| `--class DEV=CLASS` | Override the detected media class of a device |
| `--sustain DURATION` | Sustain window, e.g. `10m` |

Exit codes: 0 success, 1 runtime error (bad bundle, missing matplotlib for `plot` and `report`), 2 usage or setting error.

## How the analysis works

### Duration before magnitude

A bottleneck is a resource that stays over its limit long enough to queue work. For every indicator, an **episode** is a run of samples past the warning threshold. Up to `gap_samples` samples that dip back under are allowed inside one run. A gap in the data longer than twice the sample interval always ends a run.

- **Sustained**: at least `sustain_s` long (default 300 s) and at least 3 samples.
- **Transient**: anything shorter.
- **Severity** is `crit` when the **median** of the run is past the critical threshold, so one extreme sample cannot make a run critical.
- **Score** is the sum over the run of `(value - warn) / warn` times the interval: how far over, for how long. A 5-second burst to 2× the threshold scores 5. Forty-five seconds at 1.5× scores 22.5. Findings are ranked by score, not by peak.

If `sustain_s` is less than 3 sample intervals, the interval is too coarse to tell sustained from transient. The window is raised to 3 intervals, a `NOTE:` is printed and every finding is labelled interval-limited.

### Transients: absorbed or impactful

A transient is checked against its paired impact indicators for the same samples (plus or minus one interval).

| Transient on | Paired impact indicators |
|--------------|--------------------------|
| Disk throughput or IOPS | Same device: latency, and queue depth for HDD-class devices |
| CPU busy, run queue | CPU pressure (PSI), load per CPU |
| Available memory low | Swap-out, direct reclaim, major faults, memory pressure |
| Latency, queue, PSI, swap-out, steal | Themselves: always listed |

A transient with no paired breach is **absorbed**: it is counted ("12 throughput bursts, all absorbed") and not listed. Repeated impactful transients are summarised ("w_await exceeded 20 ms in 37 separate bursts of up to 2 min between 09:00 and 17:00") and can raise a hypothesis.

### Disk rules depend on the media class

The class comes from the hardware inventory and can be overridden. It sets the latency thresholds:

| Class | Warn ms | Crit ms |
|-------|---------|---------|
| `nvme` | 2 | 10 |
| `ssd` | 5 | 20 |
| `hdd`, `hw-raid` | 20 | 50 |
| `virtual`, `cloud-ebs`, `cloud-azure`, `unknown` | 10 | 30 |
| `stacked` (LVM, LUKS, md) | the lowest warn and crit among its member disks |

- Samples with fewer than `analysis.disk_min_tps` IO/s (default 5) are ignored for latency.
- Queue depth counts only for `hdd` and `hw-raid`: warn above 2 × parallelism with latency at its warn value, crit at 4 × parallelism with latency at its crit value. `aqu-sz` on NVMe and cloud volumes is normal under concurrency.
- `%util` counts only for `hdd` (warn 80, crit 95). On other media it is not a saturation signal.
- Throughput and IOPS have no threshold unless a limit is declared (`--device-limit`, warn at 80 % and crit at 95 % of it) or an absolute threshold is set (`disk.read_kBps.warn`, `disk.write_kBps.warn`, `disk.throughput_kBps.warn`, `disk.iops.warn`, each with a `.crit`).
- **Plateau detection** looks for a limit nobody told you about. A sustained window counts when throughput (or IOPS) is flat (coefficient of variation below `analysis.plateau_cv`, default 0.10), the window mean is at least 90 % of the run's p99, and latency in the window is at least twice the device's median latency and at least the class warn value. A steady workload is flat too, so the latency condition is what separates a cap from a busy disk. For cloud volumes the same test runs on the sum across all of them (`cloud-sum`).
- A stacked device whose latency exceeds its slowest member's by more than 2× and 5 ms raises a stacking-layer indicator.

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

Kernel log events can raise a hypothesis to high confidence on their own: an OOM kill raises H6, an IO error or NVMe/SCSI timeout on a device raises H1 for that device, a hung task raises H8. Such a hypothesis has no primary episode and is ranked first.

Each hypothesis carries fixed next-step text (CloudWatch and Azure Monitor metric names for H2 and H4, `iostat`, `pidstat`, `/proc/mdstat`, `vm.dirty_*` settings and so on). PostgreSQL-specific lines appear only when a PostgreSQL run directory is in the bundle.

### Process attribution

For each sustained episode, `pidstat` rows inside the window are summed per command (the first 60 characters of the redacted command line) and the top 5 are listed: by IO then `iodelay` for disk, `%CPU` for CPU (at least 5 % of one CPU), RSS growth then major faults for memory. The share of device writes uses the device's own throughput as the denominator when only one physical device is active; otherwise the line says "of total process IO". Without a `pidstat` log the report says "No process data for this window".

## Report

`report.html` sections: header, summary (up to 5 hypotheses), indicator timeline, findings with evidence tables and charts, transient events, time above threshold for every indicator, host and storage layout, data coverage and limits, analysis settings.

- Charts are inline SVG with selectable text. Colours follow the system light or dark setting; the charts always sit on a light surface. A print stylesheet makes "Print to PDF" usable.
- `report.md` plus `charts/*.png` suit tickets and wikis.
- Times are shown in the host's zone, taken from its manifest, and every chart axis states the zone. `--tz` overrides it. A daylight-saving change inside the window is not followed: the offset at collection start is used.
- Several runs from one host are joined. A gap longer than twice the interval is drawn as a break and no episode spans it.

### findings.json

`schema` 1. Epoch seconds throughout. The schema version increments on a breaking change.

| Key | Content |
|-----|---------|
| `host`, `window` | Host name, start and end epoch, `interval_s`, `timezone` |
| `settings` | Effective `sustain_s`, `gap_samples`, `interval_limited`, and every non-default setting with its source |
| `episodes` | Every sustained episode, every impactful transient and every episode a hypothesis refers to: `id`, `rule`, `device`, `kind`, `severity`, `start`, `end`, `duration_s`, `samples`, `exceeding_samples`, `mean`, `p95`, `peak`, `score`, `threshold` |
| `transients` | `absorbed` counts per indicator, `impactful` episode ids, repeat summaries |
| `hypotheses` | `id`, `subject`, `confidence`, `primary`, `supporting`, `contradicting`, `kernel_events`, `top_processes`, `next_steps` |
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
throughput_mbps = 125

[device.sdb]
class = hw-raid
spindles = 8

[cpu]
busy.warn_idle_pct = 20
runq.warn_per_cpu = 1.0
```

`gap_samples = auto` means `max(1, min(5, round(0.2 × sustain_s / interval_s)))`. The report's "Analysis settings" section lists every value that differs from the default and where it came from. The full list of keys is `DEFAULTS` in `perfdiag/thresholds.py`.

## Bundles are untrusted input

A bundle comes from a customer server. Before anything is extracted, every member is checked: absolute paths, `..` components, links, devices and FIFOs are refused, and the unpacked size is capped. Everything from the bundle that is shown in the HTML report is escaped.

## Limits

- Samples are interval averages. At a 10-minute interval a 30-second stall is invisible, and the report says so.
- The plateau test compares latency with the device's median over the whole run, so a plateau that covers most of the run is not detected.
- The text names indicators, not causes. A hypothesis is a place to look.
- Network rules and database-metric correlation are not implemented.
