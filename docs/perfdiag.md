# perfdiag

Analyses a bundle from [gather-perf-diag.sh](gather-perf-diag.md) on a workstation. It ranks problems by how far over a threshold and for how long, groups them into hypotheses with next steps, and writes charts and a single-file HTML report. When the bundle holds database collector runs or database processes, it also analyses the databases and ties their waits and latency to the host findings (see [Databases](#databases)).

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
| `inspect` | Hosts, runs, time range, interval, activities present, database instances, PSI, inventory summary and the `NOTE:` lines from the manifests |
| `analyse` | Ranked hypotheses and episodes as text. `--json FILE` writes `findings.json` (for several hosts the host name is added to the file name) |
| `plot` | One chart per indicator group, plus the indicator timeline and, for each database instance, the charts its data supports |
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
| D1 | Database reads waiting on storage | database read latency | read rate fell during the window |
| D2 | Commit latency (log or WAL writes) | database commit latency, or commit waits when the engine has no latency source | |
| D3 | Lock contention | sessions waiting on locks | |
| D4 | Internal latch or buffer contention | sessions waiting on latches | |
| D5 | Database CPU demand above capacity | database CPU demand per CPU | host CPU idle above 40 % |
| D6 | Dirty-buffer flushing falling behind | buffer flush stalls | |
| D7 | Query memory grants queued | memory grants pending | |
| D8 | Connection or worker exhaustion | connections in use, or worker queue | |
| D9 | Replication lag | replication lag | |

The D codes exist only when a database collector run is in the bundle. Each one is about one database instance, and the subject is the instance (`PostgreSQL 16.4 (localhost:5432)`). They are ranked together with the H codes by the score of the primary episode, so a lock problem on an otherwise idle host ranks first. Details are under [Database hypotheses](#database-hypotheses).

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

Each hypothesis carries fixed next-step text (CloudWatch and Azure Monitor metric names for H2 and H4, `iostat`, `pidstat`, `/proc/mdstat`, `vm.dirty_*` settings and so on). The only engine line on an H code is the PostgreSQL checkpoint line on H1 and H7, which appears when a PostgreSQL run directory is in the bundle. A database seen only as processes adds a collector hint (see [Databases](#databases)).

### Process attribution

For each sustained episode, `pidstat` rows inside the window are summed per program and the top 5 are listed. The program is the executable name, so the 200 backends of a PostgreSQL server that retitle themselves (`postgres: app db 10.0.0.5(51234) idle`) are one entry, `postgres`. For interpreters it is the interpreter and the script or class (`python3 worker.py`, `java com.acme.Main`). With more than one process the line gives the count and the largest member: "postgres 82 MiB/s writes (61 % of device writes; 212 processes, largest postgres: checkpointer 60 MiB/s)". Thread rows (`pidstat -t`) repeat their process's totals and are not counted; H5 uses them to name the busy thread. The list is by: by IO then `iodelay` for disk, `%CPU` for CPU (at least 5 % of one CPU), RSS growth then major faults for memory. The share of device writes uses the device's own throughput as the denominator when only one physical device is active; otherwise the line says "of total process IO". Without a `pidstat` log the report says "No process data for this window".

## Databases

perfdiag reads the database collector runs in a bundle (`collect-postgresql-metrics.sh`, `collect-mysql-metrics.sh`, `collect-oracle-metrics.sh`, `collect-mssql-metrics.sh`), finds database servers that have no collector run, and ties both to the host findings. A database is analysed with the same method as the host: duration before magnitude, one hypothesis per problem, indicators with thresholds.

When the bundle has no database run, no `dbproc.*` key and no database process in `pidstat`, nothing changes. There is no Databases section, chart, note, coverage line or `Database:` line, and `databases` in `findings.json` is an empty list.

### Where a database comes from

| Source | Gives | Instance `source` |
|--------|-------|-------------------|
| Database collector run in the bundle | Database metrics, settings, file paths, locality | `collector` |
| `dbproc.*` keys in `hardware.txt` (from `collect-hw-inventory.sh`) | Engine, data location, Oracle SID | merged into a collector instance, or `process` |
| `pidstat` rows that match a database server process | Engine, CPU, IO and RSS of its processes, role breakdown | merged into a collector instance, or `process` |

Instance rules:

1. A collector run always creates an instance. Runs of one target (same engine and `db_instance`) are one instance.
2. A process match with no collector instance of that engine creates a process-only instance.
3. A process match with a collector instance of the same engine and `target_local=yes` is attached to it. MySQL and MariaDB count as one family.
4. With several local collector instances of one engine (two PostgreSQL clusters), the process is matched by data directory. When that is not possible it is attached to all of them and the instance notes `process data could not be split between the PostgreSQL instances on this host`.
5. A process is never attached to a collector instance that is remote or of unknown locality. It becomes its own process-only instance.

Process signatures are matched on the basename of the first command-line argument:

| Engine | Matches |
|--------|---------|
| PostgreSQL | `postgres`, `postmaster`, and retitled backends (`postgres: app db …`) |
| MySQL, MariaDB | `mysqld`, `mariadbd` (not `mysqld_safe`, not `mysqld_exporter`) |
| Oracle | `ora_<name>_<SID>` background processes and `oracle<SID>` foreground processes. ASM instances (`asm_pmon_+ASM`) are not databases and are left out |
| SQL Server | `sqlservr` |

`pidstat` lists only tasks with activity, so an idle database can be missed there. A `dbproc.*` entry in the inventory does not depend on activity.

Names used in tables, charts and as the indicator device ("short name"):

| Engine | Short name | Example |
|--------|------------|---------|
| PostgreSQL | `pg:<port>` | `pg:5432` |
| MySQL, MariaDB | `my:<port>` | `my:3306` |
| Oracle | `ora:<instance>/<container>` | `ora:ORCL/PDB1` |
| SQL Server | `mssql:<instance>` | `mssql:MSSQLSERVER` |
| Process only | `pg:proc`, `my:proc`, `mssql:proc`, `ora:<SID>` | `pg:proc` |

When two instances would get the same name (the same port on two servers), the later one uses its full `db_instance` text.

### Locality

The collector records whether the database server runs on the host it was started on (`target_local` in its manifest) and how it decided (`socket`, `loopback`, `server hostname`, `local address`). The report shows the basis beside the location. A 2.0.0 run has no such key: a loopback target counts as local and anything else as unknown.

| Locality | OS data in the bundle is | Effect |
|----------|--------------------------|--------|
| local | the database server's own host | Full correlation: file paths are placed on devices, D hypotheses use host evidence on those devices, CPU and memory, and H hypotheses can link to the instance |
| remote | the collecting machine, not the database host | D hypotheses are built from database data only and carry the note `the database server is remote (<instance>); the OS data in this bundle is from <host>, the collecting machine, and is not used as evidence`. File paths are not placed. The instance never links to an H hypothesis. Latency thresholds come from the `db.*` settings, not from a device class |
| unknown | not known | Treated like remote for evidence, with a note that it is not known whether the server runs on the host. File paths are still placed on devices, but only to show them |

A remote or unknown instance still gets its indicators, hypotheses, charts and Databases section. Per-CPU metrics (`db_cpu_demand`, `db_concurrency`) use the host's CPU count only for a local instance. Otherwise they use the engine's own count (Oracle `cpu_count`, SQL Server `scheduler_count`), and PostgreSQL and MySQL have none.

### Process-only instances

An instance seen only in `pidstat` or in the inventory has no database metrics, so it has no indicators and raises no D hypothesis. It still appears:

- as a `Database:` line in the text output and a facts block in the report (processes, share of `pidstat` samples it appears in, data location, devices);
- as database context on an H hypothesis it took part in, with its processes broken down by role (below);
- with a coverage line `mysqld: process data only. Database-level findings need collect-mysql-metrics.sh.`;
- with the collector hint added to the H hypothesis next steps: `Run linux/db/collect-mysql-metrics.sh alongside collect-os-metrics.sh during the next occurrence for database-level evidence.`

The database collectors need a database login and refuse to run under `sudo`; the collector guides explain how to start them.

### Database-only bundles

A host with database runs and no `os` or `psar` run is analysed. The window comes from the database series, and a note says `no os or psar run in the bundle: only database collector data is available for this host`. There are no OS indicators, so only database indicators and D hypotheses exist, and no host evidence is attached to them.

When OS data is present, the window comes from it and database series are clipped to it (`--from` and `--to` apply too). A database run that covers less than half of that window gets a note such as `postgresql run covers 35 min of the 2 h window`.

### Path to device mapping

The collector writes the locations of the database files to `db_paths.txt` by role: `data`, `wal`, `redo`, `log`, `temp`, `tablespace`, `asm_disk`. perfdiag maps each path to the devices in the disk data:

1. The path is normalised. Nothing is read from the file system, because the path belongs to the database server.
2. The longest mount point in the hardware inventory that is the path or a parent of it picks the file system, and so the device.
3. From that device, perfdiag keeps the device itself, its whole disk (for a partition) and everything below it (LVM, LUKS, md) that has disk data.
4. A raw device path (`/dev/sdb`, `/dev/mapper/x`) resolves by device name.

Not resolvable: `/dev/oracleasm/disks/*` and `AFD:` labels (a note says so), and ASM disk groups (`+DATA`; their disks are listed under `asm_disk`). Without a hardware inventory nothing is placed and the instance notes `no hardware inventory: PostgreSQL files could not be placed on devices`. When none of a role's paths matches a disk, the instance notes up to three of them.

The result is shown as `data on nvme1n1, WAL on nvme1n1` in the header line and as a device chain with media class in the report. For a local instance the media class of the data device sets the read-latency thresholds, and the class of the log device sets the commit-latency thresholds (see [Database indicators](#database-indicators)).

### Process roles

The processes of a database are grouped by role, so the report can say which part of it did the work, with or without a collector. The ordinary top-process list (by program) is unchanged.

| Engine | Command | Role |
|--------|---------|------|
| PostgreSQL | `postgres: checkpointer` | checkpointer |
| | `postgres: background writer` | background writer |
| | `postgres: walwriter` | WAL writer |
| | `postgres: autovacuum worker`, `autovacuum launcher` | autovacuum |
| | `postgres: walsender …`, `walreceiver`, `startup` | replication |
| | `postgres: archiver`, `logger`, `io worker`, `bgworker …` and similar server-internal processes | background |
| | other `postgres: …` | client backend |
| | `postgres -D …`, `postmaster` | postmaster |
| Oracle | `ora_dbw*_<SID>`, `ora_bw*_<SID>` | DB writer |
| | `ora_lgwr_<SID>`, `ora_lg0*_<SID>` | log writer |
| | `ora_ckpt_<SID>` | checkpoint |
| | `ora_arc*_<SID>` | archiver |
| | other `ora_*_<SID>` | background |
| | `oracle<SID> …` | foreground |
| MySQL, MariaDB, SQL Server | the server process | server |

PostgreSQL titles vary by version; the cluster prefix of Debian-style names (`postgres: 16/main: checkpointer`) and a trailing `process` are ignored.

### Basis: time-averaged or sampled

Every database metric is either **time-averaged** or **sampled**.

- Time-averaged: a rate or ratio from the change of a cumulative counter over the sample interval (statements per second, read latency, cache hit ratio, wait time). It covers the whole interval.
- Sampled: a value read at the sample instant (active sessions, connections in use, queue lengths, replication lag, PostgreSQL wait events). Anything that happens between two samples is not seen.

Sampled: `db.sessions_active`, `db.connections_pct`, the PostgreSQL `db.wait.*` categories, `db.cpu_demand_per_cpu` (PostgreSQL, SQL Server), `db.concurrency_per_cpu`, `db.dirty_pct`, `db.checkpoint_age_pct`, `db.memory_grants_pending`, `db.worker_queue`, `db.xact_age_s`, `db.history_list`, `db.replication_lag_s`, and the SQL Server cache hit ratio. Everything else is time-averaged.

An episode on a sampled metric carries the note `sampled every N s, not averaged`. The report's activity table has a Basis column, and the title of the waits chart states the basis.

Waits are shown as sessions waiting, in 13 categories: `cpu`, `io_read`, `io_write`, `commit`, `lock`, `latch`, `memory`, `workers`, `parallel`, `network`, `replica`, `temp`, `other`. For counters of wait time, sessions waiting is the wait time per second of elapsed time, which is the average number of sessions waiting. Each engine fills the categories its data supports; the event names behind a category are kept as detail (the latch names in D4, for example).

### Resets and gaps

A counter that goes backwards, or a changed server start or statistics reset marker, means a restart or a statistics reset. Every rate-based series is blank at that sample, the time is recorded as a reset, and no episode spans it. A gap longer than twice the collector interval is blank in the same way, so a rate never covers a gap.

A ratio is blank when its denominator is too small, so a quiet interval does not read as 0 % or 100 %. The floors are `db.min_reads_ps`, `db.min_commits_ps` and `db.cache_hit.min_reads_ps`.

Resets are listed in the report facts, in `findings.json` and in a coverage line, and are drawn as dotted lines on the database charts.

### Database indicators

Each indicator belongs to one instance. In tables the instance short name is in the device column (`pg:5432`). An indicator exists only when the instance's data has the metric, so the set differs by engine.

| Indicator | Fires when | Role | Settings | Engines |
|-----------|-----------|------|----------|---------|
| `db_read_latency` | read latency above | primary | device class, else `db.read_latency.*` | all |
| `db_commit_latency` | commit latency above | primary | device class, else `db.commit_latency.*` | MySQL, MariaDB, Oracle, SQL Server |
| `db_commit_wait` | sessions waiting on commit above | primary | `db.commit_wait.*` | PostgreSQL, which has no commit latency source. Built only when `db_commit_latency` has no data |
| `db_lock_wait` | sessions waiting on locks above | primary | `db.lock_wait.*` | all |
| `db_latch_wait` | sessions waiting on latches above | primary | `db.latch_wait.*` | PostgreSQL, Oracle, SQL Server |
| `db_cpu_demand` | database CPU demand per CPU above | primary | `db.cpu_demand.*` | PostgreSQL, Oracle, SQL Server |
| `db_flush_stall` | buffer flush stalls per second above | primary | `db.flush_stall.*` | all |
| `db_memory_grants` | memory grants pending, or sessions waiting for memory, above (the higher of the two) | primary | `db.memory_grants.*` | SQL Server |
| `db_connections` | connections in use as % of the limit above | primary | `db.connections.*` | all |
| `db_worker_queue` | worker queue, or tasks waiting for a worker, above (the higher of the two) | primary | `db.worker_queue.*` | SQL Server |
| `db_repl_lag` | replication lag above | primary | `db.replication_lag.*` | PostgreSQL, MySQL, MariaDB |
| `db_io_wait` | sessions waiting on read plus write IO above | supporting | `db.io_wait.*` | all |
| `db_concurrency` | running sessions per CPU above | supporting | `db.cpu_demand.*` | MySQL, MariaDB |
| `db_cache_hit` | buffer cache hit ratio **below** | supporting | `db.cache_hit.*` | all |
| `db_temp` | temporary file writes above | supporting | `db.temp.*` | PostgreSQL, SQL Server |
| `db_xact_age` | oldest transaction age above | supporting | `db.xact_age.*` | PostgreSQL |
| `db_history_list` | InnoDB history list length above | supporting | `db.history_list.*` | MySQL, MariaDB |
| `db_compile` | compilations as % of statements above | supporting | `db.compile.*` | Oracle (hard parses), SQL Server |
| `db_dirty` | dirty pages or checkpoint age above (the higher of the two) | supporting | `db.dirty.*` | MySQL, MariaDB |

Read and commit latency are judged by the media class of the mapped device, as for the host disks: 8 ms is bad on NVMe and normal on a hard disk. That applies to a local instance whose data (or log) device is mapped. For anything else, or when you set any `db.read_latency.*` or `db.commit_latency.*` key, the `db.*` settings decide.

Load indicators (`db_io_wait`, `db_temp`, `db_concurrency`, `db_cache_hit`) are supporting only, as on the host: high load with normal latency and waits is a busy database, not a bottleneck. The primary indicators are latency, wait or exhaustion measures themselves, so a transient on one is always listed. A transient on `db_cache_hit` is checked against read latency and device read await, and one on `db_temp` against device write await, on the instance's own devices.

### Database hypotheses

Each D hypothesis is built from one instance's indicators. Supporting and contradicting evidence follows the same overlap and confidence rules as the H codes. Evidence from the operating system is attached only for a local instance.

| Code | Supporting | Facts | Against |
|------|------------|-------|---------|
| D1 | `db_io_wait`; `db_cache_hit` below its threshold; read await on the data devices; `mem_avail` (less page cache means more physical reads) | | Read rate in the last third of the window under 70 % of the first third (fewer reads and slower points at contention, not volume) |
| D2 | Write await on the log devices | Synchronous replica waits; Oracle `log buffer space` waits | |
| D3 | `db_xact_age`; `db_history_list` | Deadlocks in the window; `host CPU idle averaged 94 % and no disk latency episode` | |
| D4 | `db_cpu_demand` | CPU demand near its warn threshold (70 % or more) when no episode; latch names with their sessions (PostgreSQL `LWLock` names, Oracle and SQL Server latch and buffer waits); SQL Server `PAGELATCH` waits while tempdb is written | |
| D5 | `db_compile`; `cpu_busy`, `runq`, `psi_cpu` | Oracle `resmgr:cpu quantum` waits: `Resource Manager limited CPU; the plan, not the host, set the limit` | Host CPU idle above 40 % while demand was high (points at Resource Manager, a cgroup or CPU affinity) |
| D6 | `db_dirty`; write await on the data devices | Backends wrote over 50 % of the flushed buffers themselves (PostgreSQL); `N of M checkpoints were requested (WAL volume reached max_wal_size), not timed` | |
| D7 | `db_temp`; `mem_avail`, `psi_mem_some`, `psi_mem_full` | | |
| D8 | `db_lock_wait` | Sessions waiting on locks overlapped: blocked sessions hold their connections | |
| D9 | `db_io_wait`; latency on the data devices of a local standby | | |

Notes:

- D1 gets an extra note when the mapped data device is measured and has no latency episode in the window: `the device's own latency stayed under its warn threshold while the database waited; look above the block layer: filesystem locks, cgroup io.max throttling, a network filesystem, or memory pressure evicting the page cache`. The note moves the next steps, not the confidence.
- The requested-checkpoint fact needs at least two checkpoints in the window and at least `db.checkpoint_req.warn_pct` of them requested. PostgreSQL checkpoints are too sparse for episode detection, so this is a fact, not an indicator.
- With SQL Server file statistics, D1, D2 and D6 name the devices whose latency passed the warn threshold, not every device the files are on.
- When an instance is local but its files could not be placed on a device, disk episodes are listed as facts worded `on <device> (not confirmed as this database's device)`.
- D7 can occur only for SQL Server, and D9 only for PostgreSQL, MySQL and MariaDB, because no other engine records the data.

Next steps are fixed text per code and engine (`pg_stat_statements` ordering, blocker queries, log device and `wal_sync_method`, `innodb_io_capacity`, `v$sqlstats`, `sys.dm_exec_requests` and so on). A step that cites a setting adds `(now X)` only when the run recorded that setting. MariaDB uses the MySQL steps. D8 has one text for all engines.

### Links between database and host hypotheses

An H hypothesis in H1, H2, H3, H5, H6, H7, H8 or H12 that has a measured primary episode is linked to a **local** instance when:

- a process of the instance is in the hypothesis's top-process list, or
- the hypothesis is a disk code (H1, H2, H7) and its device is one of the instance's devices, or sits directly above or below one.

A linked H hypothesis gets a database line under its process line: what the database was doing during the primary window.

```
Database pg:5432, PostgreSQL 16.4 (localhost:5432): 410 transactions/s; waits io_write 6.10, commit 1.20 sessions; 71 % of the device's writes; checkpointer 60.0 MiB/s writes, client backends 10.0 MiB/s writes.
```

It lists, as far as the data has them: statements or transactions per second, the three largest wait categories, read and commit latency, temporary file write rate, the database's share of the device's reads and writes (local instance, when both sides are known), and the process roles by IO or CPU, depending on the code.

When a D hypothesis of that instance overlaps the H hypothesis in time (and, for disk codes, concerns the same disk), both sides gain a supporting fact and a link:

```
On H1: PostgreSQL (pg:5432) sessions waiting on IO averaged 6.1 in this window (D1)
        See also: D1 pg:5432.
On D1:  nvme1n1 (/var/lib/pgsql) read await averaged 48 ms (H1)
        See also: H1 nvme1n1.
```

The facts count as supporting for confidence and can only raise it. The links appear as `See also` lines in the reports and in `links` in `findings.json`. H4, H9, H10 and H11 do not link to databases.

### Reading database limits

- **Sampled waits.** PostgreSQL wait events are one snapshot per sample. A lock held for two seconds between two samples at 15 s is invisible, and the session counts are counts at that instant. Use them as a share of samples, not as time.
- **SQL Server tasks.** Wait time is per task. A parallel query has several tasks, so `parallel` and `io_read` can exceed the number of sessions. The waits chart title says so.
- **Pluggable databases.** Connected to an Oracle PDB, the `v$` views describe that container only. The coverage line says `connected to a PDB; instance-wide figures need a CDB$ROOT connection`. Treat per-container readings with care.
- **Oracle licensing.** The Oracle collector never reads ASH, AWR or any `DBA_HIST_*` view, which need the Diagnostics Pack. Waits come from cumulative `v$system_event` and `v$system_wait_class` totals, so they are averages per interval. There are no per-session waits and no SQL ids. On a system licensed for the Diagnostics Pack, ASH and AWR give that detail; this tool does not read them.
- **Settings that switch measurement off.** With `track_io_timing` off, PostgreSQL read latency is not measured. With `performance_schema` off, MySQL InnoDB file IO latency is not measured. Coverage lines say so.
- **Old collector runs.** A 2.0.0 run has PostgreSQL counters for the `postgres` database only and no waits, IO timing, settings or file placement. It loads, and a coverage line names what is missing and says to re-run with 3.0.0.
- **Containers.** SQL Server file paths inside a container are placed on host devices only when they match the mount table.
- **Remote servers.** The OS data is not evidence about a remote database. The database findings stand on database data alone.
- **Thresholds.** The `db.*` numbers are starting points like the host ones. Set them for your workload (see Configuration).

## Report

`report.html` sections: header, summary (up to 5 measured hypotheses, then the kernel log findings), indicator timeline, findings with evidence tables and charts, databases (only when the host has any), transient events, time above threshold for every indicator, host and storage layout, data coverage and limits, analysis settings.

- Charts are inline SVG with selectable text. Colours follow the system light or dark setting; the charts always sit on a light surface. A print stylesheet makes "Print to PDF" usable.
- `report.md` plus `charts/*.png` suit tickets and wikis.
- Times are shown in the host's zone, taken from its manifest, and every chart axis states the zone. `--tz` overrides it. When the manifest has an IANA name (`tz_name=Europe/London`, written by `collect-os-metrics.sh` and `psar.sh`) the zone follows daylight-saving changes: times show `BST` or `GMT` as they were, and a note names the day of the change. Without a name, or when this machine has no time zone database for it (`pip install tzdata`; Windows and minimal containers), the fixed offset at collection start is used.
- Several runs from one host are joined. A gap longer than twice the interval is drawn as a break and no episode spans it.

### Database output

All of this appears only when the host has at least one database instance.

**Text output.** One line per instance follows the `Window:` line:

```
Database: PostgreSQL 16.4 (pg:5432), collector, local, data on nvme1n1, WAL on nvme1n1
Database: mysqld (process data only), seen in 96 % of pidstat samples, data on sdb
```

A remote instance says `remote`, and one of unknown locality says `locality unknown`. Roles are `data`, `WAL`, `redo`, `log` and `ASM disks`, for those that were placed on a device. D hypotheses are ranked with the H hypotheses. The sustained-episode table shows database indicators with the instance short name in the device column, and its columns widen to fit.

**Findings.** D hypotheses are findings like any other. Under the process line, an H hypothesis with database context has a `Database …` line, and a hypothesis with links has a `See also: D1 pg:5432.` line. A D hypothesis of a remote instance carries the remote note. The charts under a D finding are its instance's charts (below). An H finding with database context also shows the linked instance's latency chart.

**Header.** A `Databases` fact lists the instance labels.

**Databases section.** It follows Findings and has one block per instance:

| Part | Content |
|------|---------|
| Facts | Engine and version, source (collector run or process data only), location and how it was decided, collector script and schema, samples and interval, window covered, server start, resets in the window, processes |
| File placement | Role, path, device chain and media class for each database file location, or why there is none (remote server, no recorded paths, no hardware inventory). A path that matched no disk reads `not placed` |
| Activity summary | Median and p95 of statements or transactions per second, active sessions, each wait category, read and commit latency and cache hit ratio, with the basis (time-averaged or sampled) |
| Recorded settings | The `setting.*` values the collector wrote to its manifest |
| Charts | The instance's charts that the findings above did not already show. A chart shown under Findings is referenced, not repeated |
| Coverage | The coverage lines and notes of the instance |

**Charts.** Five per instance, named `db_<kind>_<short>` (`db_waits_pg_5432`). A chart exists only when its data does, and an instance with no data for any of them adds none. Episodes of the related indicators are drawn as bands, and restarts or resets as dotted vertical lines.

| Chart | Content | Needs |
|-------|---------|-------|
| `db_activity_` | Statements or transactions per second, active sessions on a second axis | A rate series |
| `db_waits_` | Stacked area of sessions waiting by category, `cpu` at the bottom. For a local instance, the host CPU count as a reference line when waits come near it | Any wait series |
| `db_latency_` | Read and commit latency. For a local instance with mapped files, the data device's read await and the log device's write await on the same axis, and the thresholds | Read or commit latency |
| `db_cache_` | Cache hit ratio and physical reads per second | A cache hit series |
| `db_processes_` | CPU and IO of the instance's processes by role, from `pidstat` | Process keys and `pidstat` rows for them |

The title of a chart states its basis: `sessions waiting, by category (sampled every 15 s, not averaged)` or `(average over each interval)`. For SQL Server the waits title says that parallel tasks are counted individually.

**Coverage lines.** Added to the instance's block and to the report's data coverage section, only when they apply:

| Line | When |
|------|------|
| `PostgreSQL pg:5432: track_io_timing is off, so read latency was not measured.` | The setting is off in a 3.0.0 run |
| `PostgreSQL pg:5432: wait events are sampled every 15 s, not averaged; short waits between samples are not visible.` | The waits are sampled |
| `MySQL my:3306: performance_schema is off, so InnoDB file IO latency was not measured.` | The setting is off in a 3.0.0 run |
| `PostgreSQL pg:5432: collected with collect-postgresql-metrics.sh 2.0.0. … Re-run with 3.0.0.` | A 2.0.0 collector run; the text names what that version could not record |
| `Oracle ORCL/PDB1: connected to a PDB; instance-wide figures need a CDB$ROOT connection.` | `con_name` is not `CDB$ROOT` |
| `SQL Server MSSQLSERVER: file paths are inside the container and were mapped to host devices through the mount table only when the paths matched.` | The paths belong to a container |
| `mysqld: process data only. Database-level findings need collect-mysql-metrics.sh.` | A process-only instance |
| `PostgreSQL pg:5432: statistics reset or server restart at 14:02; rates are blank for that sample.` | The instance has resets. Dates are added when the window is longer than 20 hours |

The notes of an instance (unplaced files, short runs, rows skipped in a CSV file, split process data) follow as `NOTE:` lines.

**inspect.** After the `sadf` activities, each instance has a block:

```
  databases:
    PostgreSQL 16.4 (localhost:5432) [pg:5432]
      source: collector, schema 3, on this host (socket)
      runs: db01_postgresql_20261016_103000
      samples: 360, every 10 s
      files: pg_stats.csv: yes, pg_database.csv: yes, pg_waits.csv: yes
      processes: postgres
      devices: data nvme1n1; wal nvme1n1
```

A process-only instance shows `source: process data only` and has no runs, samples or files lines. Resets and notes follow when there are any. Nothing is printed when no database was found.

### findings.json

`schema` 2. Epoch seconds throughout. Schema 2 added `databases` and, on each hypothesis, `database_context` and `links`; nothing else changed, so a reader of schema 1 keeps working. The schema version increments on a breaking change.

| Key | Content |
|-----|---------|
| `host`, `window` | Host name, start and end epoch, `interval_s`, `timezone` |
| `settings` | Effective `sustain_s`, `gap_samples`, `interval_limited`, and every non-default setting with its source |
| `episodes` | Every sustained episode, every impactful transient and every episode a hypothesis refers to: `id`, `rule`, `device`, `kind`, `severity`, `start`, `end`, `duration_s`, `samples`, `exceeding_samples`, `mean`, `p95`, `peak`, `score`, `threshold`, and `role` when it is `supporting` |
| `transients` | `absorbed` counts per indicator, `impactful` episode ids, repeat summaries |
| `hypotheses` | `id`, `subject`, `confidence`, `origin` (`measured` or `kernel`), `primary`, `supporting`, `contradicting`, `related` (`[{device, episode}]`, stacked devices that show the same problem), `kernel_events`, `top_processes` (with `processes`, `example`, `example_value` for a group), `next_steps`, `database_context`, `links` |
| `time_above` | Per `indicator:device`: `warn_pct`, `crit_pct`, `p50`, `p95` and `max` (for rules that fire when low: `p5` and `min`) |
| `notes` | The `NOTE:` lines |
| `databases` | One entry per database instance; `[]` when none was found (see below) |

Schema 2 additions:

`databases` entry:

| Key | Content |
|-----|---------|
| `id`, `short` | Stable identity (`postgresql:localhost:5432`) and the short name used as the `device` of database episodes (`pg:5432`) |
| `engine`, `label` | `postgresql`, `mysql`, `mariadb`, `oracle` or `mssql`; the display label |
| `source` | `collector` or `process` |
| `local` | `true`, `false`, or `null` when unknown |
| `version_num` | The server's version number, or `null` |
| `collector_schema` | Schema of the collector run (`2` for a 2.0.0 run, `3` for 3.0.0, `0` for a process-only instance) |
| `devices` | Role to device names, for example `{"data": ["nvme1n1"], "wal": ["nvme1n1"]}` |
| `resets` | Epoch seconds of restarts and statistics resets |
| `settings` | The `setting.*` values from the manifest |
| `notes` | The instance's notes |

On a hypothesis:

| Key | Content |
|-----|---------|
| `database_context` | List of objects, one per linked instance (H hypotheses only; `[]` otherwise): `instance`, `label`, `engine`, `source`, `statements_ps`, `transactions_ps`, `waits` (`[{category, sessions}]`, the three largest), `read_latency_ms`, `commit_latency_ms`, `temp_kBps`, `write_share_pct`, `read_share_pct`, `roles` (`[{role, processes, cpu_pct, rd_kBps, wr_kBps}]`), `roles_by` (`io` or `cpu`). A value that was not measured is `null` |
| `links` | List of `{code, subject}` for the hypotheses of the other family that overlap this one: `{"code": "D1", "subject": "pg:5432"}` on an H hypothesis, `{"code": "H7", "subject": "nvme1n1"}` on a D hypothesis |

For a D hypothesis, `subject` is the instance short name and the episodes carry the same short name as `device`. Facts that link the two sides are in `supporting_facts`.

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

### Database settings

Keys of the `db.` group. They are overridden like any other setting, with `--set`, `--config` or `perfdiag.ini`. There are no dedicated flags. The defaults are starting points, not vendor figures. Pairs are warn and crit, in that order.

| Keys | Defaults | Meaning | Used by |
|------|----------|---------|---------|
| `db.min_reads_ps` | 10 | Smallest reads per second at which read latency is computed; below it the sample is blank | `db_read_latency` |
| `db.min_commits_ps` | 1 | Smallest commits per second at which commit latency is computed | `db_commit_latency` |
| `db.cache_hit.min_reads_ps` | 100 | Smallest logical reads per second at which the cache hit ratio is computed | `db_cache_hit` |
| `db.read_latency.warn_ms`, `db.read_latency.crit_ms` | 10, 30 | Read latency in ms. Used when the data device is not mapped or the instance is not local; setting either key overrides the device class | `db_read_latency` |
| `db.commit_latency.warn_ms`, `db.commit_latency.crit_ms` | 10, 30 | Commit latency in ms. Same rule, for the log, WAL or redo device | `db_commit_latency` |
| `db.commit_wait.warn_sessions`, `db.commit_wait.crit_sessions` | 1, 4 | Sessions waiting on commit | `db_commit_wait` |
| `db.lock_wait.warn_sessions`, `db.lock_wait.crit_sessions` | 1, 5 | Sessions waiting on locks | `db_lock_wait` |
| `db.latch_wait.warn_sessions`, `db.latch_wait.crit_sessions` | 1, 5 | Sessions waiting on latches | `db_latch_wait` |
| `db.io_wait.warn_sessions`, `db.io_wait.crit_sessions` | 1, 4 | Sessions waiting on read or write IO | `db_io_wait` |
| `db.cpu_demand.warn_per_cpu`, `db.cpu_demand.crit_per_cpu` | 0.9, 1.5 | Sessions wanting CPU per CPU. MySQL running threads per CPU use the same pair | `db_cpu_demand`, `db_concurrency` |
| `db.flush_stall.warn_ps`, `db.flush_stall.crit_ps` | 0.01, 1 | Buffer flush stalls per second | `db_flush_stall` |
| `db.memory_grants.warn`, `db.memory_grants.crit` | 1, 5 | Queries waiting for a memory grant | `db_memory_grants` |
| `db.connections.warn_pct`, `db.connections.crit_pct` | 80, 95 | Connections in use, % of the limit | `db_connections` |
| `db.worker_queue.warn`, `db.worker_queue.crit` | 1, 10 | Tasks queued for a worker | `db_worker_queue` |
| `db.replication_lag.warn_s`, `db.replication_lag.crit_s` | 30, 300 | Replication lag in seconds | `db_repl_lag` |
| `db.cache_hit.warn_pct`, `db.cache_hit.crit_pct` | 95, 90 | Buffer cache hit ratio in %. Fires below the value | `db_cache_hit` |
| `db.temp.warn_kBps`, `db.temp.crit_kBps` | 1024, 10240 | Temporary file writes in kB/s (1 kB = 1024 bytes) | `db_temp` |
| `db.xact_age.warn_s`, `db.xact_age.crit_s` | 600, 3600 | Age of the oldest open transaction in seconds | `db_xact_age` |
| `db.history_list.warn`, `db.history_list.crit` | 100000, 1000000 | InnoDB history list length | `db_history_list` |
| `db.compile.warn_pct`, `db.compile.crit_pct` | 10, 25 | Compilations or hard parses as % of statements | `db_compile` |
| `db.dirty.warn_pct`, `db.dirty.crit_pct` | 75, 90 | Dirty pages or checkpoint age, % of the maximum | `db_dirty` |
| `db.checkpoint_req.warn_pct` | 50 | Share of checkpoints that were requested, not timed, at which D6 adds the fact (needs at least two checkpoints in the window) | D6 fact, no indicator |

```bash
python3 perfdiag.py analyse BUNDLE --set db.lock_wait.warn_sessions=2 --set db.lock_wait.crit_sessions=8
```

```ini
[db.lock_wait]
warn_sessions = 2
crit_sessions = 8

[db]
min_reads_ps = 20
```

## Bundles are untrusted input

A bundle comes from a customer server. Before anything is extracted, every member is checked: absolute paths, `..` components, links, devices and FIFOs are refused, and the unpacked size is capped. Everything from the bundle that is shown in the HTML report is escaped.

A truncated or corrupt archive stops with `ERROR: archive … is damaged or truncated` and exit 1; copy it again from the server. `gather_manifest.txt` records a SHA-256 for every file, and perfdiag checks them on opening. A mismatch or missing file is a `NOTE:` on stderr and in the report's data coverage section, naming up to 10 files, and the analysis still runs. `inspect` prints `checksums: 214 verified, 0 mismatched, 0 missing`, or `none recorded` for a run directory or an older bundle. `--no-verify` skips the check.

## Performance

Measured on a 4 vCPU VM: 7 days at a 10 s interval with 16 CPUs and 3 disks took about 100 s and 1.5 GB of RAM for `analyse`. `report` adds chart rendering. Use `--from` and `--to` to analyse a shorter window.

## Limits

- Samples are interval averages. At a 10-minute interval a 30-second stall is invisible, and the report says so.
- The plateau test compares latency with the device's median over the whole run, so a plateau that covers most of the run is not detected.
- The text names indicators, not causes. A hypothesis is a place to look.
- Network rules are not implemented. Database metrics are correlated with host findings only for a local instance; limits specific to databases are under [Reading database limits](#reading-database-limits).
