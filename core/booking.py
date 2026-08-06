"""Playwright auto-booking for the public facility site."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PATH_VENUES = ROOT / "config" / "venues.json"
PATH_TASKS = ROOT / "config" / "booking_tasks.json"
PATH_NAMES = ROOT / "local" / "names.json"
PATH_GROUPS = ROOT / "local" / "groups.json"
TZ = ZoneInfo("Asia/Tokyo")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_PURPOSE = "バドミントン"
_DEFAULT_HEADCOUNT = 12
_POLL_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.4
_APPLY_RETRY_MAX = 20
_APPLY_RETRY_INTERVAL_S = 1.5
_TIME_UNAVAILABLE_HINT = "選択された時間枠の一部が利用できなくなっています。"
_TIMEOUT_PEAK_MS = 90000
_TIMEOUT_LOCAL_MS = 5000


def _format_step_when(date_s: str, time_slot: str) -> str:
    """MM-DD HH:MM-HH:MM for step-log prefix (merged slot range as-is)."""
    raw = (date_s or "").strip()
    md = "--"
    if raw:
        try:
            d = date.fromisoformat(raw[:10])
            md = f"{d.month:02d}-{d.day:02d}"
        except ValueError:
            md = raw[:5] if len(raw) >= 5 else raw
    slot = (time_slot or "").strip() or "--:--"
    return f"{md} {slot}"


def _team_initial(group_id: str = "", group_name: str = "") -> str:
    """First character of team name (groups.json name / passed group_name)."""
    name = (group_name or "").strip()
    if not name and group_id:
        gid = str(group_id).strip()
        try:
            for entry in load_groups().values():
                if entry.get("id") == gid:
                    name = (entry.get("name") or "").strip()
                    break
        except Exception:
            pass
    return name[0] if name else "-"


def _step_log(
    group_id: str,
    venue_name: str,
    message: str,
    *,
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    """Unified booking-step log: [{team0}|{venue}|{MM-DD HH:MM-HH:MM}]{message}."""
    initial = _team_initial(group_id, group_name)
    vname = venue_name or "-"
    when = _format_step_when(date_s, time_slot)
    logger.info(
        "[%s|%s|%s]%s",
        initial,
        vname,
        when,
        message,
        extra={"booking_step": True},
    )


def _safe_print(msg: str) -> None:
    """Print without crashing on Windows cp932 consoles."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


