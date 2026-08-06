"""HTTP helpers and local state for daily task."""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import jpholiday

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PATH_T1 = DATA_DIR / "t1.json"
PATH_T2 = DATA_DIR / "t2.json"
PATH_SUP = DATA_DIR / "suppressed.json"
PATH_FAIL = DATA_DIR / "fail_count.json"
PATH_DAILY_SENT = DATA_DIR / "daily_sent.json"
PATH_LOG = DATA_DIR / "scan.log"
PATH_BOOKING_LOG = DATA_DIR / "booking.log"
PATH_LOTTERY_LOG = DATA_DIR / "lottery.log"
PATH_CHANGES_LOG = DATA_DIR / "changes.log"
PATH_CFG = ROOT / "config" / "cfg_items.json"

TZ_TOKYO = ZoneInfo("Asia/Tokyo")
DELTA_T = timedelta(hours=4)
CUT_H = 15
BOUND_T = "19:00"
_ST_OK = "available"
_MAX_TRY = 3
_WAIT_S = 2.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_LOG_READY = False
_BOOKING_LOG_READY = False
_LOTTERY_LOG_READY = False
_CHANGES_LOG_READY = False
_LOG_KEEP_DAYS = 3
_LOTTERY_LOG_KEEP_DAYS = 30
_CHANGES_LOG_KEEP_DAYS = 90
_LOG_LINE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_LOG_FMT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
changes_logger = logging.getLogger("core.scanner.changes")


def prune_log(
    path: Path | None = None,
    *,
    keep_days: int = _LOG_KEEP_DAYS,
    now: datetime | None = None,
) -> None:
    """Keep only lines dated within the last keep_days calendar days (Tokyo)."""
    p = path or PATH_LOG
    if not p.exists() or keep_days < 1:
        return
    cur = now if now is not None else now_tokyo()
    if cur.tzinfo is not None:
        cur = cur.astimezone(TZ_TOKYO).replace(tzinfo=None)
    cutoff = cur.date() - timedelta(days=keep_days - 1)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if not text:
        return
    kept: list[str] = []
    keeping = False
    for line in text.splitlines(keepends=True):
        m = _LOG_LINE_DATE.match(line)
        if m:
            try:
                d = date.fromisoformat(m.group(1))
            except ValueError:
                if keeping:
                    kept.append(line)
                continue
            keeping = d >= cutoff
            if keeping:
                kept.append(line)
            continue
        if keeping:
            kept.append(line)
    new_text = "".join(kept)
    if new_text == text:
        return
    try:
        p.write_text(new_text, encoding="utf-8")
    except OSError:
        logger.warning("log prune write failed: %s", p)


def _ensure_console_logging(level: int = logging.INFO) -> None:
    """Shared StreamHandler on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)
    has_stream = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler()
        sh.setFormatter(_LOG_FMT)
        sh.setLevel(level)
        root.addHandler(sh)


def _detach_root_file_handler(path: Path) -> None:
    """Remove legacy root FileHandlers that wrote into path (migration)."""
    root = logging.getLogger()
    try:
        resolved = path.resolve()
    except OSError:
        return
    for h in list(root.handlers):
        if not isinstance(h, logging.FileHandler):
            continue
        try:
            if Path(getattr(h, "baseFilename", "")).resolve() == resolved:
                root.removeHandler(h)
                h.close()
        except OSError:
            continue


class _BookingStepFileFilter(logging.Filter):
    """Pass DEBUG always; INFO+ only when marked booking_step."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return True
        return bool(getattr(record, "booking_step", False))


def _attach_named_file_logger(
    logger_name: str,
    path: Path,
    *,
    level: int = logging.INFO,
    booking_step_filter: bool = False,
) -> None:
    """Attach a FileHandler to a named logger if not already present."""
    log = logging.getLogger(logger_name)
    log.setLevel(level)
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for h in log.handlers:
        if isinstance(h, logging.FileHandler):
            try:
                if Path(getattr(h, "baseFilename", "")).resolve() == resolved:
                    return
            except OSError:
                continue
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(_LOG_FMT)
    fh.setLevel(level)
    if booking_step_filter:
        fh.addFilter(_BookingStepFileFilter())
    log.addHandler(fh)


