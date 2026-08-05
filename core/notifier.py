"""SMTP helper and mail body formatting."""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_HOST = "smtp.gmail.com"
_PORT = 465
_ROOT = Path(__file__).resolve().parent.parent
_NAMES_PATH = _ROOT / "local" / "names.json"
_MAIL_CONTENT_PATH = _ROOT / "local" / "mail_content.json"
_YOBI = ("月", "火", "水", "木", "金", "土", "日")
_TZ = ZoneInfo("Asia/Tokyo")
_SCAN_MAIL_DEFAULTS = {"subject": "N({mmdd})", "header": "", "footer": ""}
# Fallback only when mail_content.json / lottery key is missing — keep generic
# so it cannot be mistaken for a hardcoded production subject.
_LOTTERY_MAIL_DEFAULTS = {
    "subject": "Lottery ({yyyy}-{mm})",
    "header": "",
}


def _today() -> date:
    return datetime.now(_TZ).date()


def _read_mail_content_file(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON object, or None on missing/invalid (logs already emitted)."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("mail content load failed: %s", path)
        return None
    if not isinstance(raw, dict):
        logger.warning("mail content invalid (not an object): %s", path)
        return None
    return raw


def load_mail_content(path: Path | None = None) -> dict[str, str]:
    """Load scan subject/header/footer from local/mail_content.json["scan"]."""
    p = path or _MAIL_CONTENT_PATH
    defaults = dict(_SCAN_MAIL_DEFAULTS)
    raw = _read_mail_content_file(p)
    if raw is None:
        if not p.exists():
            logger.error("mail content missing: %s", p)
        return defaults
    scan = raw.get("scan")
    if not isinstance(scan, dict):
        logger.warning(
            "mail content missing 'scan' key: %s (using scan defaults)", p
        )
        return defaults
    footer = str(scan.get("footer", "")).strip("\n")
    return {
        "subject": str(scan.get("subject", defaults["subject"])),
        "header": str(scan.get("header", "")),
        "footer": footer,
    }


def load_lottery_mail_content(path: Path | None = None) -> dict[str, str]:
    """Load lottery subject/header from local/mail_content.json["lottery"]."""
    p = path or _MAIL_CONTENT_PATH
    defaults = dict(_LOTTERY_MAIL_DEFAULTS)
    raw = _read_mail_content_file(p)
    if raw is None:
        if not p.exists():
            logger.warning(
                "mail content missing: %s (using lottery defaults)", p
            )
        return defaults
    lottery = raw.get("lottery")
    if not isinstance(lottery, dict):
        logger.warning(
            "mail content missing 'lottery' key: %s (using lottery defaults)", p
        )
        return defaults
    return {
        "subject": str(lottery.get("subject") or defaults["subject"]),
        "header": str(lottery.get("header", "")),
    }


def _apply_yyyy_mm(text: str, year: int, month: int) -> str:
    return (
        str(text)
        .replace("{yyyy}", str(year))
        .replace("{mm}", str(month))
    )


def load_names(path: Path | None = None) -> dict[str, dict[str, str]]:
    p = path or _NAMES_PATH
    if not p.exists():
        logger.error("names file missing: %s", p)
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("names load failed: %s", p)
        return {}
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for code, val in raw.items():
        if isinstance(val, dict):
            name = str(val.get("name", "")).strip()
            letter = str(val.get("letter", "")).strip()
            if name and letter:
                out[str(code)] = {"name": name, "letter": letter}
        elif isinstance(val, str) and val.strip():
            continue
    return out


def _mmdd(d: date) -> str:
    return f"{d.month}/{d.day}"


def _apply_mmdd(text: str, d: date) -> str:
    return text.replace("{mmdd}", _mmdd(d))


def _fmt_clock(hhmm: str) -> str:
    hhmm = hhmm.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm)
    if not m:
        return hhmm
    return f"{int(m.group(1))}:{m.group(2)}"


