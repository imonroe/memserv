"""Parse a Readwise highlights CSV export into memory records.

Readwise's CSV export has a header row with columns like ``Highlight``,
``Book Title``, ``Book Author``, and ``Note``. One highlight becomes one memory;
an attached note (if any) is appended to the highlight text.
"""

import csv
from collections.abc import Iterable, Iterator

_TEXT_COLUMNS = ("Highlight", "Text")
_NOTE_COLUMNS = ("Note",)
_META_COLUMNS = {"book": ("Book Title", "Title"), "author": ("Book Author", "Author")}


def _first(row: dict, columns) -> str:
    for col in columns:
        value = (row.get(col) or "").strip()
        if value:
            return value
    return ""


def parse_highlights(rows: Iterable[dict], *, source: str = "readwise") -> Iterator[dict]:
    """Yield ``MemoryClient.add`` kwargs from rows of a Readwise CSV export."""
    for row in rows:
        text = _first(row, _TEXT_COLUMNS)
        if not text:
            continue
        note = _first(row, _NOTE_COLUMNS)
        content = text if not note else f"{text}\n\nNote: {note}"
        metadata = {"source": source}
        for key, columns in _META_COLUMNS.items():
            value = _first(row, columns)
            if value:
                metadata[key] = value
        yield {
            "content": content,
            "agent_id": f"import:{source}",
            "metadata": metadata,
        }


def load(path: str, *, source: str = "readwise") -> Iterator[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        yield from parse_highlights(csv.DictReader(f), source=source)
