"""Monthly reservation stats events on CFG_C4 (from CFG_C2/C3 data)."""

from __future__ import annotations

import calendar
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from core.booking import PATH_GROUPS, load_groups
from core.calendar_read import _list_month, _list_range
from core.calendar_sync import _cal_svc, group_name_initial
from core.lottery import load_venues
from core.scanner import today_tokyo

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Tokyo")
_STATS_KIND = "monthly_reservation_stats"


def _stats_calendars_cfg(
    cred_path: str | None = None,
    cal_regular: str | None = None,
    cal_lottery: str | None = None,
    cal_stats: str | None = None,
) -> tuple[str, str, str, str]:
    path = (cred_path if cred_path is not None else os.getenv("CFG_C1", "")).strip()
    cid_reg = (
        cal_regular if cal_regular is not None else os.getenv("CFG_C2", "")
    ).strip()
    cid_lot = (
        cal_lottery if cal_lottery is not None else os.getenv("CFG_C3", "")
    ).strip()
    cid_stats = (
        cal_stats if cal_stats is not None else os.getenv("CFG_C4", "")
    ).strip()
    if not path or not cid_reg or not cid_lot or not cid_stats:
        raise RuntimeError("CFG_C1 / CFG_C2 / CFG_C3 / CFG_C4 missing")
    return path, cid_reg, cid_lot, cid_stats


def stats_target_months(today: date | None = None) -> list[tuple[int, int]]:
    """Current month and next month (same coverage as reservation sync window)."""
    day = today or today_tokyo()
    y, m = day.year, day.month
    if m == 12:
        return [(y, 12), (y + 1, 1)]
    return [(y, m), (y, m + 1)]


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def stats_event_title(month: int) -> str:
    return f"{month}月度予約統計"


def _fid_to_venue_type() -> dict[int, str]:
    venues = load_venues()
    out: dict[int, str] = {}
    for meta in venues.values():
        fid = int(meta["fid"])
        vtype = str(meta.get("type") or "").strip()
        if vtype in ("gym", "hall"):
            out[fid] = vtype
    return out


def _is_reservation_event(ev: dict[str, Any]) -> bool:
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    if not isinstance(priv, dict):
        return False
    if priv.get("stats_kind") == _STATS_KIND:
        return False
    return bool(priv.get("reservation_id") or priv.get("reservation_ids"))