def _fmt_span(time_string: str) -> str:
    parts = re.split(r"\s*[-～~]\s*", time_string.strip(), maxsplit=1)
    if len(parts) != 2:
        return time_string.strip()
    return f"{_fmt_clock(parts[0])}～{_fmt_clock(parts[1])}"


def _fmt_day(d: date) -> str:
    return f"{d.month}月{d.day}日（{_YOBI[d.weekday()]}）"


def _parse_slot_line(line: str) -> tuple[str, date, str] | None:
    if "|" not in line:
        return None
    code, rest = line.split("|", 1)
    rest = rest.strip()
    if " " not in rest:
        return None
    day_s, time_s = rest.split(" ", 1)
    try:
        d = date.fromisoformat(day_s[:10])
    except ValueError:
        return None
    return code.strip(), d, time_s.strip()


def _slot_dates(
    slot_lines: list[str],
    *,
    suppressed: set[str] | None = None,
) -> set[date]:
    """Dates for 予約済み (get_matched).

    When ``suppressed`` is provided, only keys *not* in that set contribute
    dates (a day is omitted only if every current key on that day is suppressed).
    """
    out: set[date] = set()
    for line in slot_lines:
        parsed = _parse_slot_line(line)
        if not parsed:
            continue
        code, d, t = parsed
        if suppressed is not None:
            key = f"{code}|{d.isoformat()} {t}"
            if key in suppressed:
                continue
        out.add(d)
    return out


def _fmt_recent_block(
    day0: date,
    rows: dict[date, list[str]],
) -> str:
    d1 = day0
    d2 = day0 + timedelta(days=1)
    day_blocks: list[str] = []
    for d, label in ((d1, "今日"), (d2, "明日")):
        chunk = [f"【{_fmt_day(d)}】　{label}"]
        items = rows.get(d) or []
        if items:
            chunk.extend(f"・{x}" for x in items)
        else:
            chunk.append("・なし")
        day_blocks.append("\n".join(chunk))
    return (
        "----------直近予定----------\n"
        + "\n\n".join(day_blocks)
        + "\n-----------------------------"
    )


def _fmt_matched_block(rows: dict[date, list[str]]) -> str:
    blocks: list[str] = []
    for d in sorted(rows.keys()):
        items = rows.get(d) or []
        if not items:
            continue
        chunk = [f"【{_fmt_day(d)}】"]
        chunk.extend(f"・{x}" for x in items)
        blocks.append("\n".join(chunk))
    if not blocks:
        return ""
    return (
        "----------予約済み----------\n"
        + "\n\n".join(blocks)
        + "\n-----------------------------"
    )


