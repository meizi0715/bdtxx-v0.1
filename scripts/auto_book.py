#!/usr/bin/env python3
"""Auto-book entry. Use --probe to test anonymous browse up to time-select."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.booking import (  # noqa: E402
    format_book_error,
    load_tasks,
    run_tasks,
    send_results_mail,
    tasks_from_cli,
)
from core.scanner import setup_booking_logging  # noqa: E402

logger = logging.getLogger("core.booking")

_CLI_TASK_FLAGS = (
    ("venue_code", "--venue-code"),
    ("day", "--day/--date"),
    ("hours", "--hours"),
    ("group_ref", "--group-ref"),
)


def missing_cli_task_flags(
    *,
    venue_code: str | None,
    day: int | None,
    hours: str | None,
    group_ref: str | None,
) -> list[str]:
    """Return human-readable flag names that are missing for CLI task mode."""
    values = {
        "venue_code": venue_code,
        "day": day,
        "hours": hours,
        "group_ref": group_ref,
    }
    any_set = any(
        v is not None and str(v).strip() != "" for v in values.values()
    )
    if not any_set:
        return []
    missing: list[str] = []
    for key, flag in _CLI_TASK_FLAGS:
        val = values[key]
        if val is None or str(val).strip() == "":
            missing.append(flag)
    return missing


def resolve_tasks_for_run(
    *,
    venue_code: str | None = None,
    day: int | None = None,
    hours: str | None = None,
    group_ref: str | None = None,
    probe: bool = False,
) -> list[dict]:
    """Load booking_tasks.json, or build one CLI task via expand_booking_tasks."""
    missing = missing_cli_task_flags(
        venue_code=venue_code,
        day=day,
        hours=hours,
        group_ref=group_ref,
    )
    if missing:
        raise ValueError(
            "CLI task mode requires all of --venue-code, --day/--date, "
            f"--hours, --group-ref; missing: {', '.join(missing)}"
        )

    cli_mode = all(
        v is not None and str(v).strip() != ""
        for v in (venue_code, day, hours, group_ref)
    )
    if cli_mode:
        tasks = tasks_from_cli(
            venue_code=str(venue_code),
            day=int(day),  # type: ignore[arg-type]
            hours=str(hours),
            group_ref=str(group_ref),
        )
    else:
        tasks = load_tasks()

    if probe and tasks:
        tasks = tasks[:1]
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Stop at time-select; verify anonymous vs login-first branch",
    )
    parser.add_argument("--headed", action="store_true", help="Show browser UI")
    parser.add_argument(
        "--no-wait-9am",
        action="store_true",
        help="Skip wait_until_target(9:00) on the 1st-before-9am branch (test)",
    )
    parser.add_argument(
        "--no-mail",
        action="store_true",
        help="Do not send result mail",
    )
    parser.add_argument(
        "--inject-error",
        action="store_true",
        help="Inject one unexpected RuntimeError on the first task (test harness)",
    )
    parser.add_argument(
        "--stop-after-input",
        action="store_true",
        help="Stop after reaching 予約入力 (skip final confirm; dry-run harness)",
    )
    parser.add_argument(
        "--venue-code",
        default=None,
        help="CLI single-task mode: venue code (requires --day/--hours/--group-ref)",
    )
    parser.add_argument(
        "--day",
        "--date",
        type=int,
        default=None,
        dest="day",
        help="CLI single-task mode: day-of-month for next month (alias: --date)",
    )
    parser.add_argument(
        "--hours",
        default=None,
        help="CLI single-task mode: comma-separated hour starts (e.g. 17,19)",
    )
    parser.add_argument(
        "--group-ref",
        default=None,
        help="CLI single-task mode: group key in local/groups.json",
    )
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    setup_booking_logging()

    if args.inject_error:
        os.environ["BOOK_INJECT_ERROR"] = "1"
    if args.stop_after_input:
        os.environ["BOOK_STOP_AFTER_INPUT"] = "1"

    results: list[dict] = []
    try:
        tasks = resolve_tasks_for_run(
            venue_code=args.venue_code,
            day=args.day,
            hours=args.hours,
            group_ref=args.group_ref,
            probe=args.probe,
        )
        results = asyncio.run(
            run_tasks(
                tasks,
                headless=not args.headed,
                wait_9am=not args.no_wait_9am,
                stop_at_time_select=args.probe,
            )
        )
    except ValueError as exc:
        # Incomplete CLI task args — print reminder and exit without booking.
        msg = str(exc)
        print(msg, file=sys.stderr)
        logger.error("%s", msg)
        return 2
    except Exception as exc:
        logger.debug("batch aborted", exc_info=True)
        results = [
            {
                "success": False,
                "venue_code": "-",
                "venue_name": "-",
                "date": "",
                "time_slot": "",
                "group_id": "",
                "error_message": f"batch aborted: {format_book_error(exc)}",
            }
        ]

    print(json.dumps(results, ensure_ascii=False, indent=2))
    logger.debug(
        "book done n=%s ok=%s",
        len(results),
        sum(1 for r in results if r.get("success")),
    )

    # Always attempt summary mail unless explicitly disabled / probe.
    if not args.probe and not args.no_mail and results:
        try:
            mailed = send_results_mail(results)
            logger.debug("summary mail sent=%s", mailed)
        except Exception:
            logger.debug("summary mail failed", exc_info=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
