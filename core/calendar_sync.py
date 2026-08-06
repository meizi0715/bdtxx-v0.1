"""Sync reservation history from the facility site into Google Calendar."""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

from core.booking import (
    ROOT,
    _UA,
    _base_url,
    _goto_home,
    _tenant,
    login_as_group,
    resolve_group_ref,
)
from core.calendar_read import SCOPES, _list_range
from core.scanner import DATA_DIR, helper_1, today_tokyo

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Tokyo")
PATH_SYNC_STATE = DATA_DIR / "sync_calendar_state.json"
PATH_VENUE_SHORT_NAMES = ROOT / "local" / "venue_short_names.json"

# 已按 receptionDate 的「日=16」规则区分抽選/予約并分流写入 CFG_C3/CFG_C2；
# 待本月16号抽选结果验证准确性（16号先到先得予約可能被误判为抽選）。

_COLOR_REGULAR = "10"  # green — 予約 (CFG_C2)
_COLOR_LOTTERY = "6"  # orange — 抽選 (CFG_C3)
_CIRCLED_HOURS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"
# Whitelist: only these reservation statuses sync to Calendar.
_SYNC_STATUSES = frozenset({1, 6})  # 1=仮予約, 6=予約確定


def _empty_state(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "done": [],
        "attempts": 0,
        "summary_sent": False,
    }


def load_sync_state(
    path: Path | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Load today's sync state; reset when missing/corrupt/stale date."""
    day = today or today_tokyo()
    empty = _empty_state(day)
    p = path or PATH_SYNC_STATE
    if not p.exists():
        return empty
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return empty
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("sync state corrupt (reset): %s", p)
        return empty
    if not isinstance(raw, dict):
        return empty
    if str(raw.get("date") or "") != day.isoformat():
        return empty
    done_raw = raw.get("done") or []
    done = [str(x) for x in done_raw] if isinstance(done_raw, list) else []
    try:
        attempts = int(raw.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "date": day.isoformat(),
        "done": done,
        "attempts": max(0, attempts),
        "summary_sent": bool(raw.get("summary_sent")),
    }


def save_sync_state(
    state: dict[str, Any],
    path: Path | None = None,
) -> None:
    p = path or PATH_SYNC_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": str(state.get("date") or ""),
        "done": list(state.get("done") or []),
        "attempts": int(state.get("attempts") or 0),
        "summary_sent": bool(state.get("summary_sent")),
    }
    with p.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)


def sync_window(today: date | None = None) -> tuple[date, date]:
    """Inclusive [month start, end of next month]."""
    day = today or today_tokyo()
    return date(day.year, day.month, 1), helper_1(day)


def day_work_finished(state: dict[str, Any], total: int) -> bool:
    done_n = len(list(state.get("done") or []))
    attempts = int(state.get("attempts") or 0)
    return done_n >= total or attempts >= total + 2


def build_summary_mail(done_n: int, total: int, *, when: date | None = None) -> tuple[str, str]:
    day = when or today_tokyo()
    subject = f"予約履歴カレンダー同期完了({day.month}/{day.day})"
    fail_n = max(0, total - done_n)
    body = (
        "本日の同期結果\n"
        f"成功: {done_n}件 / 全{total}件\n"
        f"失敗: {fail_n}件\n"
    )
    return subject, body


def _calendars_cfg(
    cred_path: str | None = None,
    cal_regular: str | None = None,
    cal_lottery: str | None = None,
) -> tuple[str, str, str]:
    """Return (service-account path, CFG_C2 予約 id, CFG_C3 抽選 id)."""
    path = (cred_path if cred_path is not None else os.getenv("CFG_C1", "")).strip()
    cid_reg = (
        cal_regular if cal_regular is not None else os.getenv("CFG_C2", "")
    ).strip()
    cid_lot = (
        cal_lottery if cal_lottery is not None else os.getenv("CFG_C3", "")
    ).strip()
    if not path or not cid_reg or not cid_lot:
        raise RuntimeError("CFG_C1 / CFG_C2 / CFG_C3 missing")
    return path, cid_reg, cid_lot


