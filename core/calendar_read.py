"""Google Calendar helpers (read for mail sections; write scope shared with sync)."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Tokyo")
# Full calendar scope — shared with calendar_sync (read + write).
SCOPES = ("https://www.googleapis.com/auth/calendar",)
_TAG_A = "予約"
_TAG_B = "抽選"
_GRP_A = "体育館"
_GRP_B = "公民館"
MonthKey = tuple[int, int]
CountRow = tuple[str, int]
CountMap = dict[MonthKey, dict[str, list[CountRow]]]


def _svc(cred_path: str) -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=list(SCOPES),
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _cfg(
    cred_path: str | None,
    cal_a: str | None,
    cal_b: str | None,
) -> tuple[str, str, str]:
    path = (cred_path if cred_path is not None else os.getenv("CFG_C1", "")).strip()
    id_a = (cal_a if cal_a is not None else os.getenv("CFG_C2", "")).strip()
    id_b = (cal_b if cal_b is not None else os.getenv("CFG_C3", "")).strip()
    if not path or not id_a or not id_b:
        raise RuntimeError("CFG_C1 / CFG_C2 / CFG_C3 missing")
    return path, id_a, id_b


def _day_bounds(d: date) -> tuple[str, str]:
    start = datetime.combine(d, time.min, tzinfo=_TZ)
    end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=_TZ)
    return start.isoformat(), end.isoformat()


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = datetime(year, month, 1, tzinfo=_TZ)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=_TZ)
    else:
        end = datetime(year, month + 1, 1, tzinfo=_TZ)
    return start.isoformat(), end.isoformat()


def _event_hhmm(ev: dict[str, Any]) -> str:
    start = ev.get("start") or {}
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        if dt.tzinfo is not None:
            dt = dt.astimezone(_TZ)
        return f"{dt.hour}:{dt.minute:02d}"
    return "0:00"


def _event_sort_key(ev: dict[str, Any]) -> tuple[int, int, str]:
    start = ev.get("start") or {}
    if "dateTime" in start:
        dt = datetime.fromisoformat(start["dateTime"])
        if dt.tzinfo is not None:
            dt = dt.astimezone(_TZ)
        return (dt.hour, dt.minute, str(ev.get("summary") or ""))
    return (0, 0, str(ev.get("summary") or ""))


def _list_range(
    svc: Any,
    cal_id: str,
    tmin: str,
    tmax: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": cal_id,
            "timeMin": tmin,
            "timeMax": tmax,
            "singleEvents": True,
            "orderBy": "startTime",
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


def _list_day(svc: Any, cal_id: str, d: date) -> list[dict[str, Any]]:
    tmin, tmax = _day_bounds(d)
    return _list_range(svc, cal_id, tmin, tmax)


def _list_month(svc: Any, cal_id: str, year: int, month: int) -> list[dict[str, Any]]:
    tmin, tmax = _month_bounds(year, month)
    return _list_range(svc, cal_id, tmin, tmax)


def _rows_for_dates(
    dates: list[date],
    *,
    cred_path: str | None = None,
    cal_a: str | None = None,
    cal_b: str | None = None,
    svc: Any | None = None,
) -> dict[date, list[str]]:
    path, id_a, id_b = _cfg(cred_path, cal_a, cal_b)
    client = svc if svc is not None else _svc(path)
    out: dict[date, list[str]] = {d: [] for d in dates}
    for d in dates:
        bundled: list[tuple[tuple[int, int, str], str]] = []
        for cal_id, tag in ((id_a, _TAG_A), (id_b, _TAG_B)):
            for ev in _list_day(client, cal_id, d):
                summary = str(ev.get("summary") or "").strip()
                if not summary:
                    continue
                line = f"{_event_hhmm(ev)} {summary}（{tag}）"
                bundled.append((_event_sort_key(ev), line))
        bundled.sort(key=lambda x: x[0])
        out[d] = [line for _, line in bundled]
    return out


def get_recent(
    dates: list[date],
    *,
    cred_path: str | None = None,
    cal_a: str | None = None,
    cal_b: str | None = None,
    svc: Any | None = None,
) -> dict[date, list[str]]:
    return _rows_for_dates(
        list(dates),
        cred_path=cred_path,
        cal_a=cal_a,
        cal_b=cal_b,
        svc=svc,
    )


def get_matched(
    dates: set[date],
    *,
    cred_path: str | None = None,
    cal_a: str | None = None,
    cal_b: str | None = None,
    svc: Any | None = None,
) -> dict[date, list[str]]:
    ordered = sorted(dates)
    return _rows_for_dates(
        ordered,
        cred_path=cred_path,
        cal_a=cal_a,
        cal_b=cal_b,
        svc=svc,
    )


def _cfg_stats(
    cred_path: str | None = None,
    cal_stats: str | None = None,
) -> tuple[str, str]:
    path = (cred_path if cred_path is not None else os.getenv("CFG_C1", "")).strip()
    cid = (
        cal_stats if cal_stats is not None else os.getenv("CFG_C4", "")
    ).strip()
    if not path or not cid:
        raise RuntimeError("CFG_C1 / CFG_C4 missing")
    return path, cid


def _parse_stats_rows(raw: Any) -> list[CountRow]:
    """Parse gym/hall JSON (or list) into sorted (name, count) rows."""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    rows: list[CountRow] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            n = int(item.get("count"))
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        rows.append((name, n))
    rows.sort(key=lambda x: (x[1], x[0]))
    return rows


def get_counts(
    months: list[MonthKey],
    *,
    cred_path: str | None = None,
    cal_stats: str | None = None,
    svc: Any | None = None,
) -> CountMap:
    """Read monthly reservation counts from CFG_C4 stats events (demand 5)."""
    path, cid_stats = _cfg_stats(cred_path, cal_stats)
    client = svc if svc is not None else _svc(path)
    out: CountMap = {}
    for year, month in months:
        month_key = f"{year:04d}-{month:02d}"
        month_out: dict[str, list[CountRow]] = {_GRP_A: [], _GRP_B: []}
        for ev in _list_month(client, cid_stats, year, month):
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            if not isinstance(priv, dict):
                continue
            if str(priv.get("stats_kind") or "") != "monthly_reservation_stats":
                continue
            if str(priv.get("stats_month") or "") != month_key:
                continue
            month_out[_GRP_A] = _parse_stats_rows(priv.get("gym"))
            month_out[_GRP_B] = _parse_stats_rows(priv.get("hall"))
            break
        out[(year, month)] = month_out
    return out