def _count_events_by_group_and_venue_type(
    events: list[dict[str, Any]],
    fid_type: dict[int, str],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Return (gym_counts, hall_counts, skip_reasons)."""
    gym: dict[str, int] = defaultdict(int)
    hall: dict[str, int] = defaultdict(int)
    skips: dict[str, int] = defaultdict(int)
    for ev in events:
        if not _is_reservation_event(ev):
            skips["not_reservation"] += 1
            continue
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        gname = str(priv.get("group_name") or "").strip()
        if not gname:
            skips["no_group_name"] += 1
            continue
        raw_fid = priv.get("facilityId")
        if raw_fid is None or str(raw_fid).strip() == "":
            skips["no_facility_id"] += 1
            continue
        try:
            fid = int(raw_fid)
        except (TypeError, ValueError):
            skips["bad_facility_id"] += 1
            continue
        vtype = fid_type.get(fid)
        if vtype == "gym":
            gym[gname] += 1
        elif vtype == "hall":
            hall[gname] += 1
        else:
            skips["unknown_facility_id"] += 1
    return dict(gym), dict(hall), dict(skips)


def _line_items(
    groups: dict[str, dict[str, str]],
    venue_type: str,
    counts: dict[str, int],
) -> list[tuple[int, str]]:
    """Sorted (count, initial) for one venue-type row; all groups of that type listed."""
    items: list[tuple[int, str]] = []
    for entry in groups.values():
        gtype = str(entry.get("type") or "").strip()
        if gtype != venue_type:
            continue
        name = str(entry.get("name") or "").strip()
        initial = group_name_initial(name)
        cnt = int(counts.get(name, 0))
        items.append((cnt, initial))
    items.sort(key=lambda x: (x[0], x[1]))
    return items


def build_stats_structured_rows(
    groups: dict[str, dict[str, str]],
    venue_type: str,
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    """Structured rows for CFG_C4 private props (same order as description)."""
    return [
        {"name": init, "count": cnt}
        for cnt, init in _line_items(groups, venue_type, counts)
    ]


def format_stats_description(
    year: int,
    month: int,
    groups: dict[str, dict[str, str]],
    gym_counts: dict[str, int],
    hall_counts: dict[str, int],
) -> str:
    gym_items = _line_items(groups, "gym", gym_counts)
    hall_items = _line_items(groups, "hall", hall_counts)
    gym_txt = "、".join(f"{init}({cnt})" for cnt, init in gym_items)
    hall_txt = "、".join(f"{init}({cnt})" for cnt, init in hall_items)
    return f"【{month}月】\n・体育館：{gym_txt}\n・公民館：{hall_txt}"


def _stats_event_dates(year: int, month: int) -> tuple[str, str]:
    """All-day stats event on the last day of the target month.

    Google Calendar all-day ``end.date`` is exclusive (day after).
    """
    last = calendar.monthrange(year, month)[1]
    start_d = date(year, month, last)
    end_d = start_d + timedelta(days=1)
    return start_d.isoformat(), end_d.isoformat()


def _clear_stats_calendar(svc: Any, cal_stats: str) -> int:
    """Delete every event on CFG_C4 (no title/month matching)."""
    deleted = 0
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": cal_stats,
            "singleEvents": True,
            "maxResults": 2500,
        }
        if token:
            kwargs["pageToken"] = token
        resp = svc.events().list(**kwargs).execute()
        for ev in resp.get("items") or []:
            eid = ev.get("id")
            if not eid:
                continue
            svc.events().delete(calendarId=cal_stats, eventId=eid).execute()
            deleted += 1
        token = resp.get("nextPageToken")
        if not token:
            break
    return deleted


def _log_group_type_summary(groups: dict[str, dict[str, str]]) -> None:
    gym_n = hall_n = missing_n = 0
    for entry in groups.values():
        gtype = str(entry.get("type") or "").strip()
        if gtype == "gym":
            gym_n += 1
        elif gtype == "hall":
            hall_n += 1
        else:
            missing_n += 1
    logger.info(
        "stats groups.json: gym=%s hall=%s missing_type=%s total=%s",
        gym_n,
        hall_n,
        missing_n,
        len(groups),
    )
    if missing_n == len(groups) and groups:
        logger.warning(
            "stats: all groups missing type in local/groups.json; "
            "体育館/公民館 lines will be empty until type is configured"
        )


def _log_stats_diagnostics(
    year: int,
    month: int,
    source: list[dict[str, Any]],
    fid_type: dict[int, str],
    gym_counts: dict[str, int],
    hall_counts: dict[str, int],
    skips: dict[str, int],
) -> None:
    month_key = _month_key(year, month)
    reservation = [ev for ev in source if _is_reservation_event(ev)]
    logger.info(
        "stats diagnostics month=%s source_total=%s reservation_events=%s",
        month_key,
        len(source),
        len(reservation),
    )
    for ev in reservation:
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        raw_fid = priv.get("facilityId")
        vtype: str | None = None
        if raw_fid is not None and str(raw_fid).strip() != "":
            try:
                vtype = fid_type.get(int(raw_fid))
            except (TypeError, ValueError):
                vtype = None
        logger.info(
            "stats event summary=%r group_name=%r facilityId=%r venue_type=%r",
            ev.get("summary"),
            priv.get("group_name"),
            raw_fid,
            vtype,
        )
    if skips:
        logger.info("stats skipped reservation events: %s", dict(skips))
    logger.info("stats counts month=%s gym=%s hall=%s", month_key, gym_counts, hall_counts)


def upsert_month_stats(
    year: int,
    month: int,
    *,
    groups: dict[str, dict[str, str]] | None = None,
    cred_path: str | None = None,
    cal_regular: str | None = None,
    cal_lottery: str | None = None,
    cal_stats: str | None = None,
    svc: Any | None = None,
) -> None:
    """Recompute one month's stats from CFG_C2/C3 and write to CFG_C4."""
    path, cid_reg, cid_lot, cid_stats = _stats_calendars_cfg(
        cred_path, cal_regular, cal_lottery, cal_stats
    )
    client = svc if svc is not None else _cal_svc(path)
    gmap = groups if groups is not None else load_groups(PATH_GROUPS)
    fid_type = _fid_to_venue_type()

    source = _list_month(client, cid_reg, year, month) + _list_month(
        client, cid_lot, year, month
    )
    gym_counts, hall_counts, skips = _count_events_by_group_and_venue_type(
        source, fid_type
    )
    _log_stats_diagnostics(
        year, month, source, fid_type, gym_counts, hall_counts, skips
    )
    month_key = _month_key(year, month)
    body_text = format_stats_description(
        year, month, gmap, gym_counts, hall_counts
    )
    title = stats_event_title(month)
    gym_rows = build_stats_structured_rows(gmap, "gym", gym_counts)
    hall_rows = build_stats_structured_rows(gmap, "hall", hall_counts)

    start_d, end_d = _stats_event_dates(year, month)
    payload = {
        "summary": title,
        "description": body_text,
        "start": {"date": start_d},
        "end": {"date": end_d},
        "extendedProperties": {
            "private": {
                "stats_kind": _STATS_KIND,
                "stats_month": month_key,
                # Calendar private props are string-valued; store JSON arrays.
                "gym": json.dumps(gym_rows, ensure_ascii=False),
                "hall": json.dumps(hall_rows, ensure_ascii=False),
            }
        },
    }
    client.events().insert(calendarId=cid_stats, body=payload).execute()
    logger.info(
        "stats inserted month=%s title=%s body=%r gym_n=%s hall_n=%s",
        month_key,
        title,
        body_text.replace("\n", " / "),
        len(gym_rows),
        len(hall_rows),
    )


def update_stats_calendar(
    today: date | None = None,
    *,
    groups: dict[str, dict[str, str]] | None = None,
) -> None:
    """Clear CFG_C4 entirely, then refresh stats for current + next month."""
    path, _cid_reg, _cid_lot, cid_stats = _stats_calendars_cfg()
    client = _cal_svc(path)
    gmap = groups if groups is not None else load_groups(PATH_GROUPS)
    _log_group_type_summary(gmap)

    deleted = _clear_stats_calendar(client, cid_stats)
    logger.info("stats cleared CFG_C4 entirely n=%s", deleted)

    for year, month in stats_target_months(today):
        upsert_month_stats(
            year,
            month,
            groups=gmap,
            svc=client,
        )


def preview_stats(
    year: int,
    month: int,
    *,
    groups: dict[str, dict[str, str]] | None = None,
    cred_path: str | None = None,
    cal_regular: str | None = None,
    cal_lottery: str | None = None,
) -> str:
    """Return formatted stats body without writing (for inspection)."""
    path, cid_reg, cid_lot, _cid_stats = _stats_calendars_cfg(
        cred_path, cal_regular, cal_lottery, os.getenv("CFG_C4", "")
    )
    client = _cal_svc(path)
    gmap = groups if groups is not None else load_groups(PATH_GROUPS)
    _log_group_type_summary(gmap)
    fid_type = _fid_to_venue_type()
    source = _list_month(client, cid_reg, year, month) + _list_month(
        client, cid_lot, year, month
    )
    gym_counts, hall_counts, skips = _count_events_by_group_and_venue_type(
        source, fid_type
    )
    _log_stats_diagnostics(
        year, month, source, fid_type, gym_counts, hall_counts, skips
    )
    return format_stats_description(year, month, gmap, gym_counts, hall_counts)
