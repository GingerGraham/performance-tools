# perfdiag

Workstation analysis for bundles from `linux/os/gather-perf-diag.sh`. User guide: [docs/perfdiag.md](../../docs/perfdiag.md).

```bash
python3 perfdiag.py check
python3 perfdiag.py analyse BUNDLE
python3 perfdiag.py report BUNDLE -o out/
```

## Layout

```
perfdiag.py              entry point
perfdiag/
    cli.py cli_analysis.py   argparse subcommands
    deps.py                  dependency check and install hints
    bundle.py                safe extraction of .tar.gz, .zip and directories
    loader.py                builds one HostData per host from its runs
    readers/                 sadf, psar, pidstat, manifest and hardware, kernel log
    readers/dbcsv.py         header-driven readers for the database collector CSV files
    canonical.py             column names -> canonical metric names
    thresholds.py            defaults, INI, --set and flag overrides, disk classes
    rules.py                 indicators (derived series with thresholds)
    episodes.py plateau.py   episode detection and scoring, plateau detection
    analysis.py              runs the rules, classifies transients
    hypotheses.py            H1-H12, kernel events
    attribution.py           top processes from pidstat
    findings.py              finding text, findings.json, text output
    charts.py                matplotlib charts
    dbdetect.py              database signature table, process instances from pidstat and dbproc.*, process roles
    dbengines.py             one adapter per engine: raw collector tables -> canonical db.* series
    dbload.py                builds DatabaseInstance objects, merges process data, maps file paths to devices
    dbrules.py               database indicators (db_read_latency, db_lock_wait, ...)
    dbhypotheses.py          D1-D9, links to the H hypotheses, engine next steps
    dbcharts.py              the five charts per database instance
    report_db.py             data for the Databases part of the reports, coverage lines, findings.json entries
    report_common.py report_html.py report_md.py report.py
tests/                       unittest, no pytest needed
    fakeclients/             fake psql, mysql, mariadb, sqlplus and sqlcmd for the collector tests
    synth_db.py              writes synthetic database collector runs (schema 3, and a 2.0.0 PostgreSQL run)
    dbsupport.py             helpers for running the collector scripts against the fake clients
    test_db_*.py             database collectors and the database part of perfdiag
```

Parsing and analysis use the standard library only. Only `charts.py` and the report need matplotlib, and it is imported lazily.

## Tests

```bash
cd linux/analysis/tests
python3 -m unittest discover -s . -p 'test_*.py'
```

To run only the database tests:

```bash
python3 -m unittest discover -s . -p 'test_db_*.py'
```

- `test_hw_inventory.py` builds a fake `/sys` tree (`fakesys.py`) and runs `collect-hw-inventory.sh` against it, because md and device-mapper often cannot be created in a test environment. The script reads `HW_INVENTORY_SYSFS` and `HW_INVENTORY_MOUNTINFO` for this. They are test hooks, not options.
- `test_gather.py` runs `gather-perf-diag.sh` on generated run directories. Tests that need `sadf` are skipped without sysstat.
- `synth.py` writes synthetic sadf-format run directories for the scenario tests. `fixtures/real_96s/` is a real 96-second `sadc` capture with a known load pattern (a 5 s CPU burst, a 5 s direct-IO write burst, then 45 s of direct-IO write plus CPU burn).
- `fakeclients/` holds small bash scripts that stand in for the database clients. They print canned output chosen by environment variables (for example `FAKE_PG_VERSION`) and log their arguments to the file named by `FAKE_LOG`. `dbsupport.py` puts them first on `PATH` and runs the scripts in `linux/db/` against them.
- `synth_db.py` writes database collector run directories on the same timeline as `synth.py`, so an OS run and a database run line up. A test sets the per-sample values it cares about (`pg.fill("read_latency_ms", 30, 90, 40.0)`) and calls `write()`. Counters are built from rates, so the rate perfdiag computes equals the rate the test set.
- `test_db_postgresql.py`, `test_db_mysql.py`, `test_db_oracle.py`, `test_db_mssql.py` and `test_db_blockd.py` test the collector scripts: output columns per version, client arguments, record routing, timeouts, locality, the manifest, and that the code shared by the four scripts is identical and passes `shellcheck`. The PostgreSQL and MySQL tests also run against a live server when one answers with the default login, and are skipped otherwise. There is no Oracle or SQL Server server in the test environment.
- `test_db_readers.py`, `test_db_load.py`, `test_db_rules.py`, `test_db_hypotheses.py`, `test_db_charts.py`, `test_db_report.py` and `test_db_render.py` test perfdiag: CSV readers, loading and merging, path mapping, indicators, D hypotheses and links, charts, report data, and the Markdown, HTML and JSON output.
- Tests that need matplotlib are skipped without it.

The scripts under `linux/os/` and `linux/db/` are checked with `shellcheck` (`sudo dnf install ShellCheck` on Fedora and RHEL).