def setup_logging(level: int = logging.INFO) -> None:
    """Console + core.scanner → data/scan.log; changes → data/changes.log."""
    global _LOG_READY, _CHANGES_LOG_READY
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_console_logging(level)
    _detach_root_file_handler(PATH_LOG)
    _detach_root_file_handler(PATH_CHANGES_LOG)
    if not _LOG_READY:
        prune_log(PATH_LOG)
        _attach_named_file_logger("core.scanner", PATH_LOG, level=level)
        # Quiet noisy HTTP client chatter at INFO
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("googleapiclient").setLevel(logging.WARNING)
        logging.getLogger("google_auth_httplib2").setLevel(logging.WARNING)
        _LOG_READY = True
    if not _CHANGES_LOG_READY:
        prune_log(PATH_CHANGES_LOG, keep_days=_CHANGES_LOG_KEEP_DAYS)
        changes_logger.propagate = False
        _attach_named_file_logger(
            "core.scanner.changes", PATH_CHANGES_LOG, level=level
        )
        _CHANGES_LOG_READY = True


def setup_booking_logging(level: int = logging.INFO) -> None:
    """Console + core.booking → data/booking.log (idempotent)."""
    global _BOOKING_LOG_READY
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_console_logging(level)
    if _BOOKING_LOG_READY:
        return
    prune_log(PATH_BOOKING_LOG)
    _attach_named_file_logger(
        "core.booking",
        PATH_BOOKING_LOG,
        level=level,
        booking_step_filter=True,
    )
    _BOOKING_LOG_READY = True


