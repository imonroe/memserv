#!/usr/bin/env python3
"""Import a ChatGPT data export (conversations.json) into the memory server.

Each conversation is sent as a messages payload so the server extracts facts from
it. Example:

    python scripts/import_chatgpt.py ~/Downloads/conversations.json --dry-run
    MEM0_URL=https://mem0.example.com MEM0_API_KEY=... \\
        python scripts/import_chatgpt.py ~/Downloads/conversations.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from importers import chatgpt, cli  # noqa: E402 - path set up above before import


def main() -> int:
    args = cli.build_arg_parser(__doc__, default_source="chatgpt").parse_args()
    client = cli.make_client(args)
    records = chatgpt.load(args.path, source=args.source)
    return cli.run(records, client, limit=args.limit, label="conversations")


if __name__ == "__main__":
    raise SystemExit(main())
