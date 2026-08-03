"""Consecutive overall-run failure counter and alert cadence."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FIRST_AT = 3
EVERY = 6


def should_alert(n: int) -> bool:
    """True at first threshold, then every EVERY additional failures."""
    if n < FIRST_AT:
        return False
    return (n - FIRST_AT) % EVERY == 0


def load_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return max(0, int(raw.get("n", 0)))
        if isinstance(raw, int):
            return max(0, raw)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        logger.warning("fail count load failed: %s", path)
    return 0


def save_count(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"n": max(0, int(n))}, f, ensure_ascii=False, indent=2)


def apply_outcome(ok: bool, path: Path) -> tuple[int, bool]:
    """Update counter. Returns (new_count, should_send_alert)."""
    if ok:
        save_count(path, 0)
        return 0, False
    n = load_count(path) + 1
    save_count(path, n)
    return n, should_alert(n)
