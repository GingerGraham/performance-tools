"""analyse, plot and report subcommands."""

import os
import sys

from . import findings, loader
from .analysis import run_analysis
from .bundle import open_bundle
from .thresholds import SettingsError, build_settings
from .timeutil import TimeError, parse_when


def _add_analysis_arguments(parser):
    parser.add_argument("--config", metavar="FILE", help="INI file with threshold overrides")
    parser.add_argument("--set", action="append", metavar="KEY=VALUE", help="override any setting (repeatable)")
    parser.add_argument("--from", dest="time_from", metavar="TIME", help="HH:MM, 'YYYY-mm-dd HH:MM' or epoch")
    parser.add_argument("--to", dest="time_to", metavar="TIME", help="HH:MM, 'YYYY-mm-dd HH:MM' or epoch")
    parser.add_argument("--tz", help="display zone: UTC or an offset such as +01:00 (default: the host's zone)")
    parser.add_argument("--latency", action="append", metavar="CLASS=WARN[,CRIT]",
                        help="override a class's latency thresholds in ms, e.g. nvme=1,5")
    parser.add_argument("--device-limit", action="append", metavar="DEV:iops=N,mibps=N",
                        help="declared device limits; enables limit-based throughput rules (mibps is MiB/s)")
    parser.add_argument("--spindles", action="append", metavar="DEV=N", help="parallelism of a hardware RAID device")
    parser.add_argument("--class", dest="device_class", action="append", metavar="DEV=CLASS",
                        help="override the detected media class of a device")
    parser.add_argument("--include-partitions", action="store_true",
                        help="analyse partitions (sda1) as separate devices, judged as their parent disk "
                             "(default: partitions are folded into the disk)")
    parser.add_argument("--sustain", metavar="DURATION", help="sustain window, e.g. 10m (default 5m)")


def register(sub, add_bundle_arguments):
    analyse = sub.add_parser("analyse", aliases=["analyze"], help="rank findings and print them")
    add_bundle_arguments(analyse)
    _add_analysis_arguments(analyse)
    analyse.add_argument("--json", metavar="FILE", help="also write findings.json to FILE")

    plot = sub.add_parser("plot", help="render charts (needs matplotlib)")
    add_bundle_arguments(plot)
    _add_analysis_arguments(plot)
    plot.add_argument("-o", "--outdir", required=True, metavar="DIR", help="output directory")
    plot.add_argument("--format", choices=("svg", "png"), default="svg")

    report = sub.add_parser("report", help="write report.html and/or report.md (needs matplotlib)")
    add_bundle_arguments(report)
    _add_analysis_arguments(report)
    report.add_argument("-o", "--outdir", required=True, metavar="DIR", help="output directory")
    report.add_argument("--format", choices=("html", "md", "both"), default="html")


def analysed_hosts(args, bundle, zone_override):
    """Yields (host, result) for every host in the bundle."""
    settings = build_settings(args)
    for name in bundle.hosts():
        try:
            host = loader.load_host(bundle, name, zone_override, args.include_partitions)
        except loader.LoadError as error:
            sys.stderr.write("NOTE: skipping host %s: %s\n" % (name, error))
            continue
        start = end = None
        if args.time_from:
            start = parse_when(args.time_from, host.zone, host.start)
        if args.time_to:
            end = parse_when(args.time_to, host.zone, host.start)
        if start is not None and end is not None and end <= start:
            raise TimeError("--to must be later than --from")
        loader.apply_window(host, start, end)
        if host.start is None:
            sys.stderr.write("NOTE: host %s has no samples in the requested window\n" % name)
            continue
        yield host, run_analysis(host, settings)


def run(args, zone_override):
    try:
        if args.command in ("analyse", "analyze"):
            return _analyse(args, zone_override)
        if args.command == "plot":
            return _plot(args, zone_override)
        if args.command == "report":
            return _report(args, zone_override)
    except SettingsError as error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 2
    raise NotImplementedError(args.command)


def _json_path(path, host_name, multiple):
    if not multiple:
        return path
    stem, extension = os.path.splitext(path)
    return "%s-%s%s" % (stem, host_name, extension or ".json")


def _analyse(args, zone_override):
    with open_bundle(args.bundle, args.keep_extracted) as bundle:
        multiple = len(bundle.hosts()) > 1
        produced = 0
        for host, result in analysed_hosts(args, bundle, zone_override):
            produced += 1
            sys.stdout.write(findings.render_text(
                result, json_path=_json_path(args.json, host.host, multiple) if args.json else None))
            if args.json:
                path = _json_path(args.json, host.host, multiple)
                findings.write_json(result, path)
                sys.stderr.write("wrote %s\n" % path)
            if multiple:
                sys.stdout.write("\n")
        if produced == 0:
            sys.stderr.write("ERROR: nothing to analyse\n")
            return 1
    return 0


def _plot(args, zone_override):
    from .deps import require_matplotlib

    require_matplotlib()
    from . import charts

    os.makedirs(args.outdir, exist_ok=True)
    with open_bundle(args.bundle, args.keep_extracted) as bundle:
        produced = 0
        for host, result in analysed_hosts(args, bundle, zone_override):
            produced += 1
            target = os.path.join(args.outdir, host.host) if len(bundle.hosts()) > 1 else args.outdir
            os.makedirs(target, exist_ok=True)
            for path in charts.write_all(result, target, args.format):
                sys.stdout.write("%s\n" % path)
        if produced == 0:
            sys.stderr.write("ERROR: nothing to plot\n")
            return 1
    return 0


def _report(args, zone_override):
    from . import report as report_module
    from .deps import require_matplotlib

    require_matplotlib()
    os.makedirs(args.outdir, exist_ok=True)
    with open_bundle(args.bundle, args.keep_extracted) as bundle:
        results = []
        for host, result in analysed_hosts(args, bundle, zone_override):
            results.append(result)
        if not results:
            sys.stderr.write("ERROR: nothing to report\n")
            return 1
        for path in report_module.write_reports(results, args.outdir, args.format):
            sys.stdout.write("%s\n" % path)
    return 0
