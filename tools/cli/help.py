"""`callisto help` — usage and safety summary for the front door."""
from __future__ import annotations

import argparse


def cmd_help(args: argparse.Namespace) -> int:
    from callisto import build_parser
    parser = build_parser()
    parser.print_help()
    return 0


_cmd_help = cmd_help  # backwards-compatible alias
