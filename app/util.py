"""Shared low-level filesystem helpers used by every persistence layer
in the app (jobs queue, two-level cache, transcript cache, stats
sidecars, settings.json).

Both functions here exist to deduplicate patterns that appeared 4-6
times across the codebase pre-0.8.3, each implementing the same idea
with slightly different boilerplate. Centralizing them:

- removes the risk of "fix the atomic-write race in one place but not
  the others";
- keeps the JSON-load-with-quarantine policy uniform (corrupt files
  get renamed to ``.corrupt`` so a future operator can investigate);
- gives downstream test code a single seam for mocking persistence.

Neither helper raises in the normal flow — write errors are caller-
specific (some sites want them swallowed as best-effort, others want
them to bubble up) so this module deliberately stays simple and
delegates that decision to the caller.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any


def atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    The write goes to ``<path>.tmp`` first, then ``os.replace()`` moves
    it into place — so a crash, SIGKILL, or container restart mid-write
    can never leave a half-written file behind. The directory is
    created if it doesn't exist.

    Raises ``OSError`` on disk-full / permission errors. Callers decide
    whether to swallow (persistence is best-effort: jobs queue, stats
    sidecar) or surface (correctness-critical: settings.json).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def load_json_with_quarantine(
    path: Path,
    log: logging.Logger,
    *,
    label: str = "",
) -> Any:
    """Load JSON from ``path``. Return None if missing, the parsed
    object on success, None on corruption.

    On corruption, the file is renamed to ``<path>.corrupt`` so a
    future operator can investigate, and a warning is logged. The
    optional ``label`` prefixes the log message to distinguish the
    caller (e.g. ``"jobs_store"``, ``"transcript_cache"``).

    Used by: ``app/cache.py``, ``app/transcript_cache.py``,
    ``app/jobs_store.py``, ``app/config.py``. Each had a slightly
    different implementation pre-0.8.3 — this helper unifies the
    policy so corrupt-file handling stays consistent across the app.
    """
    if not path.exists():
        return None
    prefix = f"{label}: " if label else ""
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        try:
            backup = path.with_suffix(path.suffix + ".corrupt")
            path.rename(backup)
            log.warning(
                "%s%s was unreadable (%s); renamed to %s — starting clean",
                prefix, path, e, backup,
            )
        except OSError as backup_err:
            log.warning(
                "%s%s was unreadable (%s) AND could not be renamed (%s) — "
                "starting clean",
                prefix, path, e, backup_err,
            )
        return None
