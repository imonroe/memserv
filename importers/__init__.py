"""Standalone import tooling for bulk-loading existing data into the memory server.

These modules are operator/dev tooling, not part of the server runtime — they are
plain REST clients of ``POST /api/v1/memories`` and are not copied into the app
image. Each ``parse_*`` function is a pure generator over an export format,
yielding kwargs for ``importers.client.MemoryClient.add``; the thin CLIs in
``scripts/import_*.py`` wire a parser to the client.
"""
