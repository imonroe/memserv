#!/usr/bin/env python3
"""Import an Obsidian vault (a directory of Markdown notes) into the memory server.

One note becomes one memory; YAML frontmatter is stripped. Example:

    python scripts/import_obsidian.py ~/vault --dry-run
    MEM0_URL=https://mem0.example.com MEM0_API_KEY=... \\
        python scripts/import_obsidian.py ~/vault
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importers import cli, obsidian  # noqa: E402 - path set up above before import


def main() -> int:
    args = cli.build_arg_parser(__doc__, default_source="obsidian").parse_args()
    client = cli.make_client(args)
    records = obsidian.parse_vault(args.path, source=args.source)
    return cli.run(records, client, limit=args.limit, label="notes")


if __name__ == "__main__":
    raise SystemExit(main())
