"""HTTP helpers and local state for daily task."""

from __future__ import annotations

import calendar
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jpholiday

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PATH_T1 = DATA_DIR / "t1.json"
PATH_T2 = DATA_DIR / "t2.json"
PATH_CFG = ROOT / "config" / "cfg_items.json"

DELTA_T = timedelta(hours=4)
CUT_H = 17
BOUND_T = "19:00"
_ST_OK = "available"
_MAX_TRY = 3
_WAIT_S = 2.0


def helper_1(today: date | datetime) -> date:
    if isinstance(today, datetime):
        today = today.date()
    y, m = today.year, today.month + 1
    if m > 12:
        y += 1
        m = 1
    return date(y, m, calendar.monthrange(y, m)[1])


def helper_2(today: date | datetime) -> date:
    if isinstance(today, datetime):
        today = today.date()
    y, m = today.year, today.month + 2
    if m > 12:
        y += 1
        m -= 12
    return date(y, m, calendar.monthrange(y, m)[1])


def helper_3(d: date | datetime) -> bool:
    if isinstance(d, datetime):
        d = d.date()
    if d.weekday() >= 5:
        return True
    return bool(jpholiday.is_holiday(d))


def helper_4(today: date | datetime) -> date:
    if isinstance(today, datetime):
        cur = today
        day = today.date()
    else:
        cur = datetime.now()
        day = today
    last = calendar.monthrange(day.year, day.month)[1]
    if day.day == last and cur.hour >= CUT_H:
        return helper_2(day)
    return helper_1(day)


def helper_5(d: date, time_string: str) -> bool:
    start = time_string.split("-", 1)[0].strip()
    if helper_3(d):
        return True
    return start >= BOUND_T


def helper_6(start: date, end: date) -> list[date]:
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            y += 1
            m = 1
    return out


def _load_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}
    except (json.JSONDecodeError, OSError):
        logger.exception("load failed: %s", path)
        return {}


def _save_map(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return []
    except (json.JSONDecodeError, OSError):
        logger.exception("load failed: %s", path)
        return []


def _save_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False, indent=2)


