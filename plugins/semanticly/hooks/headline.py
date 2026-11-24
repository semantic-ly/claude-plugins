#!/usr/bin/env python3
"""Print the headline of an `ingest --json` answer in the CLI's own words.

Usage: headline.py < BODY

An empty or unparseable body prints nothing and exits 0. A CLI that does not
load exits hook_inputs.EXIT_STOP with one reason on stderr.
"""

from __future__ import annotations

import json
import sys

sys.dont_write_bytecode = True

from hook_inputs import EXIT_STOP, Stop, load_cli  # noqa: E402


def main() -> int:
    try:
        body = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(body, dict):
        return 0
    try:
        cli = load_cli()
    except Stop as stop:
        print(stop, file=sys.stderr)
        return EXIT_STOP
    print(cli.ingest_headline(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
