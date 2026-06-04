"""Shared argparse wiring and the run loop for the import scripts."""

import argparse
import os
import sys
from collections.abc import Iterable

from importers.client import MemoryClient


def build_arg_parser(description: str | None, *, default_source: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("path", help="Path to the export file or vault directory")
    p.add_argument(
        "--base-url",
        default=os.environ.get("MEM0_URL"),
        help="Server base URL, e.g. https://mem0.example.com (default: $MEM0_URL)",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("MEM0_API_KEY"),
        help="Bearer token (default: $MEM0_API_KEY)",
    )
    p.add_argument(
        "--source",
        default=default_source,
        help=f"Provenance tag, stored as agent_id=import:<source> (default: {default_source})",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N memories — handy for a trial run before a full import",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report what would be sent, without calling the server",
    )
    return p


def make_client(args: argparse.Namespace) -> MemoryClient:
    if not args.dry_run and (not args.base_url or not args.api_key):
        sys.exit(
            "error: --base-url/$MEM0_URL and --api-key/$MEM0_API_KEY are required "
            "(or pass --dry-run to preview without sending)"
        )
    return MemoryClient(args.base_url or "", args.api_key or "", dry_run=args.dry_run)


def run(records: Iterable[dict], client: MemoryClient, *, limit: int | None, label: str) -> int:
    """Send each record; report progress and a final tally. Returns exit code."""
    sent = failed = 0
    for i, record in enumerate(records):
        if limit is not None and i >= limit:
            break
        try:
            client.add(**record)
            sent += 1
            if client.dry_run:
                print(f"[dry-run] would send {label} #{i + 1}: {record.get('metadata', {})}")
        except Exception as exc:  # noqa: BLE001 - one bad record shouldn't abort the import
            failed += 1
            print(f"  ! failed {label} #{i + 1}: {exc}", file=sys.stderr)
        if not client.dry_run and (i + 1) % 25 == 0:
            print(f"  ... {i + 1} {label} processed")
    verb = "would send" if client.dry_run else "sent"
    print(f"Done: {sent} {verb}, {failed} failed.")
    return 1 if failed and not sent else 0
