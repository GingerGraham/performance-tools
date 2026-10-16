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
    canonical.py             column names -> canonical metric names
    thresholds.py            defaults, INI, --set and flag overrides, disk classes
    rules.py                 indicators (derived series with thresholds)
    episodes.py plateau.py   episode detection and scoring, plateau detection
    analysis.py              runs the rules, classifies transients
    hypotheses.py            H1-H10, kernel events
    attribution.py           top processes from pidstat
    findings.py              finding text, findings.json, text output
    charts.py                matplotlib charts
    report_common.py report_html.py report_md.py report.py
tests/                       unittest, no pytest needed
```

Parsing and analysis use the standard library only. Only `charts.py` and the report need matplotlib, and it is imported lazily.

## Tests

```bash
cd linux/analysis/tests
python3 -m unittest discover -s . -p 'test_*.py'
```

- `test_hw_inventory.py` builds a fake `/sys` tree (`fakesys.py`) and runs `collect-hw-inventory.sh` against it, because md and device-mapper often cannot be created in a test environment. The script reads `HW_INVENTORY_SYSFS` and `HW_INVENTORY_MOUNTINFO` for this. They are test hooks, not options.
- `test_gather.py` runs `gather-perf-diag.sh` on generated run directories. Tests that need `sadf` are skipped without sysstat.
- `synth.py` writes synthetic sadf-format run directories for the scenario tests. `fixtures/real_96s/` is a real 96-second `sadc` capture with a known load pattern (a 5 s CPU burst, a 5 s direct-IO write burst, then 45 s of direct-IO write plus CPU burn).
- Tests that need matplotlib are skipped without it.

The scripts under `linux/os/` are checked with `shellcheck`.