def _cal_svc(cred_path: str) -> Any:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        cred_path,
        scopes=list(SCOPES),
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _range_bounds(start: date, end: date) -> tuple[str, str]:
    tmin = datetime.combine(start, time.min, tzinfo=_TZ)
    tmax = datetime.combine(end + timedelta(days=1), time.min, tzinfo=_TZ)
    return tmin.isoformat(), tmax.isoformat()


def _parse_hhmm(raw: str) -> time | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (raw or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return time(h, mi)


def parse_reserve_window(
    reserve_date: str,
    reserve_time: str,
) -> tuple[datetime, datetime] | None:
    """Parse reserveDate + reserveTime → aware Tokyo start/end."""
    try:
        day = date.fromisoformat(str(reserve_date).strip()[:10])
    except ValueError:
        return None
    parts = re.split(r"\s*[-～~]\s*", str(reserve_time or "").strip())
    if len(parts) != 2:
        return None
    t0 = _parse_hhmm(parts[0])
    t1 = _parse_hhmm(parts[1])
    if t0 is None or t1 is None:
        return None
    start = datetime.combine(day, t0, tzinfo=_TZ)
    end = datetime.combine(day, t1, tzinfo=_TZ)
    if end <= start:
        return None
    return start, end


def _norm_facility_name(name: str) -> str:
    """Strip and NFKC-normalize for map lookup (full/half-width, etc.)."""
    return unicodedata.normalize("NFKC", str(name or "")).strip()


def _facility_name(row: dict[str, Any]) -> str:
    """Facility display name from list/detail row (API uses ``facilityName``)."""
    for key in (
        "facilityName",
        "facilitiesName",
        "facility_name",
        "facilities_name",
    ):
        val = str(row.get(key) or "").strip()
        if val:
            return val
    return ""


def load_venue_short_names(path: Path | None = None) -> dict[str, str]:
    """Load facilityName → short label from local/venue_short_names.json."""
    p = path or PATH_VENUE_SHORT_NAMES
    if not p.exists():
        logger.warning(
            "venue short names file not found: %s (titles will use full facility names)",
            p,
        )
        return {}
    try:
        with p.open(encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("venue short names load failed: %s", p)
        return {}
    if not isinstance(raw, dict):
        logger.warning("venue short names invalid (not an object): %s", p)
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key = _norm_facility_name(str(k))
        if not key:
            continue
        out[key] = str(v).strip()
    if not out:
        logger.warning("venue short names empty after load: %s", p)
    return out


def venue_short_label(
    facilities_name: str,
    short_map: dict[str, str] | None = None,
) -> str:
    """Return mapped short name, or full facility name when missing."""
    name = str(facilities_name or "").strip()
    if not name:
        return ""
    mapping = short_map if short_map is not None else load_venue_short_names()
    key = _norm_facility_name(name)
    short = mapping.get(key)
    if short:
        return short
    if mapping:
        logger.debug("venue short name miss key=%r file=%s", name, PATH_VENUE_SHORT_NAMES)
    return name


def group_name_initial(group_name: str) -> str:
    """First character of display name (groups.json ``name``)."""
    n = str(group_name or "").strip()
    return n[0] if n else "?"


def duration_hours(start_dt: datetime, end_dt: datetime) -> int:
    secs = int((end_dt - start_dt).total_seconds())
    if secs <= 0:
        return 0
    return secs // 3600


def format_duration_mark(hours: int) -> str:
    if hours <= 0:
        return ""
    if hours <= len(_CIRCLED_HOURS):
        return _CIRCLED_HOURS[hours - 1]
    return f"({hours})"


def build_event_summary(
    facilities_name: str,
    group_name: str,
    hours: int,
    *,
    short_map: dict[str, str] | None = None,
) -> str:
    short = venue_short_label(facilities_name, short_map)
    initial = group_name_initial(group_name)
    if hours <= 2:
        return f"{short}/{initial}"
    mark = format_duration_mark(hours)
    return f"{short}/{initial} {mark}"


def row_time_bounds(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Parse row start/end from startTime+endTime or reserveTime."""
    try:
        day = date.fromisoformat(str(row.get("reserveDate") or "")[:10])
    except ValueError:
        return None
    st = str(row.get("startTime") or "").strip()
    et = str(row.get("endTime") or "").strip()
    if st and et:
        t0 = _parse_hhmm(st)
        t1 = _parse_hhmm(et)
        if t0 is not None and t1 is not None:
            start = datetime.combine(day, t0, tzinfo=_TZ)
            end = datetime.combine(day, t1, tzinfo=_TZ)
            if end > start:
                return start, end
    return parse_reserve_window(
        str(row.get("reserveDate") or ""),
        str(row.get("reserveTime") or ""),
    )


def _merge_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("reserveDate") or "")[:10],
        str(row.get("facilityId") or ""),
        str(row.get("areaId") or ""),
        str(row.get("inferred_type") or "regular"),
    )


def merge_consecutive_reservations(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge adjacent slots (same date/facility/area/type, end==next start)."""
    items: list[dict[str, Any]] = []
    for row in rows:
        rid = _reservation_id(row)
        bounds = row_time_bounds(row)
        if not rid or bounds is None:
            logger.warning(
                "skip unmergeable reservation id=%s date=%s time=%s",
                rid,
                row.get("reserveDate"),
                row.get("reserveTime") or row.get("startTime"),
            )
            continue
        start_dt, end_dt = bounds
        items.append(
            {
                "row": row,
                "rid": rid,
                "key": _merge_key(row),
                "start": start_dt,
                "end": end_dt,
            }
        )
    items.sort(key=lambda x: (x["key"], x["start"]))

    merged: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for it in items:
        if cur is None:
            cur = {
                "key": it["key"],
                "start": it["start"],
                "end": it["end"],
                "rows": [it["row"]],
                "ids": [it["rid"]],
            }
            continue
        if it["key"] == cur["key"] and it["start"] == cur["end"]:
            cur["end"] = it["end"]
            cur["rows"].append(it["row"])
            cur["ids"].append(it["rid"])
            continue
        merged.append(cur)
        cur = {
            "key": it["key"],
            "start": it["start"],
            "end": it["end"],
            "rows": [it["row"]],
            "ids": [it["rid"]],
        }
    if cur is not None:
        merged.append(cur)

    out: list[dict[str, Any]] = []
    for block in merged:
        first = block["rows"][0]
        out.append(
            {
                "start_dt": block["start"],
                "end_dt": block["end"],
                "reservation_ids": list(block["ids"]),
                "inferred_type": str(first.get("inferred_type") or "regular"),
                "receptionDate": str(first.get("receptionDate") or ""),
                "facilitiesName": _facility_name(first),
                "facilityId": str(first.get("facilityId") or ""),
                "status": str(first.get("status", "")),
                "screeningResult": str(first.get("screeningResult", "")),
                "source_rows": block["rows"],
            }
        )
    return out


def filter_reservations(
    rows: list[dict[str, Any]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("reserveDate") or "")[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        if start <= d <= end:
            out.append(row)
    return out


def _parse_reservation_status(row: dict[str, Any]) -> int | None:
    raw = row.get("status")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def filter_reservations_by_status(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only status 1 (仮予約) or 6 (予約確定); all others excluded."""
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _parse_reservation_status(row) in _SYNC_STATUSES:
            out.append(row)
    return out


async def read_auth_user(page: Page) -> dict[str, Any]:
    """Parse userOrganizationId / userType from localStorage persist:root."""
    user = await page.evaluate(
        """() => {
          const raw = localStorage.getItem('persist:root');
          if (!raw) return null;
          let root;
          try { root = JSON.parse(raw); } catch (e) { return null; }
          const authRaw = root && root.auth;
          if (authRaw == null) return null;
          let auth;
          try {
            auth = typeof authRaw === 'string' ? JSON.parse(authRaw) : authRaw;
          } catch (e) { return null; }
          const u = auth && auth.user;
          if (!u || typeof u !== 'object') return null;
          return {
            userOrganizationId: u.userOrganizationId,
            userType: u.userType,
          };
        }"""
    )
    if not isinstance(user, dict):
        raise RuntimeError("persist:root auth.user missing after login")
    oid = user.get("userOrganizationId")
    utype = user.get("userType")
    if oid is None or utype is None:
        raise RuntimeError("userOrganizationId / userType missing in auth.user")
    return {"userOrganizationId": oid, "userType": utype}


async def fetch_reservation_list(
    page: Page,
    user_organization_id: Any,
    user_type: Any,
) -> list[dict[str, Any]]:
    base = _base_url()
    tenant = _tenant() or "kawaguchi-city"
    if not base:
        raise RuntimeError("CFG_B1 missing")
    url = (
        f"{base.rstrip('/')}/api/{tenant}/public/reservation"
        f"?userOrganizationId={user_organization_id}&userType={user_type}"
    )
    resp = await page.request.get(url, timeout=60000)
    if not resp.ok:
        raise RuntimeError(
            f"reservation API HTTP {resp.status}: {url}"
        )
    payload = await resp.json()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("reservationList"), list):
            rows = data["reservationList"]
        elif isinstance(payload.get("reservationList"), list):
            rows = payload["reservationList"]
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def parse_reception_day(reception_date: str) -> int | None:
    """Parse day-of-month from e.g. ``2026年06月16日（火）`` → ``16``."""
    raw = str(reception_date or "").strip()
    m = re.search(r"(\d{1,2})日", raw)
    if m:
        return int(m.group(1))
    try:
        return date.fromisoformat(raw[:10]).day
    except ValueError:
        return None


def infer_reservation_type(reception_date: str) -> str:
    """Return ``lottery`` when reception day is 16, else ``regular``."""
    day = parse_reception_day(reception_date)
    if day == 16:
        return "lottery"
    return "regular"


async def fetch_reservation_detail(
    page: Page,
    reservation_id: str,
) -> dict[str, Any]:
    base = _base_url()
    tenant = _tenant() or "kawaguchi-city"
    if not base:
        raise RuntimeError("CFG_B1 missing")
    url = f"{base.rstrip('/')}/api/{tenant}/public/reservation/{reservation_id}"
    resp = await page.request.get(url, timeout=60000)
    if not resp.ok:
        raise RuntimeError(
            f"reservation detail HTTP {resp.status}: id={reservation_id}"
        )
    payload = await resp.json()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    raise RuntimeError(f"reservation detail invalid: id={reservation_id}")


async def enrich_reservations_with_details(
    page: Page,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fetch detail per row; attach receptionDate and inferred_type."""
    out: list[dict[str, Any]] = []
    for row in rows:
        rid = _reservation_id(row)
        if not rid:
            logger.warning("skip reservation without id: %s", row)
            continue
        detail = await fetch_reservation_detail(page, rid)
        reception = str(detail.get("receptionDate") or "")
        merged = dict(row)
        merged["receptionDate"] = reception
        merged["inferred_type"] = infer_reservation_type(reception)
        out.append(merged)
    return out


def _reservation_id(row: dict[str, Any]) -> str | None:
    for key in ("id", "reservationId", "reservation_id"):
        if row.get(key) is not None and str(row.get(key)).strip() != "":
            return str(row.get(key)).strip()
    return None


def _delete_group_synced_events(
    client: Any,
    cal_id: str,
    group_name: str,
    tmin: str,
    tmax: str,
) -> int:
    deleted = 0
    for ev in _list_range(client, cal_id, tmin, tmax):
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        if not isinstance(priv, dict):
            continue
        if str(priv.get("group_name") or "") != group_name:
            continue
        if "reservation_id" not in priv and "reservation_ids" not in priv:
            continue
        eid = ev.get("id")
        if not eid:
            continue
        client.events().delete(calendarId=cal_id, eventId=eid).execute()
        deleted += 1
    return deleted


def replace_group_events(
    rows: list[dict[str, Any]],
    *,
    group_name: str,
    start: date,
    end: date,
    cred_path: str | None = None,
    cal_regular: str | None = None,
    cal_lottery: str | None = None,
    svc: Any | None = None,
    short_map: dict[str, str] | None = None,
) -> int:
    """Delete prior synced events for group in both calendars, then insert rows.

    Rows must include ``receptionDate`` and ``inferred_type`` (from detail API).
    Consecutive slots are merged before insert. Returns events inserted.
    """
    path, cid_reg, cid_lot = _calendars_cfg(cred_path, cal_regular, cal_lottery)
    client = svc if svc is not None else _cal_svc(path)
    tmin, tmax = _range_bounds(start, end)
    deleted = 0
    for cid in (cid_reg, cid_lot):
        n = _delete_group_synced_events(client, cid, group_name, tmin, tmax)
        deleted += n
    if deleted:
        logger.info(
            "calendar sync deleted old events group=%s n=%s (both calendars)",
            group_name,
            deleted,
        )

    labels = short_map if short_map is not None else load_venue_short_names()
    logger.info(
        "venue short names: %s key(s) from %s",
        len(labels),
        PATH_VENUE_SHORT_NAMES,
    )
    events = merge_consecutive_reservations(rows)
    inserted = 0
    for ev in events:
        start_dt = ev["start_dt"]
        end_dt = ev["end_dt"]
        ids = [str(x) for x in ev.get("reservation_ids") or []]
        if not ids:
            continue
        hours = duration_hours(start_dt, end_dt)
        inferred = str(ev.get("inferred_type") or "regular")
        reception = str(ev.get("receptionDate") or "")
        fac = str(ev.get("facilitiesName") or "").strip()
        fid = str(ev.get("facilityId") or "").strip()
        if inferred == "lottery":
            cal_id = cid_lot
            color_id = _COLOR_LOTTERY
        else:
            cal_id = cid_reg
            color_id = _COLOR_REGULAR
        summary = build_event_summary(fac, group_name, hours, short_map=labels)
        ids_csv = ",".join(ids)
        body = {
            "summary": summary,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Tokyo"},
            "colorId": color_id,
            "extendedProperties": {
                "private": {
                    "reservation_id": ids[0],
                    "reservation_ids": ids_csv,
                    "group_name": group_name,
                    "facilityId": fid,
                    "status": str(ev.get("status", "")),
                    "screening_result": str(ev.get("screeningResult", "")),
                    "reception_date": reception,
                    "inferred_type": inferred,
                }
            },
        }
        client.events().insert(calendarId=cal_id, body=body).execute()
        inserted += 1
        if len(ids) > 1:
            logger.info(
                "merged event summary=%s ids=%s span=%s..%s",
                summary,
                ids_csv,
                start_dt.strftime("%H:%M"),
                end_dt.strftime("%H:%M"),
            )
    return inserted


async def sync_one_group_async(
    group_ref: str,
    *,
    headless: bool = True,
    today: date | None = None,
) -> None:
    """Login as group, fetch reservations + details, replace Calendar events."""
    login_id, group_name, password = resolve_group_ref(group_ref)
    day = today or today_tokyo()
    start, end = sync_window(day)
    base = _base_url()
    if not base or not _tenant():
        raise RuntimeError("CFG_B1 / CFG_B2 missing")
    _calendars_cfg()  # fail fast before browser

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                locale="ja-JP",
                user_agent=_UA,
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            await _goto_home(page)
            await login_as_group(
                page,
                login_id,
                password,
                group_name=group_name,
                wait_for_nine=False,
            )
            auth = await read_auth_user(page)
            rows = await fetch_reservation_list(
                page,
                auth["userOrganizationId"],
                auth["userType"],
            )
            filtered = filter_reservations(rows, start, end)
            filtered = filter_reservations_by_status(filtered)
            enriched = await enrich_reservations_with_details(page, filtered)
            merged_preview = merge_consecutive_reservations(enriched)
            lottery_n = sum(
                1 for r in enriched if r.get("inferred_type") == "lottery"
            )
            logger.info(
                "group=%s reservations total=%s after_filters=%s events=%s lottery=%s window=%s..%s",
                group_ref,
                len(rows),
                len(enriched),
                len(merged_preview),
                lottery_n,
                start,
                end,
            )
            n = replace_group_events(
                enriched,
                group_name=group_name,
                start=start,
                end=end,
            )
            logger.info(
                "group=%s calendar inserted=%s name=%s",
                group_ref,
                n,
                group_name,
            )
        finally:
            await browser.close()


def sync_one_group(
    group_ref: str,
    *,
    headless: bool = True,
    today: date | None = None,
) -> None:
    import asyncio

    asyncio.run(
        sync_one_group_async(group_ref, headless=headless, today=today)
    )
