# performance-tools

## Linux scripts

User guides, one per script: what it collects, its options, its output and how to run it.

| Script | Purpose | Guide |
|--------|---------|-------|
| `linux/os/collect-os-metrics.sh` | Detached `sadc` and `pidstat` capture for baselines and incidents | [docs/collect-os-metrics.md](docs/collect-os-metrics.md) |
| `linux/os/psar.sh` | Live `sar`-like view from `/proc`, no `sysstat` needed, optional CSV | [docs/psar.md](docs/psar.md) |
| `linux/db/collect-postgresql-metrics.sh` | PostgreSQL statistics to CSV | [docs/collect-postgresql-metrics.md](docs/collect-postgresql-metrics.md) |
| `linux/db/collect-mysql-metrics.sh` | MySQL / MariaDB status counters to CSV | [docs/collect-mysql-metrics.md](docs/collect-mysql-metrics.md) |
| `linux/db/collect-oracle-metrics.sh` | Oracle system statistics to CSV | [docs/collect-oracle-metrics.md](docs/collect-oracle-metrics.md) |
| `linux/db/collect-mssql-metrics.sh` | SQL Server performance counters to CSV | [docs/collect-mssql-metrics.md](docs/collect-mssql-metrics.md) |

Command-line conventions that every script must follow are in [docs/linux-cli-standard.md](docs/linux-cli-standard.md). That document is for people writing or changing the scripts.
