# performance-tools

## Linux scripts

User guides, one per script: what it collects, its options, its output and how to run it.

| Script | Purpose | Guide |
|--------|---------|-------|
| `linux/os/collect-os-metrics.sh` | Detached `sadc` and `pidstat` capture for baselines and incidents | [docs/collect-os-metrics.md](docs/collect-os-metrics.md) |
| `linux/os/psar.sh` | Live `sar`-like view from `/proc`, no `sysstat` needed, optional CSV | [docs/psar.md](docs/psar.md) |
| `linux/os/collect-hw-inventory.sh` | One-shot hardware inventory: CPU topology, disk stacking, RAID membership, media class, database server processes | [docs/collect-hw-inventory.md](docs/collect-hw-inventory.md) |
| `linux/os/gather-perf-diag.sh` | Bundles run directories, including database collector runs, into one `.tar.gz` with CSV exports, a kernel log excerpt and redacted `pidstat` logs. Notes a database server that has no collector run | [docs/gather-perf-diag.md](docs/gather-perf-diag.md) |
| `linux/db/collect-postgresql-metrics.sh` | PostgreSQL statistics, wait events, IO timing, settings and file locations to CSV | [docs/collect-postgresql-metrics.md](docs/collect-postgresql-metrics.md) |
| `linux/db/collect-mysql-metrics.sh` | MySQL / MariaDB status counters, InnoDB file IO, settings and file locations to CSV | [docs/collect-mysql-metrics.md](docs/collect-mysql-metrics.md) |
| `linux/db/collect-oracle-metrics.sh` | Oracle system statistics, time model, wait events, settings and file locations to CSV. Reads no ASH or AWR views | [docs/collect-oracle-metrics.md](docs/collect-oracle-metrics.md) |
| `linux/db/collect-mssql-metrics.sh` | SQL Server performance counters, wait statistics, per-file IO, settings and file locations to CSV | [docs/collect-mssql-metrics.md](docs/collect-mssql-metrics.md) |

## Analysis

`perfdiag` is a Python tool for a workstation (Fedora, macOS, WSL). It reads a bundle from `gather-perf-diag.sh`, ranks problems by how far over a threshold and for how long, groups them into hypotheses with next steps, and writes a single-file HTML report. It also analyses database collector runs and database processes in the bundle, and ties database waits and latency to the host findings.

| Tool | Purpose | Guide |
|------|---------|-------|
| `linux/analysis/perfdiag.py` | `check`, `inspect`, `analyse`, `plot`, `report` | [docs/perfdiag.md](docs/perfdiag.md) |

The design of the database collectors and of the database part of `perfdiag` is in [docs/specs/db-collectors-v3.md](docs/specs/db-collectors-v3.md).

Command-line conventions that every script must follow are in [docs/linux-cli-standard.md](docs/linux-cli-standard.md). That document is for people writing or changing the scripts.
