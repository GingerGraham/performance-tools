#!/usr/bin/env python3
"""Entry point: python3 perfdiag.py <subcommand> ...

Run from a source checkout without installing anything. The package directory
sits next to this file.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from perfdiag.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
