#!/usr/bin/env python3
"""Scan lottery waiting counts for month+2 and optionally mail the report."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.lottery import (  # noqa: E402
    PATH_VENUES,
    collect_all,
    entries_to_snapshot,
    load_lottery_prev,
    load_venues,
    lottery_changed,
    save_lottery_prev,
)
from core.notifier import (  # noqa: E402
    build_lottery_body,
    build_lottery_subject,
    check_mail_cfg,
    send_text_msg,
)
from core.scanner import send_heartbeat, setup_lottery_logging  # noqa: E402

logger = logging.getLogger("core.lottery")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-mail",
        action="store_true",
        help="Print subject/body to stdout instead of sending mail",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Max concurrent HTTP requests (default 5)",
    )
    parser.add_argument(
        "--venue-code",
        action="append",
        default=[],
        metavar="CODE",
        help="Limit scan to venue code(s), e.g. --venue-code v03 (repeatable)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    setup_lottery_logging()
    if not args.no_mail:
        check_mail_cfg()

    base = os.getenv("CFG_B1", "").strip()
    tenant = os.getenv("CFG_B2", "").strip()
    if not base or not tenant:
        raise SystemExit("CFG_B1 / CFG_B2 missing")

    try:
        venues = load_venues(PATH_VENUES)
        if args.venue_code:
            wanted = [str(c).strip() for c in args.venue_code if str(c).strip()]
            missing = [c for c in wanted if c not in venues]
            if missing:
                raise SystemExit(f"unknown venue-code: {', '.join(missing)}")
            venues = {c: venues[c] for c in wanted}

        year, month, entries = collect_all(
            base,
            tenant,
            venues,
            concurrency=max(1, int(args.concurrency)),
        )
    except SystemExit:
        raise
    except Exception:
        logger.exception("lottery scan failed")
        raise SystemExit(1) from None

    previous = load_lottery_prev()
    current = entries_to_snapshot(entries)
    changed = lottery_changed(current, previous)

    subject = build_lottery_subject(year, month)
    body = build_lottery_body(
        entries,
        year=year,
        month=month,
        previous=previous,
    )

    mailed = False
    if args.no_mail:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
        print(subject)
        print(body, end="" if body.endswith("\n") else "\n")
        save_lottery_prev(current)
    elif changed:
        mailed = bool(send_text_msg(subject, body))
        if mailed:
            save_lottery_prev(current)
        else:
            logger.info(
                "keys=%s changed=%s mailed=%s",
                len(current),
                changed,
                mailed,
            )
            raise SystemExit("mail send failed")
    else:
        # unchanged: still refresh snapshot (e.g. key order / prune) as baseline
        save_lottery_prev(current)

    logger.info(
        "keys=%s changed=%s mailed=%s year=%s month=%s venues=%s",
        len(current),
        changed,
        mailed,
        year,
        month,
        ",".join(venues.keys()),
    )
    # Normal completion (mailed / unchanged / --no-mail). Not reached on
    # uncaught exceptions / SystemExit (e.g. mail send failed).
    send_heartbeat(env_key="CFG_D3")


if __name__ == "__main__":
    main()
