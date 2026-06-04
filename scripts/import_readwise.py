#!/usr/bin/env python3
"""Import a Readwise highlights CSV export into the memory server.

One highlight becomes one memory; an attached note is appended. Example:

    python scripts/import_readwise.py ~/Downloads/readwise.csv --dry-run
    MEM0_URL=https://mem0.example.com MEM0_API_KEY=... \\
        python scripts/import_readwise.py ~/Downloads/readwise.csv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importers import cli, readwise  # noqa: E402 - path set up above before import


def main() -> int:
    args = cli.build_arg_parser(__doc__, default_source="readwise").parse_args()
    client = cli.make_client(args)
    records = readwise.load(args.path, source=args.source)
    return cli.run(records, client, limit=args.limit, label="highlights")


if __name__ == "__main__":
    raise SystemExit(main())
