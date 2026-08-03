#!/usr/bin/env python3
"""Probe Calendar read auth (no site HTTP)."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.calendar_read import get_recent  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    for key in ("CFG_C1", "CFG_C2", "CFG_C3"):
        val = os.getenv(key, "").strip()
        print(f"{key}: {'set' if val else 'MISSING'}" + (f" ({val})" if key == "CFG_C1" and val else ""))

    day0 = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    days = [day0, day0 + timedelta(days=1)]
    print(f"get_recent({days[0].isoformat()}, {days[1].isoformat()}) ...")
    try:
        rows = get_recent(days)
    except Exception:
        traceback.print_exc()
        return 1

    for d in days:
        items = rows.get(d) or []
        print(f"--- {d.isoformat()} ({len(items)}) ---")
        if not items:
            print("(empty)")
        else:
            for line in items:
                print(line)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