def _months_span(day0: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = day0.year, day0.month
    ey, em = end.year, end.month
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _resolve_scan_end(day0: date, scan_end: date | None) -> date:
    if scan_end is not None:
        return scan_end
    from core.scanner import helper_4, now_tokyo

    cur = now_tokyo()
    if day0 == cur.date():
        return helper_4(cur)
    return helper_4(datetime(day0.year, day0.month, day0.day, 12, 0, 0))


def _fmt_counts_block(
    counts: dict[tuple[int, int], dict[str, list[tuple[str, int]]]],
    months: list[tuple[int, int]],
) -> str:
    month_blocks: list[str] = []
    for ym in months:
        data = counts.get(ym) or {}
        lines: list[str] = []
        for label in ("体育館", "公民館"):
            pairs = data.get(label) or []
            if not pairs:
                continue
            formatted = "、".join(f"{name[:1]}({n})" for name, n in pairs if name)
            lines.append(f"・{label}：{formatted}")
        if not lines:
            continue
        _y, m = ym
        month_blocks.append(f"【{m}月】\n" + "\n".join(lines))
    if not month_blocks:
        return ""
    return (
        "----------予約件数----------\n"
        + "\n\n".join(month_blocks)
        + "\n-----------------------------"
    )


def _fmt_avail_block(
    slot_lines: list[str],
    name_map: dict[str, dict[str, str]],
    *,
    added: set[str] | None = None,
) -> str:
    """Format 予約可能 block.

    When ``added`` is provided, keys in that set (relative to t2) get a ``(★)``
    suffix on the day line (any-added → whole line marked).
    """
    grouped: dict[str, dict[date, list[str]]] = defaultdict(lambda: defaultdict(list))
    for line in slot_lines:
        parsed = _parse_slot_line(line)
        if not parsed:
            continue
        code, d, tstr = parsed
        if tstr not in grouped[code][d]:
            grouped[code][d].append(tstr)

    parts = ["----------予約可能----------"]
    order = list(name_map.keys()) if name_map else sorted(grouped.keys())
    blocks: list[str] = []
    for code in order:
        days = grouped.get(code)
        if not days:
            continue
        meta = name_map.get(code)
        if not meta:
            continue
        block_lines = [f"【{meta['name']}】"]
        for d in sorted(days.keys()):
            times = sorted(days[d])
            spans = [_fmt_span(t) for t in times]
            line = f"・{_fmt_day(d)} - {'、'.join(spans)}"
            if added is not None:
                is_new = any(
                    f"{code}|{d.isoformat()} {t}" in added for t in times
                )
                if is_new:
                    line += "(★)"
            block_lines.append(line)
        blocks.append("\n".join(block_lines))
    if blocks:
        parts.append("\n\n".join(blocks))
    parts.append("-----------------------------")
    return "\n".join(parts)


def build_mail_body(
    slot_lines: list[str],
    *,
    names: dict[str, dict[str, str]] | None = None,
    names_path: Path | None = None,
    template: dict[str, str] | None = None,
    template_path: Path | None = None,
    when: date | None = None,
    scan_end: date | None = None,
    recent_rows: dict[date, list[str]] | None = None,
    matched_rows: dict[date, list[str]] | None = None,
    count_rows: dict[tuple[int, int], dict[str, list[tuple[str, int]]]] | None = None,
    load_cal: bool = True,
    venue_list: str | None = None,
    suppressed: set[str] | None = None,
    added: set[str] | None = None,
) -> str:
    """Assemble plain-text body from internal slot keys (vNN|YYYY-MM-DD HH:MM-HH:MM)."""
    name_map = names if names is not None else load_names(names_path)
    tmpl = template if template is not None else load_mail_content(template_path)
    day0 = when or _today()
    header = _apply_mmdd(str(tmpl.get("header", "")), day0).rstrip("\n")
    end = _resolve_scan_end(day0, scan_end)
    months = _months_span(day0, end)

    recent = recent_rows
    matched = matched_rows
    counts = count_rows
    if load_cal and (recent is None or matched is None):
        try:
            from core.calendar_read import get_matched, get_recent

            if recent is None:
                recent = get_recent([day0, day0 + timedelta(days=1)])
            if matched is None:
                matched = get_matched(
                    _slot_dates(slot_lines, suppressed=suppressed)
                )
        except Exception as e:
            print(f"cal read failed: {type(e).__name__}: {e}")
            recent = None
            matched = None

    if load_cal and counts is None:
        try:
            from core.calendar_read import get_counts

            counts = get_counts(months)
        except Exception as e:
            print(f"cal count failed: {type(e).__name__}: {e}")
            counts = None

    sections: list[str] = [header]
    if recent is not None:
        sections.append(_fmt_recent_block(day0, recent))
    sections.append(_fmt_avail_block(slot_lines, name_map, added=added))
    if matched is not None:
        matched_txt = _fmt_matched_block(matched)
        if matched_txt:
            sections.append(matched_txt)
    if counts is not None:
        counts_txt = _fmt_counts_block(counts, months)
        if counts_txt:
            sections.append(counts_txt)

    if venue_list is not None:
        footer = venue_list.strip("\n") if venue_list else ""
    else:
        footer = str(tmpl.get("footer", "")).strip("\n")
    if footer:
        sections.append(footer)

    return "\n\n".join(sections) + "\n"


def build_mail_subject(
    when: date | None = None,
    *,
    template: dict[str, str] | None = None,
    template_path: Path | None = None,
) -> str:
    """Build scan mail subject from mail_content.json["scan"]["subject"] (+ {mmdd})."""
    tmpl = template if template is not None else load_mail_content(template_path)
    d = when or _today()
    return _apply_mmdd(str(tmpl.get("subject", _SCAN_MAIL_DEFAULTS["subject"])), d)


def _recipients(raw: str) -> list[str]:
    return [r.strip() for r in raw.split(",") if r.strip()]


_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def _is_single_email(addr: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(addr.strip()))


def check_mail_cfg() -> None:
    """Fail fast on invalid sender / empty recipients. Raises SystemExit."""
    a1 = os.getenv("CFG_A1", "").strip()
    a2 = os.getenv("CFG_A2", "").strip()
    a3_raw = os.getenv("CFG_A3", "")

    if not a1:
        raise SystemExit("CFG_A1 is missing (single sender address required)")
    if "," in a1:
        raise SystemExit("CFG_A1 应为单个发件邮箱，检测到逗号分隔的多个地址")
    if not _is_single_email(a1):
        raise SystemExit(f"CFG_A1 格式无效（应为单个发件邮箱）: {a1}")

    if not a2:
        raise SystemExit("CFG_A2 is missing")

    to_list = _recipients(a3_raw)
    if not to_list:
        raise SystemExit("CFG_A3 is missing (one or more recipient addresses, comma-separated OK)")


def send_msg(
    slot_lines: list[str],
    flag_x: bool,
    *,
    when: date | None = None,
    scan_end: date | None = None,
    names: dict[str, dict[str, str]] | None = None,
    names_path: Path | None = None,
    template: dict[str, str] | None = None,
    template_path: Path | None = None,
    recent_rows: dict[date, list[str]] | None = None,
    matched_rows: dict[date, list[str]] | None = None,
    count_rows: dict[tuple[int, int], dict[str, list[tuple[str, int]]]] | None = None,
    load_cal: bool = True,
    venue_list: str | None = None,
    suppressed: set[str] | None = None,
    added: set[str] | None = None,
) -> bool:
    a1 = os.getenv("CFG_A1", "").strip()
    a2 = os.getenv("CFG_A2", "").strip()
    to_list = _recipients(os.getenv("CFG_A3", ""))
    if not a1 or not a2 or not to_list:
        logger.error("CFG_A1 / CFG_A2 / CFG_A3 missing")
        return False
    if "," in a1 or not _is_single_email(a1):
        msg = (
            "CFG_A1 应为单个发件邮箱，检测到逗号分隔的多个地址"
            if "," in a1
            else f"CFG_A1 格式无效（应为单个发件邮箱）: {a1}"
        )
        print(msg)
        logger.error(msg)
        return False

    d = when or _today()
    subject = build_mail_subject(d, template=template, template_path=template_path)
    body = build_mail_body(
        slot_lines,
        names=names,
        names_path=names_path,
        template=template,
        template_path=template_path,
        when=d,
        scan_end=scan_end,
        recent_rows=recent_rows,
        matched_rows=matched_rows,
        count_rows=count_rows,
        load_cal=load_cal,
        venue_list=venue_list,
        suppressed=suppressed,
        added=added,
    )
    logger.debug("send flag_x=%s subject=%s", flag_x, subject)

    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = a1
    msg["To"] = ", ".join(to_list)

    try:
        with smtplib.SMTP_SSL(_HOST, _PORT) as server:
            server.login(a1, a2)
            server.sendmail(a1, to_list, msg.as_string())
        return True
    except Exception as e:
        print(f"SMTP send failed: {type(e).__name__}: {e}")
        logger.exception("send failed")
        return False


def send_alert_msg(n: int) -> bool:
    """Send a neutral repeated-failure alert (same CFG_A* as normal mail)."""
    a1 = os.getenv("CFG_A1", "").strip()
    a2 = os.getenv("CFG_A2", "").strip()
    to_list = _recipients(os.getenv("CFG_A3", ""))
    if not a1 or not a2 or not to_list:
        logger.error("CFG_A1 / CFG_A2 / CFG_A3 missing")
        return False
    if "," in a1 or not _is_single_email(a1):
        msg = (
            "CFG_A1 应为单个发件邮箱，检测到逗号分隔的多个地址"
            if "," in a1
            else f"CFG_A1 格式无效（应为单个发件邮箱）: {a1}"
        )
        print(msg)
        logger.error(msg)
        return False

    subject = "[ALERT] task failing repeatedly"
    body = f"已连续{n}次运行失败，请检查\n"
    logger.debug("send alert n=%s", n)

    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = a1
    msg["To"] = ", ".join(to_list)

    try:
        with smtplib.SMTP_SSL(_HOST, _PORT) as server:
            server.login(a1, a2)
            server.sendmail(a1, to_list, msg.as_string())
        return True
    except Exception as e:
        print(f"SMTP alert send failed: {type(e).__name__}: {e}")
        logger.exception("alert send failed")
        return False


def send_text_msg(subject: str, body: str) -> bool:
    """Send arbitrary plain-text mail via CFG_A* (multi-recipient CFG_A3 OK)."""
    a1 = os.getenv("CFG_A1", "").strip()
    a2 = os.getenv("CFG_A2", "").strip()
    to_list = _recipients(os.getenv("CFG_A3", ""))
    if not a1 or not a2 or not to_list:
        logger.error("CFG_A1 / CFG_A2 / CFG_A3 missing")
        return False
    if "," in a1 or not _is_single_email(a1):
        err = (
            "CFG_A1 应为单个发件邮箱，检测到逗号分隔的多个地址"
            if "," in a1
            else f"CFG_A1 格式无效（应为单个发件邮箱）: {a1}"
        )
        print(err)
        logger.error(err)
        return False

    logger.debug("send_text subject=%s", subject)
    mail = MIMEText(body, _subtype="plain", _charset="utf-8")
    mail["Subject"] = subject
    mail["From"] = a1
    mail["To"] = ", ".join(to_list)
    try:
        with smtplib.SMTP_SSL(_HOST, _PORT) as server:
            server.login(a1, a2)
            server.sendmail(a1, to_list, mail.as_string())
        return True
    except Exception as e:
        print(f"SMTP send failed: {type(e).__name__}: {e}")
        logger.exception("send_text failed")
        return False


def build_lottery_subject(
    year: int,
    month: int,
    *,
    template: dict[str, str] | None = None,
    template_path: Path | None = None,
) -> str:
    """Build lottery subject from mail_content.json["lottery"]["subject"] (+ {yyyy}/{mm})."""
    tmpl = (
        template
        if template is not None
        else load_lottery_mail_content(template_path)
    )
    return _apply_yyyy_mm(
        str(tmpl.get("subject", _LOTTERY_MAIL_DEFAULTS["subject"])),
        year,
        month,
    )


def _fmt_day_lottery(d: date) -> str:
    return f"{d.month}月{d.day}日({_YOBI[d.weekday()]})"


def lottery_start_hh(time_string: str) -> str:
    """Return zero-padded start hour from '09:00-11:00' / '9:00～11:00' → '09'."""
    raw = str(time_string or "").strip()
    start = re.split(r"\s*[-～~]\s*", raw, maxsplit=1)[0].strip()
    m = re.match(r"(\d{1,2})", start)
    if not m:
        return start
    return f"{int(m.group(1)):02d}"


def _fmt_lottery_day_line(
    day: date,
    slot_map: dict[str, dict[str, int]],
    faces: list[str],
    *,
    starred: bool = False,
) -> str:
    """Build one day line; ``slot_map`` is hour → {face → count}."""
    multi = len(faces) >= 2
    chunks: list[str] = []
    for hh in sorted(slot_map.keys(), key=lambda x: int(x)):
        face_counts = slot_map[hh]
        if multi:
            inner = "/".join(f"{f}{face_counts[f]}" for f in faces if f in face_counts)
            if not inner:
                continue
            chunks.append(f"{hh}({inner})")
        else:
            # single face: only the count
            if not face_counts:
                continue
            n = next(iter(face_counts.values()))
            chunks.append(f"{hh}({n})")
    line = f"・{_fmt_day_lottery(day)} {'、'.join(chunks)}"
    if starred:
        line += "(★)"
    return line


def _day_slot_changed(
    code: str,
    day: date,
    slot_map: dict[str, dict[str, int]],
    previous: dict[str, int] | None,
) -> bool:
    """True if any slot/face on this day differs from previous snapshot."""
    if previous is None:
        return False
    day_s = day.isoformat()
    # any current value different / new
    for hh, face_counts in slot_map.items():
        for face, count in face_counts.items():
            k = f"{code}|{day_s}|{hh}|{face}"
            if previous.get(k) != count:
                return True
    # any previous key for this day missing from current
    prefix = f"{code}|{day_s}|"
    current_keys = {
        f"{code}|{day_s}|{hh}|{face}"
        for hh, face_counts in slot_map.items()
        for face in face_counts
    }
    for k in previous:
        if str(k).startswith(prefix) and k not in current_keys:
            return True
    return False


def _fmt_lottery_avail_block(
    entries: list[Any],
    name_map: dict[str, dict[str, str]],
    *,
    previous: dict[str, int] | None = None,
) -> str:
    by_code: dict[str, list[Any]] = defaultdict(list)
    for e in entries:
        by_code[str(e.code)].append(e)

    parts: list[str] = ["----------抽選可能----------"]
    order = list(name_map.keys()) if name_map else sorted(by_code.keys())
    for code in sorted(by_code.keys()):
        if code not in order:
            order.append(code)

    blocks: list[str] = []
    for code in order:
        rows = by_code.get(code) or []
        if not rows:
            continue
        meta = name_map.get(code) or {}
        title = str(meta.get("name") or code)
        lines = [f"【{title}】"]

        faces = sorted({str(r.face) for r in rows})
        by_day: dict[date, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for r in rows:
            hh = lottery_start_hh(str(r.time_string))
            by_day[r.day][hh][str(r.face)] = int(r.count)

        for d in sorted(by_day.keys()):
            starred = _day_slot_changed(code, d, by_day[d], previous)
            lines.append(
                _fmt_lottery_day_line(d, by_day[d], faces, starred=starred)
            )
        blocks.append("\n".join(lines))

    if blocks:
        parts.append("\n\n".join(blocks))
    parts.append("-----------------------------")
    return "\n".join(parts)


def build_lottery_body(
    entries: list[Any],
    *,
    year: int,
    month: int,
    names: dict[str, dict[str, str]] | None = None,
    names_path: Path | None = None,
    template: dict[str, str] | None = None,
    template_path: Path | None = None,
    previous: dict[str, int] | None = None,
) -> str:
    """Format lottery mail body: optional header + 抽選可能 block.

    When ``previous`` is provided (snapshot from lottery_prev.json), day lines
    with any slot/face count change vs previous are suffixed with ``(★)``.
    Pass ``previous={}`` on first run to mark all days as new.
    """
    name_map = names if names is not None else load_names(names_path)
    tmpl = (
        template
        if template is not None
        else load_lottery_mail_content(template_path)
    )
    greet = str(tmpl.get("header", "")).rstrip("\n")
    avail = _fmt_lottery_avail_block(entries, name_map, previous=previous)
    if greet:
        return greet + "\n\n" + avail + "\n"
    return avail + "\n"