def load_cfg(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or PATH_CFG
    with p.open(encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    return list(items)


def proc_b(
    current_keys: set[str],
    now: datetime,
    *,
    path_t1: Path | None = None,
) -> None:
    p1 = path_t1 or PATH_T1
    t1 = _load_map(p1)
    for k in list(t1.keys()):
        if k not in current_keys:
            del t1[k]
    stamp = now.isoformat(timespec="seconds")
    for k in current_keys:
        if k not in t1:
            t1[k] = stamp
    _save_map(p1, t1)


def proc_c(
    current_keys: set[str],
    now: datetime,
    *,
    path_t1: Path | None = None,
    delta: timedelta | None = None,
) -> set[str]:
    p1 = path_t1 or PATH_T1
    thr = delta if delta is not None else DELTA_T
    t1 = _load_map(p1)
    ready: set[str] = set()
    for k in current_keys:
        raw = t1.get(k)
        if not raw:
            continue
        try:
            seen = datetime.fromisoformat(raw)
        except ValueError:
            seen = now - thr
        if now - seen >= thr:
            ready.add(k)
    return ready


def proc_e(new_lines: list[str], *, path_t2: Path | None = None) -> bool:
    p2 = path_t2 or PATH_T2
    old = _load_lines(p2)
    return new_lines != old


def proc_f(lines: list[str], *, path_t2: Path | None = None) -> None:
    _save_lines(path_t2 or PATH_T2, lines)


def _get_json(client: httpx.Client, url: str, params: dict[str, str]) -> Any:
    last_err: Exception | None = None
    for i in range(_MAX_TRY):
        try:
            r = client.get(url, params=params, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_err = exc
            if i + 1 < _MAX_TRY:
                import time

                time.sleep(_WAIT_S)
    assert last_err is not None
    raise last_err


def fetch_a(
    client: httpx.Client,
    base: str,
    tenant: str,
    base_date: date,
    fid: int,
    sid: int,
) -> list[dict[str, Any]]:
    fac = json.dumps([{"facilityId": fid, "spaceIds": [sid]}], separators=(",", ":"))
    url = f"{base.rstrip('/')}/api/{tenant}/public/facility/calendar"
    payload = _get_json(
        client,
        url,
        {"baseDate": base_date.isoformat(), "facilities": fac},
    )
    data = payload.get("data") or {}
    cal = data.get("calendar") or []
    return list(cal)


def fetch_b(
    client: httpx.Client,
    base: str,
    tenant: str,
    fid: int,
    sid: int,
    days: list[str],
) -> list[dict[str, Any]]:
    if not days:
        return []
    fac = json.dumps(
        [{"facilityId": fid, "spaces": [{"spaceId": sid, "selectedDates": days}]}],
        separators=(",", ":"),
    )
    search = json.dumps(
        {
            "purpose": [],
            "city": "",
            "facilities": [],
            "facilityAreaId": [],
            "useDate": "",
            "timeSlots": [],
            "keyword": "",
            "categoryId": None,
        },
        separators=(",", ":"),
    )
    url = f"{base.rstrip('/')}/api/{tenant}/public/facility/spaceTime"
    payload = _get_json(
        client,
        url,
        {"facilities": fac, "searchData": search},
    )
    data = payload.get("data") or []
    if not data:
        return []
    spaces = (data[0] or {}).get("spaces") or []
    if not spaces:
        return []
    return list((spaces[0] or {}).get("timeTable") or [])


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def collect_one(
    client: httpx.Client,
    base: str,
    tenant: str,
    item: dict[str, Any],
    start: date,
    end: date,
) -> set[str]:
    code = str(item["code"])
    fid = int(item["fid"])
    sid = int(item["sid"])
    keys: set[str] = set()
    ok_days: list[str] = []

    for month_start in helper_6(start, end):
        try:
            rows = fetch_a(client, base, tenant, month_start, fid, sid)
        except Exception:
            logger.exception("fetch_a failed code=%s month=%s", code, month_start)
            continue
        for row in rows:
            try:
                d = _parse_day(str(row.get("date", "")))
            except ValueError:
                continue
            if d < start or d > end:
                continue
            if row.get("isPast"):
                continue
            spaces = row.get("spaces") or []
            hit = False
            for sp in spaces:
                if int(sp.get("spaceId", -1)) == sid and sp.get("status") == _ST_OK:
                    hit = True
                    break
            if hit:
                ok_days.append(d.isoformat())

    ok_days = sorted(set(ok_days))
    for ds in ok_days:
        d = _parse_day(ds)
        try:
            table = fetch_b(client, base, tenant, fid, sid, [ds])
        except Exception:
            logger.exception("fetch_b failed code=%s day=%s", code, ds)
            continue
        for cell in table:
            tstr = str(cell.get("timeString", "")).strip()
            if not tstr:
                continue
            details = cell.get("details") or []
            if not details:
                continue
            if (details[0] or {}).get("status") != _ST_OK:
                continue
            if not helper_5(d, tstr):
                continue
            keys.add(f"{code}|{ds} {tstr}")
    return keys


def collect_all(
    client: httpx.Client,
    base: str,
    tenant: str,
    items: list[dict[str, Any]],
    start: date,
    end: date,
) -> set[str]:
    out: set[str] = set()
    for item in items:
        out |= collect_one(client, base, tenant, item, start, end)
    return out


def run_task(
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
    items: list[dict[str, Any]] | None = None,
    base: str | None = None,
    tenant: str | None = None,
    path_t1: Path | None = None,
    path_t2: Path | None = None,
    send: bool = True,
) -> dict[str, Any]:
    from core.notifier import send_msg

    cur = now or datetime.now()
    day0 = cur.date()
    end = helper_4(cur)
    base_url = (base if base is not None else os.getenv("CFG_B1", "")).strip()
    ten = (tenant if tenant is not None else os.getenv("CFG_B2", "")).strip()
    cfg_items = items if items is not None else load_cfg()

    own_client = client is None
    http = client or httpx.Client()
    try:
        if not base_url or not ten:
            logger.error("CFG_B1 / CFG_B2 missing")
            keys: set[str] = set()
        else:
            keys = collect_all(http, base_url, ten, cfg_items, day0, end)
    finally:
        if own_client:
            http.close()

    proc_b(keys, cur, path_t1=path_t1)
    ready = proc_c(keys, cur, path_t1=path_t1)
    lines = sorted(ready)
    changed = proc_e(lines, path_t2=path_t2)
    mailed = False
    if changed and send:
        mailed = send_msg(lines, bool(lines), when=day0)
        if mailed:
            proc_f(lines, path_t2=path_t2)
    elif changed and not send:
        proc_f(lines, path_t2=path_t2)

    return {
        "keys": keys,
        "ready": ready,
        "lines": lines,
        "changed": changed,
        "mailed": mailed,
        "end": end.isoformat(),
    }
