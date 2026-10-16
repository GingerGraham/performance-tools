"""Command line interface: perfdiag check | inspect | analyse | plot | report."""

import argparse
import sys

from . import VERSION
from .bundle import BundleError, open_bundle
from .deps import DependencyError, run_check
from .timeutil import TimeError, offset_zone

PROGRAM = "perfdiag"


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Analyse perf-diag bundles from gather-perf-diag.sh.")
    parser.add_argument("-V", "--version", action="version", version="%s %s" % (PROGRAM, VERSION))
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("check", help="check Python, matplotlib and the optional sadf")

    inspect_parser = sub.add_parser("inspect", help="show what a bundle contains")
    _add_bundle_arguments(inspect_parser)
    inspect_parser.add_argument("--tz", help="display zone: UTC or an offset such as +01:00")

    from . import cli_analysis
    cli_analysis.register(sub, _add_bundle_arguments)
    return parser


def _add_bundle_arguments(parser):
    parser.add_argument("bundle", metavar="BUNDLE", help=".tar.gz, .zip, gather directory or run directory")
    parser.add_argument("--keep-extracted", action="store_true",
                        help="keep the temporary extraction directory (printed on exit)")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except (BundleError, DependencyError, TimeError) as error:
        sys.stderr.write("ERROR: %s\n" % error)
        return 1
    except BrokenPipeError:
        return 0


def dispatch(args):
    if args.command == "check":
        return run_check()
    zone = offset_zone(args.tz) if getattr(args, "tz", None) else None
    if args.command == "inspect":
        from .inspect_cmd import inspect_bundle
        with open_bundle(args.bundle, args.keep_extracted, announce=_announce) as bundle:
            return inspect_bundle(bundle, sys.stdout, zone)
    from . import cli_analysis
    return cli_analysis.run(args, zone)


def _announce(text):
    sys.stderr.write("NOTE: %s\n" % text)