def wait_until_target(
    target_hour: int = 9,
    target_minute: int = 0,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    """Sleep until Asia/Tokyo target clock; abort if more than 10 minutes early."""
    now = datetime.now(TZ)
    target = now.replace(
        hour=target_hour,
        minute=target_minute,
        second=0,
        microsecond=0,
    )
    if target <= now:
        # already past today's target → treat as reached
        _step_log(
            group_id,
            venue_name,
            "9時に到達、ログイン実行",
            date_s=date_s,
            time_slot=time_slot,
            group_name=group_name,
        )
        return
    remain = (target - now).total_seconds()
    if remain > 600:
        raise RuntimeError(
            "9am wait aborted: too early (>10 minutes before target)"
        )
    remain_s = max(1, int(round(remain)))
    _step_log(
        group_id,
        venue_name,
        f"9時まで待機中（残り{remain_s}秒）",
        date_s=date_s,
        time_slot=time_slot,
        group_name=group_name,
    )
    if remain > 1:
        time.sleep(remain - 0.5)
        time.sleep(0.5)
    else:
        time.sleep(max(0.0, remain))
    _step_log(
        group_id,
        venue_name,
        "9時に到達、ログイン実行",
        date_s=date_s,
        time_slot=time_slot,
        group_name=group_name,
    )


def load_venues(path: Path | None = None) -> dict[str, dict[str, Any]]:
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


_HOUR_SLOT_MAP: dict[int, str] = {
    9: "09:00-11:00",
    11: "11:00-13:00",
    13: "13:00-15:00",
    15: "15:00-17:00",
    17: "17:00-19:00",
    19: "19:00-21:00",
}
_HOUR_ORDER: list[int] = [9, 11, 13, 15, 17, 19]
_SLOT_TO_HOUR: dict[str, int] = {v: k for k, v in _HOUR_SLOT_MAP.items()}


def next_month_date(day: int, *, now: datetime | None = None) -> str:
    """Map day-of-month to the same day next month (Asia/Tokyo); Dec→Jan rolls year."""
    base = now or datetime.now(TZ)
    y, m = base.year, base.month
    if m == 12:
        y, m = y + 1, 1
    else:
        m += 1
    day_i = int(day)
    last = calendar.monthrange(y, m)[1]
    if day_i < 1 or day_i > last:
        raise ValueError(
            f"day={day_i} is invalid for next month {y}-{m:02d} (1-{last})"
        )
    return date(y, m, day_i).isoformat()


def load_groups(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Load local/groups.json keyed by group_ref (e.g. GROUP1)."""
    p = path or PATH_GROUPS
    if not p.exists():
        raise ValueError(f"groups file missing: {p}")
    with p.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("groups.json must be an object keyed by group_ref")
    out: dict[str, dict[str, str]] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        out[str(key)] = {
            "id": str(val.get("id") or "").strip(),
            "name": str(val.get("name") or "").strip(),
            "password": str(val.get("password") or "").strip(),
            "type": str(val.get("type") or "").strip(),
        }
    return out


def resolve_group_ref(
    group_ref: str,
    *,
    groups: dict[str, dict[str, str]] | None = None,
) -> tuple[str, str, str]:
    """Return (login_id, display_name, password) for group_ref from local/groups.json."""
    ref = str(group_ref or "").strip()
    if not ref:
        raise ValueError("group_ref is empty")
    data = groups if groups is not None else load_groups()
    if ref not in data:
        raise ValueError(f"group_ref {ref!r}: not found in local/groups.json")
    entry = data[ref]
    missing = [f for f in ("id", "name", "password") if not entry.get(f)]
    if missing:
        raise ValueError(
            f"group_ref {ref!r}: missing field(s) in local/groups.json: "
            f"{', '.join(missing)}"
        )
    return entry["id"], entry["name"], entry["password"]


def parse_hour_starts(hours: str) -> list[int]:
    """Parse comma-separated hour starts; skip unknown with warning (preserve order, unique)."""
    seen: set[int] = set()
    out: list[int] = []
    for part in str(hours or "").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            hour = int(token)
        except ValueError:
            msg = f"skip invalid hour {token!r} (not an integer)"
            _safe_print(f"warning: {msg}")
            logger.debug(msg)
            continue
        if hour not in _HOUR_SLOT_MAP:
            msg = f"skip invalid hour {hour} (not in slot map)"
            _safe_print(f"warning: {msg}")
            logger.debug(msg)
            continue
        if hour in seen:
            continue
        seen.add(hour)
        out.append(hour)
    return out


def parse_hours(hours: str) -> list[str]:
    """Map comma-separated hour starts to time_slot strings; skip unknown with warning."""
    return [_HOUR_SLOT_MAP[h] for h in parse_hour_starts(hours)]


def group_hours_for_venue(hours: list[int], venue_type: str) -> list[list[int]]:
    """Split hour starts into submit groups: gym=always 1; hall=adjacent pairs (max 2)."""
    ordered = sorted(
        (h for h in hours if h in _HOUR_SLOT_MAP),
        key=lambda h: _HOUR_ORDER.index(h),
    )
    if not ordered:
        return []
    vtype = (venue_type or "").strip().lower()
    if vtype == "gym":
        return [[h] for h in ordered]

    # hall (and unknown): contiguous runs on _HOUR_ORDER, chunk size ≤ 2
    runs: list[list[int]] = []
    cur: list[int] = []
    for h in ordered:
        if not cur:
            cur = [h]
            continue
        if _HOUR_ORDER.index(h) == _HOUR_ORDER.index(cur[-1]) + 1:
            cur.append(h)
        else:
            runs.append(cur)
            cur = [h]
    if cur:
        runs.append(cur)

    groups: list[list[int]] = []
    for run in runs:
        for i in range(0, len(run), 2):
            groups.append(run[i : i + 2])
    return groups


def display_time_range(time_slots: list[str]) -> str:
    """Single slot as-is; two slots merge to start-of-first–end-of-last (e.g. 17:00-21:00)."""
    slots = [str(s).strip() for s in time_slots if str(s).strip()]
    if not slots:
        return ""
    if len(slots) == 1:
        return slots[0]
    start = slots[0].split("-", 1)[0].strip()
    end = slots[-1].rsplit("-", 1)[-1].strip()
    return f"{start}-{end}"


def is_open_wait_mode(now: datetime | None = None) -> bool:
    """Branch A: Tokyo calendar day is the 1st and local time is before 09:00."""
    if now is None:
        cur = datetime.now(TZ)
    elif now.tzinfo is None:
        cur = now.replace(tzinfo=TZ)
    else:
        cur = now.astimezone(TZ)
    return cur.day == 1 and (cur.hour, cur.minute, cur.second, cur.microsecond) < (
        9,
        0,
        0,
        0,
    )


def tasks_from_cli(
    *,
    venue_code: str,
    day: int,
    hours: str,
    group_ref: str,
    now: datetime | None = None,
    venues: dict[str, dict[str, Any]] | None = None,
    groups: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Build book_one tasks from CLI fields via the same expand_booking_tasks path."""
    return expand_booking_tasks(
        [
            {
                "venue_code": str(venue_code).strip(),
                "day": int(day),
                "hours": str(hours).strip(),
                "group_ref": str(group_ref).strip(),
            }
        ],
        now=now,
        venues=venues,
        groups=groups,
    )


def task_time_slots(task: dict[str, Any]) -> list[str]:
    raw = task.get("time_slots")
    if isinstance(raw, list) and raw:
        return [str(s) for s in raw]
    one = task.get("time_slot")
    if one:
        return [str(one)]
    return []


def warn_gym_adjacent_slots(
    tasks: list[dict[str, Any]],
    venues: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Warn when a gym venue has adjacent hour starts on the same day (any tasks)."""
    venues = venues if venues is not None else load_venues()
    by_vd: dict[tuple[str, str], set[int]] = defaultdict(set)
    for t in tasks:
        code = str(t.get("venue_code") or "")
        meta = venues.get(code)
        if not meta or str(meta.get("type") or "").strip().lower() != "gym":
            continue
        date_s = str(t.get("date") or "")
        for slot in task_time_slots(t):
            hour = _SLOT_TO_HOUR.get(slot.strip())
            if hour is not None:
                by_vd[(code, date_s)].add(hour)

    for (code, date_s), hours in sorted(by_vd.items()):
        ordered = sorted(hours, key=lambda h: _HOUR_ORDER.index(h))
        for a, b in zip(ordered, ordered[1:]):
            if _HOUR_ORDER.index(b) != _HOUR_ORDER.index(a) + 1:
                continue
            slot1 = _HOUR_SLOT_MAP[a]
            slot2 = _HOUR_SLOT_MAP[b]
            msg = (
                f"警告：{code} 在 {date_s} 配置了相邻时段 {slot1} 和 {slot2}，"
                f"体育馆类场地这两个时段大概率有一个会预约失败"
            )
            _safe_print(msg)
            logger.debug(msg)


def expand_booking_tasks(
    raw: list[Any],
    *,
    now: datetime | None = None,
    venues: dict[str, dict[str, Any]] | None = None,
    groups: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Turn compact booking_tasks entries into book_one tasks (time_slots list)."""
    venues = venues if venues is not None else load_venues()
    groups = groups if groups is not None else load_groups()
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"booking task[{i}] must be an object")
        venue_code = str(item.get("venue_code") or "").strip()
        if not venue_code:
            raise ValueError(f"booking task[{i}]: venue_code required")
        meta = venues.get(venue_code)
        if not meta:
            raise ValueError(f"booking task[{i}]: unknown venue_code {venue_code!r}")
        venue_type = str(meta.get("type") or "").strip()
        if "day" not in item:
            raise ValueError(f"booking task[{i}]: day required")
        date_s = next_month_date(int(item["day"]), now=now)
        hour_starts = parse_hour_starts(str(item.get("hours") or ""))
        hour_groups = group_hours_for_venue(hour_starts, venue_type)
        if not hour_groups:
            msg = f"booking task[{i}]: no valid hours after mapping; skipped"
            _safe_print(f"warning: {msg}")
            logger.debug(msg)
            continue
        group_ref = str(item.get("group_ref") or "").strip()
        login_id, group_name, password = resolve_group_ref(
            group_ref, groups=groups
        )
        for hour_group in hour_groups:
            slots = [_HOUR_SLOT_MAP[h] for h in hour_group]
            row: dict[str, Any] = {
                "venue_code": venue_code,
                "date": date_s,
                "time_slots": slots,
                "group_ref": group_ref,
                "group_id": login_id,
                "group_name": group_name,
                "password": password,
            }
            if item.get("headcount") is not None:
                try:
                    row["headcount"] = int(item["headcount"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"booking task[{i}]: headcount must be an integer"
                    ) from exc
            out.append(row)
    warn_gym_adjacent_slots(out, venues)
    return out


def load_tasks(
    path: Path | None = None,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    p = path or PATH_TASKS
    with p.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        raw = raw.get("tasks", [])
    if not isinstance(raw, list):
        raise ValueError("booking_tasks.json must be a list (or object with tasks)")
    return expand_booking_tasks(raw, now=now)


def load_names(path: Path | None = None) -> dict[str, str]:
    p = path or PATH_NAMES
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    out: dict[str, str] = {}
    for code, val in (raw or {}).items():
        if isinstance(val, dict):
            name = str(val.get("name", "")).strip()
            if name:
                out[str(code)] = name
        elif isinstance(val, str) and val.strip():
            out[str(code)] = val.strip()
    return out


def _base_url() -> str:
    return os.getenv("CFG_B1", "").strip().rstrip("/")


def _tenant() -> str:
    return os.getenv("CFG_B2", "").strip()


def time_slot_to_jp(time_slot: str) -> str:
    """Convert '09:00-11:00' / '19:00-21:00' → '9時～11時' / '19時～21時'."""
    parts = re.split(r"\s*[-～~]\s*", time_slot.strip())
    if len(parts) != 2:
        return time_slot.strip()

    def _h(hhmm: str) -> str:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
        if not m:
            return hhmm
        return str(int(m.group(1)))

    return f"{_h(parts[0])}時～{_h(parts[1])}時"


def _result(
    *,
    success: bool,
    venue_code: str,
    venue_name: str,
    date_s: str,
    time_slot: str,
    group_id: str,
    group_name: str = "",
    error_message: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """BookingResult-shaped dict used by book_one / run_tasks / mail."""
    out = {
        "success": success,
        "venue_code": venue_code,
        "venue_name": venue_name,
        "date": date_s,
        "time_slot": time_slot,
        "group_id": group_id,
        "group_name": group_name,
        "error_message": error_message,
    }
    if extra:
        out.update(extra)
    return out


class SiteDialogBlocked(RuntimeError):
    """Native browser dialog blocked the flow; do not embed dialog text."""

    def __init__(self) -> None:
        super().__init__("网站弹窗阻止提交")


# Mail-facing Japanese summaries (technical detail goes to booking.log only).
MSG_LOGIN_FAIL = (
    "ログインに失敗しました（IDまたはパスワードをご確認ください）"
)
MSG_SLOT_GONE = (
    "対象の時間帯はすでに予約できません（空きがなくなりました）"
)
MSG_SLOT_UNAVAILABLE = (
    "対象の時間帯は現在予約できません（空きがありません）"
)
MSG_UI_FAIL = (
    "画面操作に失敗しました（施設の選択がうまくいきませんでした）"
)
MSG_PURPOSE_MISSING = (
    "この施設では「バドミントン」の利用目的が見つかりませんでした"
)
MSG_ACCEPT_TIMEOUT = "受付開始の確認がタイムアウトしました"
MSG_CONFIRM_UNCLEAR = (
    "予約完了の確認ができませんでした"
    "（実際は成功している可能性があります、マイページをご確認ください）"
)
MSG_UNEXPECTED = (
    "予期しないエラーが発生しました（詳細はログをご確認ください）"
)
MSG_APPLY_RETRY_EXHAUSTED = (
    "予約可能な時間帯が見つかりませんでした（再試行上限に達しました）"
)
MSG_SESSION_EXPIRED = "ログインセッションが切れました"


def user_error_message(
    source: str | BaseException,
    *,
    log_detail: bool = True,
) -> str:
    """Map internal failure signals to a Japanese mail-facing summary."""
    if isinstance(source, BaseException):
        detail = f"{type(source).__name__}: {source}"
        raw = str(source).strip()
        name = type(source).__name__
    else:
        detail = str(source).strip()
        raw = detail
        name = ""

    if log_detail and detail:
        logger.debug("booking failure detail: %s", detail.replace("\n", " "))

    # Already a known Japanese summary → keep as-is
    known_jp = {
        MSG_LOGIN_FAIL,
        MSG_SLOT_GONE,
        MSG_SLOT_UNAVAILABLE,
        MSG_UI_FAIL,
        MSG_PURPOSE_MISSING,
        MSG_ACCEPT_TIMEOUT,
        MSG_CONFIRM_UNCLEAR,
        MSG_UNEXPECTED,
        MSG_APPLY_RETRY_EXHAUSTED,
        MSG_SESSION_EXPIRED,
    }
    if raw in known_jp:
        return raw

    low = raw.lower()
    blob = f"{name} {raw}".lower()

    if isinstance(source, SiteDialogBlocked) or "网站弹窗阻止提交" in raw:
        return MSG_UI_FAIL

    if "ログインセッションが切れました" in raw or "session" in low and "login" in low:
        return MSG_SESSION_EXPIRED

    if "再試行上限" in raw or "予約可能な時間帯が見つかりませんでした" in raw:
        return MSG_APPLY_RETRY_EXHAUSTED

    if (
        "login rejected" in low
        or "login failed" in low
        or "password missing" in low
        or "password env missing" in low
        or "idまたはパスワード" in raw
        or "パスワードが違" in raw
        or "正しくありません" in raw
    ):
        return MSG_LOGIN_FAIL

    if (
        "slot no longer available" in low
        or "unavailable after polling" in low
        or "target slot unavailable" in low
        or "現在予約できません" in raw
    ):
        if "現在予約できません" in raw:
            return MSG_SLOT_UNAVAILABLE
        return MSG_SLOT_GONE

    if (
        "checkbox" in low
        or "native click" in low
        or "click target not found" in low
        or "failed to select 団体" in raw
        or "space-" in low
    ):
        return MSG_UI_FAIL

    if "purpose option not found" in low or "バドミントン" in raw and "purpose" in low:
        return MSG_PURPOSE_MISSING

    if (
        "9am wait" in low
        or "wait_until_target" in low
        or "spacetime" in low
        or "poll" in low and "timeout" in low
        or "受付開始" in raw
    ):
        return MSG_ACCEPT_TIMEOUT

    if (
        "timeout" in name.lower()
        or "timeout" in low
        or "timed out" in low
    ):
        # Page/element timeouts during booking flow → treat as accept/window timeout
        # unless clearly a confirm-page issue.
        if any(
            x in low
            for x in (
                "confirm",
                "予約完了",
                "reservation",
                "確定",
                "complete",
            )
        ):
            return MSG_CONFIRM_UNCLEAR
        return MSG_ACCEPT_TIMEOUT

    if any(
        x in low
        for x in (
            "confirm",
            "予約完了",
            "確認ができません",
        )
    ):
        return MSG_CONFIRM_UNCLEAR

    return MSG_UNEXPECTED


def format_book_error(exc: BaseException) -> str:
    """Japanese mail-facing summary; full exception detail is logged."""
    return user_error_message(exc, log_detail=True)


def _fail_from_task(
    task: dict[str, Any],
    error_message: str | BaseException,
    *,
    names: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    venue_code = str(task.get("venue_code") or "")
    names = names or {}
    slots = task_time_slots(task)
    return _result(
        success=False,
        venue_code=venue_code,
        venue_name=names.get(venue_code, venue_code or "-"),
        date_s=str(task.get("date") or ""),
        time_slot=display_time_range(slots),
        group_id=str(task.get("group_id") or ""),
        group_name=str(task.get("group_name") or ""),
        error_message=user_error_message(error_message, log_detail=True),
        extra=extra,
    )


async def _dom_click_handle(handle: Any) -> None:
    """Fire a native DOM click() on an ElementHandle."""
    await handle.evaluate("el => el.click()")


async def _dom_click_locator(loc: Any, *, timeout: float = _TIMEOUT_PEAK_MS) -> None:
    await loc.wait_for(state="attached", timeout=timeout)
    handle = await loc.element_handle()
    if handle is None:
        raise RuntimeError("native click target not found")
    await _dom_click_handle(handle)


async def _click_text(page: Page, text: str, *, timeout: float = _TIMEOUT_PEAK_MS) -> None:
    loc = page.get_by_role("button", name=re.compile(re.escape(text)))
    if await loc.count() == 0:
        loc = page.locator(f"button:has-text('{text}')")
    await _dom_click_locator(loc.first, timeout=timeout)


async def _check_space_checkbox(page: Page, sid: int) -> None:
    """Check #space-{sid} via native DOM click; retry if checked stays false."""
    eid = f"space-{int(sid)}"
    await page.wait_for_selector(
        f"#{eid}", state="attached", timeout=_TIMEOUT_PEAK_MS
    )
    for attempt in range(3):
        checked = await page.evaluate(
            """(id) => {
              const el = document.getElementById(id);
              if (!el) return null;
              if (!el.checked) {
                el.click();
              }
              return !!el.checked;
            }""",
            eid,
        )
        if checked is True:
            return
        if checked is None:
            raise RuntimeError(f"checkbox #{eid} not found in DOM")
        logger.debug(
            "checkbox #%s still unchecked after click (attempt %s/3)",
            eid,
            attempt + 1,
        )
        try:
            await page.wait_for_function(
                """(id) => {
                  const el = document.getElementById(id);
                  return !!(el && el.checked);
                }""",
                arg=eid,
                timeout=_TIMEOUT_LOCAL_MS,
            )
            return
        except Exception:
            pass
    raise RuntimeError("checkbox click did not register after retry")


async def _wait_ready(page: Page) -> None:
    try:
        await page.wait_for_function(
            "() => !document.body.innerText.includes('読み込み中')",
            timeout=_TIMEOUT_PEAK_MS,
        )
    except Exception:
        pass


def _is_login_url(url: str) -> bool:
    return "/login" in (url or "")


async def _goto_home(page: Page) -> None:
    base = _base_url()
    await page.goto(
        f"{base}/", wait_until="domcontentloaded", timeout=_TIMEOUT_PEAK_MS
    )
    await _wait_ready(page)


async def _search_and_select_space(
    page: Page,
    venue_name: str,
    sid: int,
    *,
    group_id: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    _step_log(group_id, venue_name, "施設検索中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    box = page.get_by_placeholder("施設名称を入力")
    if await box.count() == 0:
        box = page.get_by_role("textbox", name=re.compile("施設"))
    await box.first.fill(venue_name)
    await _click_text(page, "施設を検索する")
    await page.wait_for_url(
        re.compile(r"/facility/availability"), timeout=_TIMEOUT_PEAK_MS
    )
    await _wait_ready(page)
    await _check_space_checkbox(page, sid)
    _step_log(group_id, venue_name, "施設選択完了、空き状況確認へ",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    await _click_text(page, "空き状況を確認する")
    await page.wait_for_url(
        re.compile(r"/facility/calendar"), timeout=_TIMEOUT_PEAK_MS
    )
    await _wait_ready(page)


async def _calendar_pick_date(
    page: Page,
    target: date,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    # Prefer month view so 「次月」 is available.
    month_btn = page.locator("button:has-text('1ヶ月表示')")
    if await month_btn.count():
        await _dom_click_locator(month_btn.first)
        try:
            await page.wait_for_selector(
                "button:has-text('次月')",
                state="visible",
                timeout=_TIMEOUT_PEAK_MS,
            )
        except Exception:
            pass

    nav_n = 0
    nav_total = 0
    body0 = await page.inner_text("body")
    m0 = re.search(r"(\d{4})年(\d{1,2})月", body0)
    if m0:
        cur0 = date(int(m0.group(1)), int(m0.group(2)), 1)
        tgt0 = date(target.year, target.month, 1)
        nav_total = abs((tgt0.year - cur0.year) * 12 + (tgt0.month - cur0.month))

    for _ in range(14):
        body = await page.inner_text("body")
        m = re.search(r"(\d{4})年(\d{1,2})月", body)
        if not m:
            break
        y, mo = int(m.group(1)), int(m.group(2))
        cur = date(y, mo, 1)
        tgt = date(target.year, target.month, 1)
        if cur == tgt:
            break
        want = f"{tgt.year}年{tgt.month}月"
        if cur < tgt:
            direction = "次月"
            await _dom_click_locator(page.locator("button:has-text('次月')").first)
        else:
            direction = "前月"
            await _dom_click_locator(page.locator("button:has-text('前月')").first)
        nav_n += 1
        total_show = nav_total if nav_total > 0 else nav_n
        _step_log(
            group_id,
            venue_name,
            f"月历导航中（第{nav_n}/{total_show}回 {direction}クリック）",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        try:
            await page.wait_for_function(
                """(want) => (document.body.innerText || '').includes(want)""",
                arg=want,
                timeout=_TIMEOUT_PEAK_MS,
            )
        except Exception:
            pass

    _step_log(
        group_id,
        venue_name,
        f"対象日付選択: {target.isoformat()}",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    day = str(target.day)
    cell = page.locator("div.calendar-day.clickable").filter(
        has=page.locator("div.day-number", has_text=re.compile(rf"^{day}$"))
    )
    if await cell.count() == 0:
        cell = page.locator("div.calendar-day.clickable").filter(
            has_text=re.compile(rf"^{day}\b")
        )
    await _dom_click_locator(cell.first)
    _step_log(group_id, venue_name, "時間選択画面へ遷移",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    await _click_text(page, "時間選択へ進む")
    # Caller waits for /facility/time-select with peak timeout.


async def _select_time_cell(
    page: Page,
    time_slot: str,
    venue_type: str,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    slot_range: str = "",
    group_name: str = "",
) -> bool:
    _step_log(group_id, venue_name, f"対象時間帯確認中: {time_slot}",
        date_s=date_s, time_slot=(slot_range or time_slot), group_name=group_name)
    label = time_slot_to_jp(time_slot)
    row = page.locator("tr").filter(has_text=label).first
    await row.wait_for(state="visible", timeout=_TIMEOUT_PEAK_MS)
    cells = row.locator("td")
    n = await cells.count()
    if n == 0:
        return False

    async def _cell_available(idx: int) -> bool:
        txt = (await cells.nth(idx).inner_text()).strip()
        return "○" in txt or "空き" in txt

    if venue_type == "gym":
        # Expect: time | 専用 | 共用A | 共用B  (skip 専用)
        gym_faces = {2: "共用A", 3: "共用B"}
        for idx in range(2, n):
            if await _cell_available(idx):
                face = gym_faces.get(idx, f"共用列{idx}")
                await _dom_click_locator(cells.nth(idx))
                _step_log(group_id, venue_name, f"面選択: {face}",
        date_s=date_s, time_slot=(slot_range or time_slot), group_name=group_name)
                return True
        return False

    # hall: time | 全面
    for idx in range(1, n):
        if await _cell_available(idx):
            await _dom_click_locator(cells.nth(idx))
            _step_log(group_id, venue_name, "面選択: 全面",
        date_s=date_s, time_slot=(slot_range or time_slot), group_name=group_name)
            return True
    mark = row.locator("text=○").first
    if await mark.count():
        await _dom_click_locator(mark)
        _step_log(group_id, venue_name, "面選択: 全面",
        date_s=date_s, time_slot=(slot_range or time_slot), group_name=group_name)
        return True
    return False


async def _time_slots_available_on_page(
    page: Page, slots: list[str], venue_type: str
) -> bool:
    """True if every requested slot row still shows ○/空き (without clicking)."""
    for slot in slots:
        label = time_slot_to_jp(slot)
        row = page.locator("tr").filter(has_text=label).first
        if await row.count() == 0:
            return False
        try:
            await row.wait_for(state="visible", timeout=_TIMEOUT_LOCAL_MS)
        except Exception:
            return False
        cells = row.locator("td")
        n = await cells.count()
        start = 2 if venue_type == "gym" else 1
        found = False
        for idx in range(start, n):
            txt = (await cells.nth(idx).inner_text()).strip()
            if "○" in txt or "空き" in txt:
                found = True
                break
        if not found and venue_type != "gym":
            if await row.locator("text=○").count():
                found = True
        if not found:
            return False
    return True


async def wait_for_slots_settled(
    page: Page,
    slots: list[str],
    venue_type: str,
    *,
    timeout_ms: int = _TIMEOUT_PEAK_MS,
) -> str:
    """Wait until target slots show a definitive status (not loading).

    Returns: ``available`` | ``unavailable`` | ``timeout``.
    """
    labels = [time_slot_to_jp(s) for s in slots]
    try:
        handle = await page.wait_for_function(
            """({ labels, isGym }) => {
              const body = document.body ? (document.body.innerText || '') : '';
              if (body.includes('読み込み中')) return null;
              let allAvailable = true;
              for (const label of labels) {
                const rows = Array.from(document.querySelectorAll('tr'));
                const row = rows.find(
                  (r) => (r.innerText || '').includes(label)
                );
                if (!row) return null;
                const cells = Array.from(row.querySelectorAll('td'));
                const start = isGym ? 2 : 1;
                let rowAvail = false;
                let hasMarker = false;
                for (let i = start; i < cells.length; i++) {
                  const txt = (cells[i].innerText || '').trim();
                  if (!txt) continue;
                  hasMarker = true;
                  if (txt.includes('○') || txt.includes('空き')) {
                    rowAvail = true;
                  }
                }
                if (!isGym && (row.innerText || '').includes('○')) {
                  rowAvail = true;
                  hasMarker = true;
                }
                if (!hasMarker) return null;
                if (!rowAvail) allAvailable = false;
              }
              return allAvailable ? 'available' : 'unavailable';
            }""",
            arg={"labels": labels, "isGym": venue_type == "gym"},
            timeout=timeout_ms,
        )
        return str(await handle.json_value())
    except Exception as exc:
        logger.debug(
            "wait_for_slots_settled timed out/failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return "timeout"


async def _navigate_to_time_select(
    page: Page,
    venue_name: str,
    sid: int,
    day: date,
    *,
    group_id: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    await _goto_home(page)
    if _is_login_url(page.url):
        raise RuntimeError(MSG_SESSION_EXPIRED)
    await _search_and_select_space(page, venue_name, sid, group_id=group_id,
                                date_s=date_s, time_slot=time_slot, group_name=group_name)
    await _calendar_pick_date(
        page, day, group_id=group_id, venue_name=venue_name,
                                date_s=date_s, time_slot=time_slot, group_name=group_name)
    await page.wait_for_url(
        re.compile(r"/facility/time-select"), timeout=_TIMEOUT_PEAK_MS
    )
    await _wait_ready(page)
    if _is_login_url(page.url):
        raise RuntimeError(MSG_SESSION_EXPIRED)


async def _click_yoyaku_moshikomi(
    page: Page,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> bool:
    """Click 予約申込へ if present and enabled. Return False if missing/disabled."""
    btn = page.locator("button:has-text('予約申込へ')")
    if await btn.count() == 0:
        return False
    target = btn.first
    if await target.is_disabled():
        return False
    _step_log(group_id, venue_name, "予約申込へ クリック中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    await _dom_click_locator(target)
    return True


async def wait_after_yoyaku_moshikomi(
    page: Page, *, timeout_ms: int = _TIMEOUT_PEAK_MS
) -> str:
    """After 予約申込へ: return login | proceed | unavailable | timeout."""
    hint = _TIME_UNAVAILABLE_HINT
    try:
        handle = await page.wait_for_function(
            """(hint) => {
              const t = document.body.innerText || '';
              if (location.href.includes('/login')) return 'login';
              const btns = Array.from(document.querySelectorAll('button'));
              const proceed = btns.find(
                (b) => (b.innerText || '').includes('予約入力に進む')
              );
              if (
                proceed
                && !proceed.disabled
                && !!(proceed.offsetWidth || proceed.offsetHeight
                      || proceed.getClientRects().length)
              ) {
                return 'proceed';
              }
              if (t.includes(hint) || t.includes(hint.replace(/。$/, ''))) {
                return 'unavailable';
              }
              return null;
            }""",
            arg=hint,
            timeout=timeout_ms,
        )
        return str(await handle.json_value())
    except Exception as exc:
        logger.debug(
            "wait_after_yoyaku_moshikomi timed out/failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return "timeout"


async def _page_has_unavailable_hint(page: Page) -> bool:
    try:
        body = await page.inner_text("body")
    except Exception:
        return False
    return _TIME_UNAVAILABLE_HINT in (body or "")


async def _yoyaku_button_disabled(page: Page) -> bool:
    btn = page.locator("button:has-text('予約申込へ')")
    if await btn.count() == 0:
        return False
    try:
        return bool(await btn.first.is_disabled())
    except Exception:
        return False


async def _yoyaku_button_clickable(page: Page) -> bool:
    """True when 予約申込へ is present and enabled (selection likely retained)."""
    btn = page.locator("button:has-text('予約申込へ')")
    if await btn.count() == 0:
        return False
    try:
        if not await btn.first.is_visible():
            return False
        return not bool(await btn.first.is_disabled())
    except Exception:
        return False


async def _can_click_yoyaku_input(page: Page) -> bool:
    """Branch B: next step reachable via 予約入力に進む."""
    if _is_login_url(page.url):
        return False
    btn = page.locator("button:has-text('予約入力に進む')")
    if await btn.count() == 0:
        return False
    try:
        if not await btn.first.is_visible():
            return False
        if await btn.first.is_disabled():
            return False
    except Exception:
        return False
    return True


async def _persist_has_time_selection(page: Page) -> bool:
    """True if localStorage persist:root looks like it still holds timeSelection."""
    try:
        return bool(
            await page.evaluate(
                """() => {
                  try {
                    const raw = localStorage.getItem('persist:root');
                    if (!raw) return false;
                    const root = JSON.parse(raw);
                    const tsRaw = root.timeSelection;
                    if (tsRaw == null || tsRaw === '') return false;
                    const ts = typeof tsRaw === 'string' ? JSON.parse(tsRaw) : tsRaw;
                    if (!ts || typeof ts !== 'object') return false;
                    const blob = JSON.stringify(ts);
                    if (blob.length < 10) return false;
                    if (blob === '{}' || blob === 'null') return false;
                    return true;
                  } catch (e) {
                    return false;
                  }
                }"""
            )
        )
    except Exception:
        return False


async def _try_resume_after_login(
    page: Page,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> bool:
    """After login, reuse site-persisted selection if possible.

    Returns True when we can continue without re-running 施設検索→日付→時間帯.
    """
    _step_log(group_id, venue_name, "ログイン後の選択状態を確認中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    try:
        await page.wait_for_function(
            """() => {
              const u = location.href || '';
              if (u.includes('/login')) return false;
              if (u.includes('/reservations/create')) return true;
              if (u.includes('/facility/time-select')) return true;
              const t = document.body ? (document.body.innerText || '') : '';
              if (t.includes('予約入力に進む')) return true;
              if (t.includes('予約申込へ')) return true;
              return document.readyState === 'complete';
            }""",
            timeout=_TIMEOUT_PEAK_MS,
        )
    except Exception:
        pass
    await _wait_ready(page)

    if _is_login_url(page.url):
        return False

    url = page.url or ""
    if await _can_click_yoyaku_input(page):
        _step_log(group_id, venue_name, "選択状態を保持、予約入力へ進む",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        return True

    if "/reservations/create" in url:
        _step_log(group_id, venue_name, "選択状態を保持、予約入力画面へ",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        return True

    preserved = await _persist_has_time_selection(page)
    on_time_select = "/facility/time-select" in url

    if on_time_select or preserved:
        # UI may need a moment to rehydrate selection onto the 申込 button.
        if not await _yoyaku_button_clickable(page):
            try:
                await page.wait_for_function(
                    """() => {
                      const btns = Array.from(document.querySelectorAll('button'));
                      const b = btns.find(
                        (x) => (x.innerText || '').includes('予約申込へ')
                      );
                      return !!(b && !b.disabled);
                    }""",
                    timeout=_TIMEOUT_LOCAL_MS,
                )
            except Exception:
                pass

        if await _yoyaku_button_clickable(page):
            _step_log(group_id, venue_name, "選択状態を保持、予約申込へ",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
            if await _click_yoyaku_moshikomi(
                page, group_id=group_id, venue_name=venue_name,
                                date_s=date_s, time_slot=time_slot, group_name=group_name):
                outcome = await wait_after_yoyaku_moshikomi(page)
                logger.debug("post-login resume wait outcome=%s", outcome)
                if outcome == "login" or _is_login_url(page.url):
                    return False
                if outcome == "proceed" or await _can_click_yoyaku_input(page):
                    return True
                # Still on time-select / unavailable — selection existed; let
                # apply_for_slots_with_retry handle without full re-nav.
                if "/facility/time-select" in (page.url or ""):
                    return True
                if "/reservations/create" in (page.url or ""):
                    return True

        if on_time_select and preserved:
            # Selection in storage but button not ready — still avoid full
            # facility re-search; apply retry can re-click cells on this page.
            _step_log(
                group_id,
                venue_name,
                "選択状態を保持（時間選択画面）、申込を再試行",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
            return True

    return False


async def apply_for_slots_with_retry(
    page: Page,
    *,
    slots: list[str],
    venue_type: str,
    venue_name: str,
    sid: int,
    day: date,
    group_id: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
    max_retries: int = _APPLY_RETRY_MAX,
    interval_s: float = _APPLY_RETRY_INTERVAL_S,
) -> None:
    """Select slots and click 予約申込へ until next step, or raise after retries.

    Raises RuntimeError with MSG_APPLY_RETRY_EXHAUSTED or MSG_SESSION_EXPIRED.
    """
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    for attempt in range(1, max_retries + 1):
        if _is_login_url(page.url):
            logger.debug(
                "apply retry %s/%s: session expired (redirected to login)",
                attempt,
                max_retries,
            )
            raise RuntimeError(MSG_SESSION_EXPIRED)

        if await _can_click_yoyaku_input(page):
            logger.debug(
                "apply retry %s/%s: already on next step (予約入力に進む)",
                attempt,
                max_retries,
            )
            return

        on_time_select = "/facility/time-select" in (page.url or "")
        available = False
        if on_time_select:
            available = await _time_slots_available_on_page(
                page, slots, venue_type
            )

        hint_before = await _page_has_unavailable_hint(page)
        disabled_before = await _yoyaku_button_disabled(page)
        logger.debug(
            "apply retry %s/%s: url=%s available=%s hint=%s btn_disabled=%s",
            attempt,
            max_retries,
            page.url,
            available,
            hint_before,
            disabled_before,
        )

        outcome = ""
        if on_time_select and available:
            selected_ok = True
            # If 予約申込へ is already enabled, selection is retained — skip re-clicking cells.
            if not await _yoyaku_button_clickable(page):
                for slot in slots:
                    if not await _select_time_cell(
                        page,
                        slot,
                        venue_type,
                        group_id=group_id,
                        venue_name=venue_name,
                                    date_s=date_s, slot_range=time_slot, group_name=group_name):
                        selected_ok = False
                        break
            else:
                logger.debug(
                    "apply retry %s/%s: 予約申込へ already enabled; skip cell re-select",
                    attempt,
                    max_retries,
                )
            if selected_ok:
                clicked = await _click_yoyaku_moshikomi(
                    page, group_id=group_id, venue_name=venue_name,
                                date_s=date_s, time_slot=time_slot, group_name=group_name)
                logger.debug(
                    "apply retry %s/%s: clicked 予約申込へ=%s; waiting result",
                    attempt,
                    max_retries,
                    clicked,
                )
                if clicked:
                    outcome = await wait_after_yoyaku_moshikomi(page)
                    logger.debug(
                        "apply retry %s/%s: wait outcome=%s",
                        attempt,
                        max_retries,
                        outcome,
                    )
            else:
                logger.debug(
                    "apply retry %s/%s: select cells failed after available=True",
                    attempt,
                    max_retries,
                )
        elif on_time_select and not available:
            logger.debug(
                "apply retry %s/%s: target slots not available yet",
                attempt,
                max_retries,
            )

        if outcome == "login" or _is_login_url(page.url):
            logger.debug(
                "apply retry %s/%s: session expired after apply click",
                attempt,
                max_retries,
            )
            raise RuntimeError(MSG_SESSION_EXPIRED)

        if outcome == "proceed" or await _can_click_yoyaku_input(page):
            logger.debug(
                "apply retry %s/%s: success → 予約入力に進む ready",
                attempt,
                max_retries,
            )
            return

        hint_after = outcome == "unavailable" or await _page_has_unavailable_hint(
            page
        )
        disabled_after = await _yoyaku_button_disabled(page)
        still_time_select = "/facility/time-select" in (page.url or "")
        branch_a = still_time_select and (
            hint_after or disabled_after or not available or outcome in ("", "timeout")
        )
        logger.debug(
            "apply retry %s/%s: branch_a=%s hint=%s btn_disabled=%s still_time_select=%s",
            attempt,
            max_retries,
            branch_a,
            hint_after,
            disabled_after,
            still_time_select,
        )

        if attempt >= max_retries:
            raise RuntimeError(MSG_APPLY_RETRY_EXHAUSTED)

        _step_log(
            group_id,
            venue_name,
            f"再試行 {attempt}/{max_retries}: まだ予約不可",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        await page.wait_for_timeout(int(interval_s * 1000))
        logger.debug(
            "apply retry %s/%s: refreshing time-select after %.1fs wait",
            attempt,
            max_retries,
            interval_s,
        )
        await _navigate_to_time_select(
            page, venue_name, sid, day, group_id=group_id,
                                date_s=date_s, time_slot=time_slot, group_name=group_name)

    raise RuntimeError(MSG_APPLY_RETRY_EXHAUSTED)


async def _prepare_group_login(
    page: Page,
    group_id: str,
    password: str,
    *,
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    """Select 団体 login radio, then fill #field-id / #field-password precisely."""
    _step_log(group_id, venue_name, "団体としてログイン選択",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    group_radio = page.locator('input[type="radio"][name="userType"][value="2"]')
    await group_radio.wait_for(state="attached", timeout=_TIMEOUT_PEAK_MS)
    if not await group_radio.is_checked():
        label = page.locator("label.login-radio-item").filter(
            has_text="団体としてログイン"
        )
        if await label.count():
            await _dom_click_locator(label.first)
        else:
            await group_radio.evaluate("el => el.click()")
        try:
            await page.wait_for_function(
                """() => {
                  const el = document.querySelector(
                    'input[type=\"radio\"][name=\"userType\"][value=\"2\"]'
                  );
                  return !!(el && el.checked);
                }""",
                timeout=_TIMEOUT_LOCAL_MS,
            )
        except Exception as exc:
            raise RuntimeError("failed to select 団体としてログイン radio") from exc
    if not await group_radio.is_checked():
        raise RuntimeError("failed to select 団体としてログイン radio")

    _step_log(group_id, venue_name, "ID/パスワード入力中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    user = page.locator("#field-id")
    pwd = page.locator("#field-password")
    await user.wait_for(state="visible", timeout=_TIMEOUT_PEAK_MS)
    await pwd.wait_for(state="visible", timeout=_TIMEOUT_PEAK_MS)
    await user.fill(group_id)
    await pwd.fill(password)


async def _submit_login(
    page: Page,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    _step_log(group_id, venue_name, "ログインボタンクリック、結果待機中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    btn = page.locator("button.login-page-button[type='submit']")
    if await btn.count() == 0:
        btn = page.locator("button[type='submit']").filter(has_text="ログイン")
    await _dom_click_locator(btn.first)

    try:
        handle = await page.wait_for_function(
            """() => {
              const t = document.body.innerText || '';
              if (t.includes('メニューへ戻る')) return 'ok';
              if (!location.href.includes('/login')) return 'ok';
              if (
                t.includes('正しくありません')
                || t.includes('パスワードが違')
                || t.includes('ログインに失敗')
                || t.includes('認証に失敗')
                || t.includes('無効な')
                || (t.includes('エラー') && (t.includes('パスワード') || t.includes('ログイン')))
              ) return 'ng';
              return null;
            }""",
            timeout=_TIMEOUT_PEAK_MS,
        )
        kind = await handle.json_value()
    except Exception as exc:
        _step_log(group_id, venue_name, f"ログイン失敗: {exc}",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        raise RuntimeError("login failed") from exc

    if kind == "ok":
        _step_log(group_id, venue_name, "ログイン成功",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
        return
    _step_log(group_id, venue_name, "ログイン失敗: ページに拒否されました",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    raise RuntimeError("login rejected by page")


async def login_as_group(
    page: Page,
    group_id: str,
    password: str,
    *,
    group_name: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    wait_for_nine: bool = False,
    wait_9am: bool = True,
) -> None:
    """Open login if needed, fill 団体 credentials, submit.

    Shared by book_one and calendar reservation sync.
    """
    base = _base_url()
    _step_log(
        group_id,
        venue_name,
        "ログイン開始",
        date_s=date_s,
        time_slot=time_slot,
        group_name=group_name,
    )
    if not _is_login_url(page.url):
        link = page.get_by_role("link", name=re.compile("ログイン"))
        if await link.count():
            await _dom_click_locator(link.first)
        else:
            await page.goto(
                f"{base}/login",
                wait_until="domcontentloaded",
                timeout=_TIMEOUT_PEAK_MS,
            )
        await _wait_ready(page)
    await _prepare_group_login(
        page,
        group_id,
        password,
        venue_name=venue_name,
        date_s=date_s,
        time_slot=time_slot,
        group_name=group_name,
    )
    if wait_for_nine and wait_9am:
        await asyncio.to_thread(
            wait_until_target,
            9,
            0,
            group_id=group_id,
            venue_name=venue_name,
            date_s=date_s,
            time_slot=time_slot,
            group_name=group_name,
        )
    elif wait_for_nine and not wait_9am:
        logger.debug("skip wait_until_target (wait_9am=False)")
    await _submit_login(
        page,
        group_id=group_id,
        venue_name=venue_name,
        date_s=date_s,
        time_slot=time_slot,
        group_name=group_name,
    )


async def _wait_input_form(page: Page) -> None:
    """After 予約入力に進む: wait for purpose select or input URL."""
    await page.wait_for_function(
        """() => {
          if ((location.href || '').includes('/reservations/create/input')) return true;
          const sel = document.querySelector('select');
          return !!(sel && (sel.offsetWidth || sel.offsetHeight || sel.getClientRects().length));
        }""",
        timeout=_TIMEOUT_PEAK_MS,
    )


async def _wait_confirm_page(page: Page) -> None:
    """After 確認へ: wait for confirm page markers."""
    await page.wait_for_function(
        """() => {
          const t = document.body.innerText || '';
          if (t.includes('予約内容確認')) return true;
          const btns = Array.from(document.querySelectorAll('button'));
          return btns.some((b) =>
            (b.innerText || '').includes('確定して全体の確認へ進む')
            || (b.innerText || '').includes('この予約申込を確定して全体の確認へ進む')
          );
        }""",
        timeout=_TIMEOUT_PEAK_MS,
    )


async def _wait_final_confirm_page(page: Page) -> None:
    """After 全体の確認へ進む: wait for final submit button."""
    await page.wait_for_selector(
        "button:has-text('この予約申込を確定する')",
        state="visible",
        timeout=_TIMEOUT_PEAK_MS,
    )


async def _wait_booking_complete(page: Page) -> str:
    """After final 確定する: return ok | ng | timeout."""
    try:
        handle = await page.wait_for_function(
            """() => {
              const href = location.href || '';
              const t = document.body.innerText || '';
              if (href.includes('/complete') || t.includes('予約申込が完了しました')) {
                return 'ok';
              }
              if (
                t.includes('予約に失敗')
                || t.includes('申込に失敗')
                || t.includes('エラーが発生')
                || t.includes('正しくありません')
                || t.includes('予約できませんでした')
                || t.includes('処理に失敗')
              ) {
                return 'ng';
              }
              return null;
            }""",
            timeout=_TIMEOUT_PEAK_MS,
        )
        return str(await handle.json_value())
    except Exception:
        return "timeout"


def _slot_available_via_api(
    client: httpx.Client,
    *,
    fid: int,
    sid: int,
    day: date,
    time_slot: str,
) -> bool:
    from core.scanner import _area_blocks, helper_7

    base = _base_url()
    tenant = _tenant()
    fac = json.dumps(
        [
            {
                "facilityId": fid,
                "spaces": [{"spaceId": sid, "selectedDates": [day.isoformat()]}],
            }
        ],
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
    url = f"{base}/api/{tenant}/public/facility/spaceTime"
    r = client.get(url, params={"facilities": fac, "searchData": search}, timeout=30.0)
    r.raise_for_status()
    data = r.json().get("data") or []
    if not data:
        return False
    row0 = data[0] or {}
    spaces = row0.get("spaces") or []
    if not spaces:
        return False
    space0 = spaces[0] or {}
    areas_raw = space0.get("areas") or row0.get("areas") or []
    ablocks = _area_blocks(list(areas_raw))
    table = list(space0.get("timeTable") or [])
    want = time_slot.strip()
    for cell in table:
        tstr = str(cell.get("timeString", "")).strip()
        if tstr != want and tstr.replace("～", "-") != want.replace("～", "-"):
            # also accept zero-padded variants already normalized in tasks
            if tstr.replace(" ", "") != want.replace(" ", ""):
                continue
        details = cell.get("details") or []
        if helper_7(list(details), ablocks):
            return True
    return False


def _poll_slot_available(
    client: httpx.Client,
    *,
    fid: int,
    sid: int,
    day: date,
    time_slot: str,
    timeout_s: float = _POLL_TIMEOUT_S,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if _slot_available_via_api(
                client, fid=fid, sid=sid, day=day, time_slot=time_slot
            ):
                return True
        except Exception:
            logger.debug("spaceTime poll failed", exc_info=True)
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_S)


async def _fill_purpose_and_headcount(
    page: Page,
    headcount: int,
    *,
    group_id: str = "",
    venue_name: str = "",
    date_s: str = "",
    time_slot: str = "",
    group_name: str = "",
) -> None:
    _step_log(
        group_id,
        venue_name,
        f"予約入力画面へ、利用目的選択中: {_PURPOSE}",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    sel = page.locator("select").first
    await sel.wait_for(state="visible", timeout=_TIMEOUT_PEAK_MS)
    options = await sel.locator("option").all()
    value = None
    for opt in options:
        text = (await opt.inner_text()).strip()
        if text == _PURPOSE:
            value = await opt.get_attribute("value")
            break
    if value is None:
        raise RuntimeError(f"purpose option not found: {_PURPOSE}")
    await sel.select_option(value=value)

    _step_log(group_id, venue_name, f"利用人数入力: {headcount}",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    num = page.locator(
        "input[type='number'], input[name*='person' i], input[name*='人数']"
    ).first
    if await num.count() == 0:
        # fallback: labeled field
        num = page.get_by_label(re.compile("人数"))
    await num.fill(str(headcount))
    _step_log(group_id, venue_name, "確認画面へ",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
    await _click_text(page, "確認へ")
    await _wait_confirm_page(page)


async def book_one(
    task: dict[str, Any],
    *,
    venues: dict[str, dict[str, Any]] | None = None,
    names: dict[str, str] | None = None,
    headless: bool = True,
    wait_9am: bool = True,
    stop_at_time_select: bool = False,
    headcount: int | None = None,
) -> dict[str, Any]:
    """Run a single booking task in its own browser context.

    Never raises to the caller: all failures become BookingResult(success=False).
    """
    venue_code = str(task.get("venue_code") or "")
    date_s = str(task.get("date") or "")
    slots = task_time_slots(task)
    time_slot = display_time_range(slots)
    group_id = str(task.get("group_id") or "")
    group_name = str(task.get("group_name") or "")
    password = str(task.get("password") or "").strip()
    people = int(task.get("headcount") or headcount or _DEFAULT_HEADCOUNT)
    login_first = False
    venue_name = venue_code or "-"
    open_wait = is_open_wait_mode()
    mode_label = "open_wait" if open_wait else "immediate"

    def _fail(msg: str | BaseException, **extra: Any) -> dict[str, Any]:
        err = user_error_message(msg, log_detail=True)
        _step_log(
            group_id,
            venue_name,
            f"予約失敗: {err} ({date_s} {time_slot})",
            date_s=date_s,
            time_slot=time_slot,
            group_name=group_name,
        )
        payload = dict(extra or {})
        payload.setdefault("mode", mode_label)
        return _result(
            success=False,
            venue_code=venue_code,
            venue_name=venue_name,
            date_s=date_s,
            time_slot=time_slot,
            group_id=group_id,
            group_name=group_name,
            error_message=err,
            extra=payload or None,
        )

    try:
        venues = venues if venues is not None else load_venues()
        names = names if names is not None else load_names()
        meta = venues.get(venue_code)
        venue_name = names.get(venue_code, venue_code or "-")
        if not meta:
            return _fail("unknown venue_code")
        if not slots:
            return _fail("time_slots empty")
        if not password and not stop_at_time_select:
            return _fail("password missing for group")

        fid = int(meta["fid"])
        sid = int(meta["sid"])
        venue_type = str(meta["type"])
        day = date.fromisoformat(date_s[:10])
        base = _base_url()
        if not base or not _tenant():
            return _fail("CFG_B1 / CFG_B2 missing")

        _step_log(
            group_id,
            venue_name,
            f"予約開始: {date_s} {time_slot}",
            date_s=date_s,
            time_slot=time_slot,
            group_name=group_name,
        )
        logger.debug("booking mode=%s wait_9am=%s", mode_label, wait_9am)

        # Test hook: first matching call raises once (see BOOK_INJECT_ERROR).
        if os.getenv("BOOK_INJECT_ERROR", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            os.environ.pop("BOOK_INJECT_ERROR", None)
            raise RuntimeError("injected unexpected error for test")

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(headless=headless)
            try:
                context: BrowserContext = await browser.new_context(
                    locale="ja-JP",
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                dialog_blocked = False

                async def _on_dialog(dialog: Any) -> None:
                    nonlocal dialog_blocked
                    dialog_blocked = True
                    try:
                        await dialog.dismiss()
                    except Exception:
                        try:
                            await dialog.accept()
                        except Exception:
                            pass

                page.on("dialog", _on_dialog)

                try:
                    await _goto_home(page)

                    async def _do_login(*, wait_for_nine: bool) -> None:
                        await login_as_group(
                            page,
                            group_id,
                            password,
                            group_name=group_name,
                            venue_name=venue_name,
                            date_s=date_s,
                            time_slot=time_slot,
                            wait_for_nine=wait_for_nine,
                            wait_9am=wait_9am,
                        )

                    async def _browse_to_calendar() -> None:
                        await _search_and_select_space(
                            page,
                            venue_name,
                            sid,
                            group_id=group_id,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_name=group_name,
                        )
                        await _calendar_pick_date(
                            page,
                            day,
                            group_id=group_id,
                            venue_name=venue_name,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_name=group_name,
                        )

                    async def _wait_time_select() -> None:
                        await page.wait_for_url(
                            re.compile(r"/facility/time-select"),
                            timeout=_TIMEOUT_PEAK_MS,
                        )
                        await _wait_ready(page)

                    async def _apply_once() -> None:
                        for slot in slots:
                            ok = await _select_time_cell(
                                page,
                                slot,
                                venue_type,
                                group_id=group_id,
                                venue_name=venue_name,
                                date_s=date_s,
                                slot_range=time_slot,
                                group_name=group_name,
                            )
                            if not ok:
                                raise RuntimeError(MSG_SLOT_UNAVAILABLE)
                        if not await _click_yoyaku_moshikomi(
                            page,
                            group_id=group_id,
                            venue_name=venue_name,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_name=group_name,
                        ):
                            raise RuntimeError(MSG_SLOT_UNAVAILABLE)
                        outcome = await wait_after_yoyaku_moshikomi(page)
                        logger.debug("immediate apply outcome=%s", outcome)
                        if outcome == "unavailable" or await _page_has_unavailable_hint(
                            page
                        ):
                            raise RuntimeError(MSG_SLOT_UNAVAILABLE)
                        if outcome == "login" or _is_login_url(page.url):
                            raise RuntimeError(MSG_SESSION_EXPIRED)
                        if outcome == "proceed" or await _can_click_yoyaku_input(
                            page
                        ):
                            return
                        if outcome == "timeout":
                            raise RuntimeError(MSG_CONFIRM_UNCLEAR)
                        raise RuntimeError(MSG_SLOT_UNAVAILABLE)

                    if open_wait:
                        # Branch A: anonymous browse → wait 9:00 → login → retry loop
                        await _browse_to_calendar()

                        if _is_login_url(page.url):
                            logger.debug(
                                "ANON_BLOCKED: redirected to /login before "
                                "time-select; switching to login-first flow"
                            )
                            login_first = True
                            await _do_login(wait_for_nine=True)
                            await _goto_home(page)
                            await _browse_to_calendar()

                        await _wait_time_select()

                        if dialog_blocked:
                            raise SiteDialogBlocked()

                        if stop_at_time_select:
                            return _result(
                                success=True,
                                venue_code=venue_code,
                                venue_name=venue_name,
                                date_s=date_s,
                                time_slot=time_slot,
                                group_id=group_id,
                                group_name=group_name,
                                error_message="",
                                extra={
                                    "probe": True,
                                    "mode": mode_label,
                                    "branch": (
                                        "login_first"
                                        if login_first
                                        else "anonymous"
                                    ),
                                    "url": page.url,
                                },
                            )

                        if not login_first:
                            probe_available = await _time_slots_available_on_page(
                                page, slots, venue_type
                            )
                            probe_outcome = ""
                            if probe_available:
                                for slot in slots:
                                    await _select_time_cell(
                                        page,
                                        slot,
                                        venue_type,
                                        group_id=group_id,
                                        venue_name=venue_name,
                                        date_s=date_s,
                                        slot_range=time_slot,
                                        group_name=group_name,
                                    )
                                if await _click_yoyaku_moshikomi(
                                    page,
                                    group_id=group_id,
                                    venue_name=venue_name,
                                    date_s=date_s,
                                    time_slot=time_slot,
                                    group_name=group_name,
                                ):
                                    probe_outcome = (
                                        await wait_after_yoyaku_moshikomi(page)
                                    )
                                    logger.debug(
                                        "login-gate probe outcome=%s",
                                        probe_outcome,
                                    )
                            need_login = (
                                probe_outcome == "login"
                                or _is_login_url(page.url)
                                or bool(
                                    await page.get_by_text(
                                        "団体としてログイン", exact=False
                                    ).count()
                                )
                            )
                            if need_login:
                                await _do_login(wait_for_nine=True)
                                resumed = await _try_resume_after_login(
                                    page,
                                    group_id=group_id,
                                    venue_name=venue_name,
                                    date_s=date_s,
                                    time_slot=time_slot,
                                    group_name=group_name,
                                )
                                if not resumed:
                                    _step_log(
                                        group_id,
                                        venue_name,
                                        "選択状態が消えたため再選択します",
                                        date_s=date_s,
                                        time_slot=time_slot,
                                        group_name=group_name,
                                    )
                                    await _navigate_to_time_select(
                                        page,
                                        venue_name,
                                        sid,
                                        day,
                                        group_id=group_id,
                                        date_s=date_s,
                                        time_slot=time_slot,
                                        group_name=group_name,
                                    )

                        if dialog_blocked:
                            raise SiteDialogBlocked()

                        await apply_for_slots_with_retry(
                            page,
                            slots=slots,
                            venue_type=venue_type,
                            venue_name=venue_name,
                            sid=sid,
                            day=day,
                            group_id=group_id,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_name=group_name,
                            max_retries=_APPLY_RETRY_MAX,
                            interval_s=_APPLY_RETRY_INTERVAL_S,
                        )
                    else:
                        # Branch B: login first → settle slot status → one-shot apply
                        login_first = True
                        await _do_login(wait_for_nine=False)
                        await _goto_home(page)
                        await _browse_to_calendar()
                        await _wait_time_select()

                        if dialog_blocked:
                            raise SiteDialogBlocked()

                        if stop_at_time_select:
                            return _result(
                                success=True,
                                venue_code=venue_code,
                                venue_name=venue_name,
                                date_s=date_s,
                                time_slot=time_slot,
                                group_id=group_id,
                                group_name=group_name,
                                error_message="",
                                extra={
                                    "probe": True,
                                    "mode": mode_label,
                                    "branch": "login_first",
                                    "url": page.url,
                                },
                            )

                        settled = await wait_for_slots_settled(
                            page, slots, venue_type, timeout_ms=_TIMEOUT_PEAK_MS
                        )
                        logger.debug("slot settled status=%s", settled)
                        if settled == "unavailable":
                            return _fail(MSG_SLOT_UNAVAILABLE)
                        if settled == "timeout":
                            return _fail(MSG_CONFIRM_UNCLEAR)
                        await _apply_once()

                    if dialog_blocked:
                        raise SiteDialogBlocked()

                    btn = page.locator("button:has-text('予約入力に進む')")
                    if await btn.count():
                        await _dom_click_locator(btn.first)
                        await _wait_input_form(page)

                    if os.getenv("BOOK_STOP_AFTER_INPUT", "").strip().lower() in (
                        "1",
                        "true",
                        "yes",
                    ):
                        os.environ.pop("BOOK_STOP_AFTER_INPUT", None)
                        _step_log(
                            group_id,
                            venue_name,
                            "テスト停止: 予約入力画面到達",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
                        return _result(
                            success=True,
                            venue_code=venue_code,
                            venue_name=venue_name,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_id=group_id,
                            group_name=group_name,
                            extra={
                                "stopped_after_input": True,
                                "branch": (
                                    "login_first"
                                    if login_first
                                    else "anonymous"
                                ),
                            },
                        )

                    if dialog_blocked:
                        raise SiteDialogBlocked()

                    if (
                        "/reservations/create/input" in page.url
                        or await page.locator("select").count()
                    ):
                        await _fill_purpose_and_headcount(
                            page,
                            people,
                            group_id=group_id,
                            venue_name=venue_name,
                                date_s=date_s, time_slot=time_slot, group_name=group_name)
                        await _wait_ready(page)

                    overall_btn = page.locator(
                        "button:has-text('この予約申込を確定して全体の確認へ進む')"
                    )
                    if await overall_btn.count() == 0:
                        overall_btn = page.locator(
                            "button:has-text('確定して全体の確認へ進む')"
                        )
                    if await overall_btn.count():
                        _step_log(group_id, venue_name, "全体の確認画面へ",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
                        await _dom_click_locator(overall_btn.first)
                        await _wait_final_confirm_page(page)

                    if dialog_blocked:
                        raise SiteDialogBlocked()

                    final_btn = page.locator(
                        "button:has-text('この予約申込を確定する')"
                    )
                    if await final_btn.count() == 0:
                        raise RuntimeError(
                            "final confirm button not found: "
                            "この予約申込を確定する"
                        )
                    _step_log(
                        group_id,
                        venue_name,
                        "予約確定ボタンクリック、結果待機中",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
                    await _dom_click_locator(final_btn.first)
                    complete_status = await _wait_booking_complete(page)
                    logger.debug(
                        "final confirm wait status=%s dialog=%s",
                        complete_status,
                        dialog_blocked,
                    )

                    if dialog_blocked:
                        raise SiteDialogBlocked()
                    if complete_status == "ok":
                        _step_log(
                            group_id,
                            venue_name,
                            f"予約成功: {date_s} {time_slot}",
        date_s=date_s, time_slot=time_slot, group_name=group_name)
                        return _result(
                            success=True,
                            venue_code=venue_code,
                            venue_name=venue_name,
                            date_s=date_s,
                            time_slot=time_slot,
                            group_id=group_id,
                            group_name=group_name,
                            extra={
                                "branch": (
                                    "login_first"
                                    if login_first
                                    else "anonymous"
                                )
                            },
                        )
                    if complete_status == "ng":
                        return _fail(
                            "予約確定に失敗しました（画面上のエラーをご確認ください）",
                            branch=(
                                "login_first" if login_first else "anonymous"
                            ),
                        )
                    return _fail(
                        MSG_CONFIRM_UNCLEAR,
                        branch=(
                            "login_first" if login_first else "anonymous"
                        ),
                    )
                except Exception as exc:
                    logger.debug(
                        "book_one failed code=%s", venue_code, exc_info=True
                    )
                    return _fail(
                        SiteDialogBlocked() if dialog_blocked else exc,
                        branch="login_first" if login_first else "anonymous",
                    )
                finally:
                    await context.close()
            finally:
                await browser.close()
    except Exception as exc:
        logger.debug(
            "book_one outer failed code=%s", venue_code, exc_info=True
        )
        return _fail(exc)

    return _fail("book_one ended without result")


async def run_tasks(
    tasks: list[dict[str, Any]] | None = None,
    *,
    headless: bool = True,
    wait_9am: bool = True,
    stop_at_time_select: bool = False,
) -> list[dict[str, Any]]:
    """Run every task concurrently (one Playwright context each); no group queue."""
    try:
        tasks = tasks if tasks is not None else load_tasks()
    except Exception as exc:
        logger.debug("load_tasks failed", exc_info=True)
        return [
            _result(
                success=False,
                venue_code="-",
                venue_name="-",
                date_s="",
                time_slot="",
                group_id="",
                error_message=user_error_message(
                    f"load_tasks failed: {exc}", log_detail=True
                ),
            )
        ]

    try:
        venues = load_venues()
        names = load_names()
    except Exception as exc:
        logger.debug("load venues/names failed", exc_info=True)
        return [
            _fail_from_task(t, f"config load failed: {exc}")
            for t in (tasks or [{"venue_code": "-"}])
        ]

    async def _safe_book(t: dict[str, Any]) -> dict[str, Any]:
        try:
            return await book_one(
                t,
                venues=venues,
                names=names,
                headless=headless,
                wait_9am=wait_9am,
                stop_at_time_select=stop_at_time_select,
            )
        except Exception as exc:
            logger.debug(
                "book_one escaped code=%s",
                t.get("venue_code"),
                exc_info=True,
            )
            return _fail_from_task(t, exc, names=names)

    try:
        bundles = await asyncio.gather(
            *[_safe_book(t) for t in tasks],
            return_exceptions=True,
        )
    except Exception as exc:
        logger.debug("run_tasks gather failed", exc_info=True)
        return [_fail_from_task(t, exc, names=names) for t in tasks]

    results: list[dict[str, Any]] = []
    for t, bundle in zip(tasks, bundles):
        if isinstance(bundle, BaseException):
            logger.debug(
                "task %s failed: %s",
                t.get("venue_code"),
                bundle,
                exc_info=bundle,
            )
            results.append(_fail_from_task(t, bundle, names=names))
        else:
            results.append(bundle)
    return results


def _mail_date_jp(date_s: str) -> str:
    try:
        d = date.fromisoformat(str(date_s)[:10])
    except ValueError:
        return str(date_s or "")
    return f"{d.year}年{d.month:02d}月{d.day:02d}日"


def _mail_time_jp(time_slot: str) -> str:
    """Normalize time range to HH:MM～HH:MM (fullwidth wave dash)."""
    s = str(time_slot or "").strip().replace("～", "-").replace("~", "-")
    parts = re.split(r"\s*-\s*", s)
    if len(parts) != 2:
        return s.replace("-", "～")
    return f"{parts[0].strip()}～{parts[1].strip()}"


def _mail_group_label(group_id: str, group_name: str) -> str:
    gid = str(group_id or "")
    name = str(group_name or "").strip()
    if not name:
        name = "未登録"
    return f"{gid} ({name})"


def format_results_mail(results: list[dict[str, Any]], when: date | None = None) -> tuple[str, str]:
    d = when or datetime.now(TZ).date()
    subject = f"自動予約結果({d.month}/{d.day})"
    oks = [r for r in results if r.get("success")]
    ngs = [r for r in results if not r.get("success")]
    lines = [
        f"合計: {len(oks) + len(ngs)}件",
        f"✅ 成功：{len(oks)}件",
        f"❌ 失敗：{len(ngs)}件",
        "",
    ]
    if oks:
        lines.append("✅ 成功")
        for i, r in enumerate(oks, start=1):
            lines.append(f"【{i}】")
            lines.append(
                f"  👤 団体: {_mail_group_label(str(r.get('group_id') or ''), str(r.get('group_name') or ''))}"
            )
            lines.append(f"  📍 会館: {r.get('venue_name') or r.get('venue_code') or ''}")
            lines.append(f"  📅 予約日: {_mail_date_jp(str(r.get('date') or ''))}")
            lines.append(f"  🕒 時間帯: {_mail_time_jp(str(r.get('time_slot') or ''))}")
            lines.append("")
    if ngs:
        lines.append("❌ 失敗")
        for i, r in enumerate(ngs, start=1):
            lines.append(f"【{i}】")
            lines.append(
                f"  👤 団体: {_mail_group_label(str(r.get('group_id') or ''), str(r.get('group_name') or ''))}"
            )
            lines.append(f"  📍 会館: {r.get('venue_name') or r.get('venue_code') or ''}")
            lines.append(f"  📅 予約日: {_mail_date_jp(str(r.get('date') or ''))}")
            lines.append(f"  🕒 時間帯: {_mail_time_jp(str(r.get('time_slot') or ''))}")
            lines.append(f"  ⚠️ エラー: {r.get('error_message') or ''}")
            lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    return subject, body


def send_results_mail(results: list[dict[str, Any]], when: date | None = None) -> bool:
    from core.notifier import send_text_msg

    subject, body = format_results_mail(results, when=when)
    try:
        return bool(send_text_msg(subject, body))
    except Exception:
        logger.debug("send_results_mail failed", exc_info=True)
        return False