def setup_lottery_logging(level: int = logging.INFO) -> None:
    """Console + core.lottery → data/lottery.log (idempotent)."""
    global _LOTTERY_LOG_READY
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_console_logging(level)
    if _LOTTERY_LOG_READY:
        return
    prune_log(PATH_LOTTERY_LOG, keep_days=_LOTTERY_LOG_KEEP_DAYS)
    _attach_named_file_logger("core.lottery", PATH_LOTTERY_LOG, level=level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _LOTTERY_LOG_READY = True


def send_heartbeat(
    ping_url: str | None = None,
    *,
    env_key: str = "CFG_D1",
) -> None:
    """GET healthcheck URL. Never raises; skips when URL unset."""
    url = (
        ping_url if ping_url is not None else os.getenv(env_key, "")
    ).strip()
    if not url:
        logger.debug("heartbeat未配置，跳过 (%s)", env_key)
        return
    try:
        httpx.get(url, timeout=10.0)
    except Exception as e:
        logger.warning("heartbeat ping failed: %s", e)


def now_tokyo() -> datetime:
    """Wall-clock now in Asia/Tokyo (naive, for stamp compatibility)."""
    return datetime.now(TZ_TOKYO).replace(tzinfo=None)


def today_tokyo() -> date:
    return now_tokyo().date()


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
        if today.tzinfo is not None:
            cur = today.astimezone(TZ_TOKYO).replace(tzinfo=None)
        else:
            cur = today
        day = cur.date()
    else:
        cur = now_tokyo()
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


def helper_7(details: list[dict[str, Any]], area_blocks: dict[Any, list[Any]]) -> bool:
    """True if any detail with blocks length == 1 has status == _ST_OK."""
    for det in details:
        if not isinstance(det, dict):
            continue
        aid = det.get("areaId")
        blocks = None
        if aid in area_blocks:
            blocks = area_blocks[aid]
        elif aid is not None and str(aid) in area_blocks:
            blocks = area_blocks[str(aid)]
        if not isinstance(blocks, list) or len(blocks) != 1:
            continue
        if det.get("status") == _ST_OK:
            return True
    return False


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
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    raw = json.loads(text)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _save_map(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def _load_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    raw = json.loads(text)
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def _save_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False, indent=2)


def load_daily_sent_date(path: Path | None = None) -> str | None:
    """Return last_sent_date (YYYY-MM-DD) or None if missing/empty."""
    p = path or PATH_DAILY_SENT
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return None
    raw = json.loads(text)
    if not isinstance(raw, dict):
        return None
    val = str(raw.get("last_sent_date") or "").strip()
    return val or None


def save_daily_sent_date(day: date | str, path: Path | None = None) -> None:
    """Persist last_sent_date for Tokyo calendar day."""
    p = path or PATH_DAILY_SENT
    if isinstance(day, date):
        s = day.strftime("%Y-%m-%d")
    else:
        s = str(day).strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump({"last_sent_date": s}, f, ensure_ascii=False, indent=2)


def is_daily_first_send(today: date, path: Path | None = None) -> bool:
    """True when daily_sent.json is missing or last_sent_date != today."""
    last = load_daily_sent_date(path)
    return last != today.strftime("%Y-%m-%d")


def load_cfg(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or PATH_CFG
    with p.open(encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("items", raw) if isinstance(raw, dict) else raw
    return list(items)


def _parse_slot_key(key: str) -> tuple[str, str] | None:
    """Return (code, YYYY-MM-DD) from 'code|YYYY-MM-DD HH:MM-HH:MM'."""
    if "|" not in key:
        return None
    code, rest = key.split("|", 1)
    day_s = rest.strip().split(" ", 1)[0][:10]
    try:
        date.fromisoformat(day_s)
    except ValueError:
        return None
    return code.strip(), day_s


def prune_suppressed_past(
    data: dict[str, str],
    *,
    today: date | None = None,
) -> dict[str, str]:
    """Drop suppressed keys whose slot date is strictly before today (Tokyo)."""
    day = today or today_tokyo()
    out: dict[str, str] = {}
    for k, v in data.items():
        parsed = _parse_slot_key(k)
        if parsed is None:
            out[str(k)] = str(v)
            continue
        try:
            slot_day = date.fromisoformat(parsed[1])
        except ValueError:
            out[str(k)] = str(v)
            continue
        if slot_day >= day:
            out[str(k)] = str(v)
    return out


def _load_suppressed(
    path: Path | None = None,
    *,
    today: date | None = None,
) -> dict[str, str]:
    """Load suppressed map and drop past slot keys; rewrite file when pruned."""
    p = path or PATH_SUP
    raw = _load_map(p)
    cleaned = prune_suppressed_past(raw, today=today)
    if cleaned != raw:
        _save_map(p, cleaned)
    return cleaned


def _save_suppressed(
    data: dict[str, str],
    path: Path | None = None,
    *,
    today: date | None = None,
) -> None:
    """Persist suppressed map after dropping past slot keys."""
    p = path or PATH_SUP
    _save_map(p, prune_suppressed_past(data, today=today))


def _key_in_failed(key: str, failed_scope: set[tuple[str, str]]) -> bool:
    parsed = _parse_slot_key(key)
    if not parsed:
        return False
    code, day_s = parsed
    if (code, day_s) in failed_scope:
        return True
    if (code, day_s[:7]) in failed_scope:
        return True
    return False


def proc_b(
    current_keys: set[str],
    now: datetime,
    *,
    path_t1: Path | None = None,
    path_sup: Path | None = None,
    failed_scope: set[tuple[str, str]] | None = None,
) -> None:
    """Update first_seen (t1). Skip keys already in suppressed."""
    p1 = path_t1 or PATH_T1
    suppressed = _load_suppressed(path_sup or PATH_SUP, today=now.date())
    failed = failed_scope or set()
    t1 = _load_map(p1)
    for k in list(t1.keys()):
        if k not in current_keys:
            if _key_in_failed(k, failed):
                continue
            del t1[k]
    stamp = now.isoformat(timespec="seconds")
    for k in current_keys:
        if k in suppressed:
            continue
        if k not in t1:
            t1[k] = stamp
    _save_map(p1, t1)


def proc_promote(
    current_keys: set[str],
    now: datetime,
    *,
    path_t1: Path | None = None,
    path_sup: Path | None = None,
    delta: timedelta | None = None,
) -> set[str]:
    """Move keys present >= delta from t1 into suppressed. Return newly promoted."""
    p1 = path_t1 or PATH_T1
    ps = path_sup or PATH_SUP
    thr = delta if delta is not None else DELTA_T
    t1 = _load_map(p1)
    suppressed = _load_suppressed(ps, today=now.date())
    promoted: set[str] = set()
    stamp = now.isoformat(timespec="seconds")
    for k, raw in list(t1.items()):
        if k not in current_keys:
            continue
        try:
            seen = datetime.fromisoformat(raw)
        except ValueError:
            seen = now - thr
        if now - seen >= thr:
            suppressed[k] = stamp
            del t1[k]
            promoted.add(k)
    _save_map(p1, t1)
    _save_suppressed(suppressed, ps, today=now.date())
    return promoted


def proc_diff(
    current_keys: set[str],
    *,
    path_t2: Path | None = None,
    failed_scope: set[tuple[str, str]] | None = None,
) -> tuple[set[str], set[str], set[str]]:
    """Return (added, removed, old_snapshot). failed_scope keys are not treated as removed."""
    old = set(_load_lines(path_t2 or PATH_T2))
    failed = failed_scope or set()
    added = set(current_keys) - old
    removed = {
        k for k in (old - current_keys) if not _key_in_failed(k, failed)
    }
    return added, removed, old


def should_send(
    added: set[str],
    removed: set[str],
    suppressed: dict[str, str] | set[str],
) -> bool:
    """True if non-suppressed added/removed remain."""
    sup = set(suppressed)
    return bool((added - sup) or (removed - sup))


def proc_f(lines: list[str], *, path_t2: Path | None = None) -> None:
    _save_lines(path_t2 or PATH_T2, lines)


def snapshot_for_t2(
    current_keys: set[str],
    old: set[str],
    failed_scope: set[tuple[str, str]] | None = None,
) -> list[str]:
    """Persist current keys, keeping failed-scope keys from the previous snapshot."""
    failed = failed_scope or set()
    out = set(current_keys)
    for k in old:
        if _key_in_failed(k, failed):
            out.add(k)
    return sorted(out)


def _http_client(**kwargs: Any) -> httpx.Client:
    headers = {"User-Agent": _UA}
    extra = kwargs.pop("headers", None)
    if extra:
        headers = {**headers, **dict(extra)}
    return httpx.Client(headers=headers, **kwargs)


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


def _area_blocks(areas_raw: list[Any]) -> dict[Any, list[Any]]:
    out: dict[Any, list[Any]] = {}
    for a in areas_raw:
        if not isinstance(a, dict):
            continue
        aid = a.get("areaId")
        if aid is None:
            continue
        blocks = a.get("blocks")
        if not isinstance(blocks, list):
            blocks = []
        out[aid] = blocks
        out[str(aid)] = blocks
    return out


def fetch_b(
    client: httpx.Client,
    base: str,
    tenant: str,
    fid: int,
    sid: int,
    days: list[str],
) -> tuple[dict[Any, list[Any]], list[dict[str, Any]]]:
    empty: tuple[dict[Any, list[Any]], list[dict[str, Any]]] = ({}, [])
    if not days:
        return empty
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
        return empty
    row0 = data[0] or {}
    spaces = row0.get("spaces") or []
    if not spaces:
        return empty
    space0 = spaces[0] or {}
    areas_raw = space0.get("areas") or row0.get("areas") or []
    ablocks = _area_blocks(list(areas_raw))
    table = list(space0.get("timeTable") or [])
    return ablocks, table


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def collect_one(
    client: httpx.Client,
    base: str,
    tenant: str,
    item: dict[str, Any],
    start: date,
    end: date,
) -> tuple[set[str], set[tuple[str, str]]]:
    code = str(item["code"])
    fid = int(item["fid"])
    sid = int(item["sid"])
    keys: set[str] = set()
    failed: set[tuple[str, str]] = set()
    ok_days: list[str] = []

    for month_start in helper_6(start, end):
        try:
            rows = fetch_a(client, base, tenant, month_start, fid, sid)
        except Exception:
            logger.exception("fetch_a failed code=%s month=%s", code, month_start)
            failed.add((code, f"{month_start.year:04d}-{month_start.month:02d}"))
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
            amap, table = fetch_b(client, base, tenant, fid, sid, [ds])
        except Exception:
            logger.exception("fetch_b failed code=%s day=%s", code, ds)
            failed.add((code, ds))
            continue
        for cell in table:
            tstr = str(cell.get("timeString", "")).strip()
            if not tstr:
                continue
            details = cell.get("details") or []
            if not details:
                continue
            if not helper_7(list(details), amap):
                continue
            if not helper_5(d, tstr):
                continue
            keys.add(f"{code}|{ds} {tstr}")
    return keys, failed


def collect_all(
    client: httpx.Client,
    base: str,
    tenant: str,
    items: list[dict[str, Any]],
    start: date,
    end: date,
) -> tuple[set[str], set[tuple[str, str]]]:
    out: set[str] = set()
    failed: set[tuple[str, str]] = set()
    for item in items:
        part, fail = collect_one(client, base, tenant, item, start, end)
        out |= part
        failed |= fail
    return out, failed


def _venue_months_all_failed(
    code: str,
    failed: set[tuple[str, str]],
    months: list[date],
) -> bool:
    if not months:
        return False
    for ms in months:
        tag = f"{ms.year:04d}-{ms.month:02d}"
        if (code, tag) not in failed:
            return False
    return True


def is_overall_fail(
    items: list[dict[str, Any]],
    failed: set[tuple[str, str]],
    start: date,
    end: date,
    *,
    base_url: str,
    tenant: str,
) -> bool:
    """True when site config missing or every venue failed all month fetches."""
    if not base_url or not tenant:
        return True
    if not items:
        return False
    months = helper_6(start, end)
    if not months:
        return False
    return all(
        _venue_months_all_failed(str(it["code"]), failed, months) for it in items
    )


def _apply_fail_watch(
    ok: bool,
    *,
    path_fail: Path,
    send: bool,
) -> tuple[int, bool]:
    from core.fail_watch import apply_outcome
    from core.notifier import send_alert_msg

    n, alert = apply_outcome(ok, path_fail)
    mailed_alert = False
    if alert and send:
        mailed_alert = bool(send_alert_msg(n))
    return n, mailed_alert


def run_task(
    now: datetime | None = None,
    *,
    client: httpx.Client | None = None,
    items: list[dict[str, Any]] | None = None,
    base: str | None = None,
    tenant: str | None = None,
    path_t1: Path | None = None,
    path_t2: Path | None = None,
    path_sup: Path | None = None,
    path_fail: Path | None = None,
    path_daily_sent: Path | None = None,
    send: bool = True,
    force_mail: bool = False,
) -> dict[str, Any]:
    from core.notifier import send_msg

    if now is None:
        cur = now_tokyo()
    elif now.tzinfo is not None:
        cur = now.astimezone(TZ_TOKYO).replace(tzinfo=None)
    else:
        cur = now
    day0 = cur.date()
    end = helper_4(cur)
    base_url = (base if base is not None else os.getenv("CFG_B1", "")).strip()
    ten = (tenant if tenant is not None else os.getenv("CFG_B2", "")).strip()
    cfg_items = items if items is not None else load_cfg()
    p1 = path_t1 or PATH_T1
    p2 = path_t2 or PATH_T2
    ps = path_sup or PATH_SUP
    pf = path_fail or PATH_FAIL
    pdaily = path_daily_sent or PATH_DAILY_SENT

    daily_first = is_daily_first_send(day0, pdaily)
    force_mail = bool(force_mail) or daily_first

    overall_ok = False
    try:
        own_client = client is None
        http = client or _http_client()
        failed: set[tuple[str, str]] = set()
        try:
            if not base_url or not ten:
                logger.error("CFG_B1 / CFG_B2 missing")
                keys: set[str] = set()
            else:
                keys, failed = collect_all(http, base_url, ten, cfg_items, day0, end)
        finally:
            if own_client:
                http.close()

        proc_b(keys, cur, path_t1=p1, path_sup=ps, failed_scope=failed)
        proc_promote(keys, cur, path_t1=p1, path_sup=ps)
        added, removed, old = proc_diff(keys, path_t2=p2, failed_scope=failed)
        suppressed = _load_suppressed(ps, today=day0)
        changed = should_send(added, removed, suppressed)
        lines = sorted(keys)
        mailed = False
        want_mail = changed or force_mail
        if want_mail and send:
            # Full snapshot + Calendar blocks (send_msg load_cal defaults True).
            mailed = bool(
                send_msg(
                    lines,
                    bool(lines),
                    when=day0,
                    scan_end=end,
                    suppressed=set(suppressed),
                    added=set(added),
                )
            )
            if mailed:
                # Only real change events refresh t2; force-only mail does not.
                if changed:
                    proc_f(snapshot_for_t2(keys, old, failed), path_t2=p2)
                if daily_first:
                    save_daily_sent_date(day0, pdaily)
            else:
                logger.error(
                    "mail decided but SMTP failed "
                    "(changed=%s force_mail=%s daily_first=%s mailed=False)",
                    changed,
                    force_mail,
                    daily_first,
                )
        elif changed and not send:
            proc_f(snapshot_for_t2(keys, old, failed), path_t2=p2)

        overall_ok = not is_overall_fail(
            cfg_items,
            failed,
            day0,
            end,
            base_url=base_url,
            tenant=ten,
        )
    except Exception:
        _apply_fail_watch(False, path_fail=pf, send=send)
        raise

    fail_n, fail_alert = _apply_fail_watch(overall_ok, path_fail=pf, send=send)
    logger.info(
        "keys=%s added=%s removed=%s changed=%s mailed=%s",
        len(keys),
        len(added),
        len(removed),
        changed,
        mailed,
    )
    if changed and mailed:
        changes_logger.info(
            "keys=%s added=%s removed=%s changed=%s mailed=%s",
            len(keys),
            len(added),
            len(removed),
            changed,
            mailed,
        )
    return {
        "keys": keys,
        "ready": keys,
        "lines": lines,
        "added": added,
        "removed": removed,
        "changed": changed,
        "mailed": mailed,
        "end": end.isoformat(),
        "failed": failed,
        "overall_ok": overall_ok,
        "fail_count": fail_n,
        "fail_alert": fail_alert,
    }
