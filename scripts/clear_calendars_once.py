#!/usr/bin/env python3
"""One-off: delete ALL events from CFG_C2 and CFG_C3 (no date filter).

Uses local/service-account.json. Requires typing ``yes`` to confirm.
Remove this script after use to avoid accidental runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.calendar_read import SCOPES  # noqa: E402

CRED_PATH = ROOT / "local" / "service-account.json"


def _build_svc(cred_path: Path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        str(cred_path),
        scopes=list(SCOPES),
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _list_all_events(svc, cal_id: str) -> list[dict]:
    items: list[dict] = []
    token: str | None = None
    while True:
        kwargs: dict = {
            "calendarId": cal_id,
            "singleEvents": True,
            "maxResults": 2500,
        }
        if token:
            kwargs["pageToken"] = token
        resp = svc.events().list(**kwargs).execute()
        items.extend(list(resp.get("items") or []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def _clear_calendar(svc, label: str, cal_id: str) -> int:
    print(f"\n=== {label} ({cal_id}) ===")
    print("Listing all events (no timeMin/timeMax) ...")
    events = _list_all_events(svc, cal_id)
    total = len(events)
    print(f"Found {total} event(s).")
    if total == 0:
        return 0
    deleted = 0
    for i, ev in enumerate(events, start=1):
        eid = ev.get("id")
        summary = str(ev.get("summary") or "(no title)")
        if not eid:
            print(f"  skip {i}/{total}: missing event id summary={summary!r}")
            continue
        print(f"  deleting {i}/{total}: {summary!r} id={eid}")
        svc.events().delete(calendarId=cal_id, eventId=eid).execute()
        deleted += 1
    return deleted


def main() -> int:
    load_dotenv(ROOT / ".env")

    cal_c2 = os.getenv("CFG_C2", "").strip()
    cal_c3 = os.getenv("CFG_C3", "").strip()
    if not cal_c2 or not cal_c3:
        print("CFG_C2 / CFG_C3 missing in .env", file=sys.stderr)
        return 1
    if not CRED_PATH.is_file():
        print(f"Credential file not found: {CRED_PATH}", file=sys.stderr)
        return 1

    print("WARNING: 即将删除全部历史事件（含过去日期），不可恢复。")
    print(f"  CFG_C2 (予約): {cal_c2}")
    print(f"  CFG_C3 (抽選): {cal_c3}")
    print(f"  Credential:    {CRED_PATH}")
    ans = input('Type "yes" to proceed: ').strip()
    if ans != "yes":
        print("Aborted (confirmation not received).")
        return 1

    svc = _build_svc(CRED_PATH)
    n2 = _clear_calendar(svc, "CFG_C2", cal_c2)
    n3 = _clear_calendar(svc, "CFG_C3", cal_c3)

    print()
    print(f"CFG_C2 共删除 {n2} 个事件，CFG_C3 共删除 {n3} 个事件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
