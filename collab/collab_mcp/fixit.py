"""Bridge to the standalone ``fixit`` FTS5 troubleshooting index.

``C:\\myshare\\fixit`` is a small CLI/index that turns local pitfalls into
searchable ``symptom -> fix`` cards.  This module exposes that index to other
collab sessions without changing fixit itself: it imports its plain-python
readers lazily and returns the already structured result rows.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .identity import assert_identity_allowed
from .utils import fail, ok

_FIXIT_DIR = Path(__file__).resolve().parents[2] / "fixit"
_state: dict[str, object] = {}


def _ensure_modules() -> tuple[object, object]:
    """Return ``(ingest, index)``, importing fixit's standalone modules once."""
    ingest = _state.get("ingest")
    index = _state.get("index")
    if ingest is not None and index is not None:
        return ingest, index

    if not _FIXIT_DIR.is_dir():
        raise FileNotFoundError(f"fixit 索引目录不存在: {_FIXIT_DIR}")

    fixit_path = str(_FIXIT_DIR)
    if fixit_path not in sys.path:
        sys.path.insert(0, fixit_path)

    # fixit/index.py itself does ``from ingest import ...``, so both modules
    # must be loaded with the fixit directory on sys.path.
    ingest = importlib.import_module("ingest")
    index = importlib.import_module("index")
    _state["ingest"] = ingest
    _state["index"] = index
    return ingest, index


async def search_troubleshooting(query: str, limit: int = 5) -> str:
    """Search the local fixit troubleshooting index.

    Returns ranked ``key/title/symptom/fix/source_path`` cards for a symptom
    query.  The index is a derived FTS5 cache and rebuilds automatically when
    the source corpus changes.

    Args:
        query: Symptom text (required; spaces separate terms, all must match).
        limit: Maximum cards, default 5, clamped to [1, 20].
    """
    denied = assert_identity_allowed("search_troubleshooting")
    if denied:
        return fail(denied)

    q = (query or "").strip()
    if not q:
        return fail("search_troubleshooting 需要非空 query")

    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5

    try:
        _ingest, index = _ensure_modules()
        conn, tokenizer = index.ensure()
        results = index.search(conn, tokenizer, q, limit)
    except Exception as exc:  # noqa: BLE001 - tool should report, not crash server
        return fail(f"本地排障索引检索失败: {exc}")

    return ok({
        "count": len(results),
        "query": q,
        "limit": limit,
        "results": results,
    })
