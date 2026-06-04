"""Parse an Obsidian vault (a directory of Markdown notes) into memory records.

One note becomes one memory. YAML frontmatter is stripped, and Obsidian's own
dotfolders (``.obsidian``, ``.trash``) plus ``.git`` are skipped.
"""

import os
import re
from collections.abc import Iterator

_SKIP_DIRS = {".obsidian", ".trash", ".git"}
# A leading YAML frontmatter block: --- ... --- at the very top of the file.
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def parse_vault(root: str, *, source: str = "obsidian") -> Iterator[dict]:
    """Yield ``MemoryClient.add`` kwargs, one per non-empty Markdown note."""
    for dirpath, dirs, files in os.walk(root):
        # Prune skip-dirs in place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            if not name.endswith(".md"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8") as f:
                body = strip_frontmatter(f.read()).strip()
            if not body:
                continue
            rel = os.path.relpath(full, root)
            yield {
                "content": body,
                "agent_id": f"import:{source}",
                "metadata": {"source": source, "path": rel, "title": name[:-3]},
            }
