"""Scan lottery waiting counts for month+2 (no login)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from core.scanner import DATA_DIR, ROOT, TZ_TOKYO, _UA, _WAIT_S, helper_3, now_tokyo

logger = logging.getLogger(__name__)

PATH_VENUES = ROOT / "config" / "venues.json"
PATH_LOTTERY_PREV = DATA_DIR / "lottery_prev.json"
_CONCURRENCY = 5
_PACING_S = 0.15
_MAX_TRY = 3
# Weekday (non-holiday): only these start hours (2h slots 17-19 / 19-21)
_WEEKDAY_START_HOURS = frozenset({17, 19})


@dataclass(frozen=True)
class LotteryEntry:
    code: str
    day: date
    time_string: str
    face: str  # A/B/C... by areaId order among shared faces
    count: int


def load_venues(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load config/venues.json (no booking/playwright dependency)."""
    p = path or PATH_VENUES
    with p.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("venues.json must be an object keyed by venue_code")
    out: dict[str, dict[str, Any]] = {}
    for code, val in raw.items():
        if not isinstance(val, dict):
            continue
        out[str(code)] = {
            "fid": int(val["fid"]),
            "sid": int(val["sid"]),
            "type": str(val.get("type") or val.get("venue_type") or "").strip(),
        }
    return out


def target_month(when: date | datetime | None = None) -> tuple[int, int]:
    """Return (year, month) for 下下个月 (Tokyo month + 2)."""
    if when is None:
        cur = now_tokyo()
        day = cur.date()
    elif isinstance(when, datetime):
        if when.tzinfo is not None:
            day = when.astimezone(TZ_TOKYO).date()
        else:
            day = when.date()
    else:
        day = when
    y, m = day.year, day.month + 2
    while m > 12:
        y += 1
        m -= 12
    return y, m


def month_first(year: int, month: int) -> date:
    return date(year, month, 1)


def days_from_calendar(cal_rows: list[dict[str, Any]]) -> list[date]:
    """Keep isCurrentMonth days only; ignore calendar status fields."""
    out: list[date] = []
    for row in cal_rows:
        if not isinstance(row, dict):
            continue
        if not row.get("isCurrentMonth"):
            continue
        raw = str(row.get("date", ""))[:10]
        try:
            out.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return sorted(set(out))


def _area_id_sort_key(aid: Any) -> tuple[int, int | str]:
    try:
        return (0, int(aid))
    except (TypeError, ValueError):
        return (1, str(aid))


def shared_face_map(
    areas_raw: list[Any],
    venue_type: str = "",  # unused; kept for call-site compatibility
) -> dict[Any, str]:
    """Map areaId → A/B/C... Exclude blocks.length > 1; sort by areaId.

    Does not use areaName (names are inconsistent / duplicated across faces).
    """
    del venue_type  # display letters are areaId-ordered, type-agnostic
    shared: list[dict[str, Any]] = []
    for a in areas_raw:
        if not isinstance(a, dict):
            continue
        aid = a.get("areaId")
        if aid is None:
            continue
        blocks = a.get("blocks")
        if not isinstance(blocks, list):
            blocks = []
        if len(blocks) != 1:
            continue
        shared.append(a)

    shared.sort(key=lambda a: _area_id_sort_key(a.get("areaId")))
    out: dict[Any, str] = {}
    for i, a in enumerate(shared):
        aid = a.get("areaId")
        letter = chr(ord("A") + i) if i < 26 else f"F{i + 1}"
        out[aid] = letter
        out[str(aid)] = letter
    return out


def _as_nonneg_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    return n


def _start_hour(time_string: str) -> int | None:
    raw = str(time_string or "").strip()
    start = raw.split("-", 1)[0].split("～", 1)[0].split("~", 1)[0].strip()
    if ":" in start:
        start = start.split(":", 1)[0]
    try:
        return int(start)
    except ValueError:
        return None


def keep_slot(day: date, time_string: str) -> bool:
    """Weekend/holiday: all slots; weekday: only 17:00 and 19:00 starts."""
    if helper_3(day):
        return True
    h = _start_hour(time_string)
    return h in _WEEKDAY_START_HOURS


def extract_entries(
    code: str,
    day: date,
    areas_raw: list[Any],
    table: list[dict[str, Any]],
    venue_type: str,
) -> list[LotteryEntry]:
    """Pull lotteryWaitingCount for shared faces with status==available only.

    When unauthenticated, available ≡ drawable lottery slot; unavailable is
    truly closed. Other labels (e.g. lottery) do not appear without login.
    """
    faces = shared_face_map(areas_raw, venue_type)
    if not faces:
        return []
    rows: list[LotteryEntry] = []
    for cell in table:
        if not isinstance(cell, dict):
            continue
        tstr = str(cell.get("timeString", "")).strip()
        if not tstr:
            continue
        if not keep_slot(day, tstr):
            continue
        details = cell.get("details") or []
        if not isinstance(details, list):
            continue
        for det in details:
            if not isinstance(det, dict):
                continue
            if str(det.get("status") or "").strip() != "available":
                continue
            aid = det.get("areaId")
            face = faces.get(aid)
            if face is None and aid is not None:
                face = faces.get(str(aid))
            if face is None:
                continue
            n = _as_nonneg_int(det.get("lotteryWaitingCount"))
            if n is None:
                continue
            rows.append(
                LotteryEntry(
                    code=code,
                    day=day,
                    time_string=tstr,
                    face=face,
                    count=n,
                )
            )
    return rows


