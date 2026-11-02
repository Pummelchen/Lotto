#!/usr/bin/env python3
"""Unified entry point for LOTTO 6aus49 and Eurojackpot."""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified lottery analyzer CLI. Global options must precede the game command.",
    )
    parser.add_argument(
        "--game",
        choices=("lotto", "eurojackpot"),
        default="lotto",
        help="game analyzer to run (default: lotto)",
    )
    args, remaining = parser.parse_known_args()
    offline = "--offline" in remaining
    remaining = [argument for argument in remaining if argument != "--offline"]
    if offline:
        remaining.insert(0, "--offline")
    sys.argv = [sys.argv[0], *remaining]
    if args.game == "eurojackpot":
        import eurojackpot

        eurojackpot.main()
    else:
        import app

        app.main()


if __name__ == "__main__":
    main()