def _parse_space_payload(
    payload: Any,
) -> tuple[list[Any], list[dict[str, Any]]]:
    data = (payload or {}).get("data") or []
    if not data:
        return [], []
    row0 = data[0] or {}
    spaces = row0.get("spaces") or []
    if not spaces:
        return [], []
    space0 = spaces[0] or {}
    areas_raw = space0.get("areas") or row0.get("areas") or []
    table = list(space0.get("timeTable") or [])
    return list(areas_raw), table


async def _aget_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> Any:
    last_err: Exception | None = None
    for i in range(_MAX_TRY):
        try:
            r = await client.get(url, params=params, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last_err = exc
            if i + 1 < _MAX_TRY:
                await asyncio.sleep(_WAIT_S)
    assert last_err is not None
    raise last_err


async def fetch_calendar_days(
    client: httpx.AsyncClient,
    base: str,
    tenant: str,
    base_date: date,
    fid: int,
    sid: int,
) -> list[date]:
    fac = json.dumps([{"facilityId": fid, "spaceIds": [sid]}], separators=(",", ":"))
    url = f"{base.rstrip('/')}/api/{tenant}/public/facility/calendar"
    payload = await _aget_json(
        client,
        url,
        {"baseDate": base_date.isoformat(), "facilities": fac},
    )
    data = payload.get("data") or {}
    cal = data.get("calendar") or []
    return days_from_calendar(list(cal))


async def fetch_space_day(
    client: httpx.AsyncClient,
    base: str,
    tenant: str,
    fid: int,
    sid: int,
    day: date,
) -> tuple[list[Any], list[dict[str, Any]]]:
    ds = day.isoformat()
    fac = json.dumps(
        [{"facilityId": fid, "spaces": [{"spaceId": sid, "selectedDates": [ds]}]}],
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
    payload = await _aget_json(
        client,
        url,
        {"facilities": fac, "searchData": search},
    )
    return _parse_space_payload(payload)


async def collect_venue(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    base: str,
    tenant: str,
    code: str,
    meta: dict[str, Any],
    year: int,
    month: int,
) -> list[LotteryEntry]:
    fid = int(meta["fid"])
    sid = int(meta["sid"])
    vtype = str(meta.get("type") or "")
    base_date = month_first(year, month)

    async with sem:
        await asyncio.sleep(_PACING_S)
        try:
            days = await fetch_calendar_days(
                client, base, tenant, base_date, fid, sid
            )
        except Exception:
            logger.exception("lottery calendar failed code=%s month=%s-%02d", code, year, month)
            return []

    out: list[LotteryEntry] = []

    async def one_day(d: date) -> list[LotteryEntry]:
        async with sem:
            await asyncio.sleep(_PACING_S)
            try:
                areas, table = await fetch_space_day(
                    client, base, tenant, fid, sid, d
                )
            except Exception:
                logger.exception("lottery spaceTime failed code=%s day=%s", code, d)
                return []
            return extract_entries(code, d, areas, table, vtype)

    if days:
        parts = await asyncio.gather(*(one_day(d) for d in days))
        for part in parts:
            out.extend(part)
    return out


async def collect_all_async(
    base: str,
    tenant: str,
    venues: dict[str, dict[str, Any]],
    year: int,
    month: int,
    *,
    concurrency: int = _CONCURRENCY,
) -> list[LotteryEntry]:
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {"User-Agent": _UA}
    async with httpx.AsyncClient(headers=headers) as client:
        tasks = [
            collect_venue(client, sem, base, tenant, code, meta, year, month)
            for code, meta in venues.items()
        ]
        parts = await asyncio.gather(*tasks)
    rows: list[LotteryEntry] = []
    for part in parts:
        rows.extend(part)
    return rows


def collect_all(
    base: str,
    tenant: str,
    venues: dict[str, dict[str, Any]] | None = None,
    *,
    year: int | None = None,
    month: int | None = None,
    when: date | datetime | None = None,
    concurrency: int = _CONCURRENCY,
) -> tuple[int, int, list[LotteryEntry]]:
    """Sync wrapper. Returns (year, month, entries)."""
    if year is None or month is None:
        year, month = target_month(when)
    vmap = venues if venues is not None else load_venues(PATH_VENUES)
    rows = asyncio.run(
        collect_all_async(
            base,
            tenant,
            vmap,
            year,
            month,
            concurrency=concurrency,
        )
    )
    return year, month, rows


def entry_snapshot_key(code: str, day: date, time_string: str, face: str) -> str | None:
    """Stable key: venue|YYYY-MM-DD|HH|face."""
    h = _start_hour(time_string)
    if h is None:
        return None
    return f"{code}|{day.isoformat()}|{h:02d}|{face}"


def entries_to_snapshot(entries: list[LotteryEntry]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        k = entry_snapshot_key(e.code, e.day, e.time_string, e.face)
        if k is None:
            continue
        out[k] = int(e.count)
    return out


def load_lottery_prev(path: Path | None = None) -> dict[str, int]:
    """Load previous snapshot. Missing/empty → {}; corrupt non-empty raises."""
    p = path or PATH_LOTTERY_PREV
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def save_lottery_prev(
    snapshot: dict[str, int],
    path: Path | None = None,
) -> None:
    p = path or PATH_LOTTERY_PREV
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2, sort_keys=True)


def lottery_changed(
    current: dict[str, int],
    previous: dict[str, int],
) -> bool:
    """True if any venue|day|hour|face count differs (incl. new/removed keys)."""
    return current != previous


def changed_day_keys(
    current: dict[str, int],
    previous: dict[str, int],
) -> set[str]:
    """Return ``code|YYYY-MM-DD`` for days that have any slot/face change."""
    out: set[str] = set()
    for k in set(current) | set(previous):
        if current.get(k) != previous.get(k):
            parts = str(k).split("|")
            if len(parts) >= 2:
                out.add(f"{parts[0]}|{parts[1]}")
    return out
