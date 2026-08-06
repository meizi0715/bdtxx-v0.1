#!/usr/bin/env python3
"""Sync one group's reservation history into Google Calendar (daily)."""

from __future__ import annotations

import argparse
import logging
import random
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.booking import load_groups  # noqa: E402
from core.calendar_sync import (  # noqa: E402
    PATH_SYNC_STATE,
    build_summary_mail,
    day_work_finished,
    load_sync_state,
    save_sync_state,
    sync_one_group,
)
from core.stats_calendar import update_stats_calendar  # noqa: E402
from core.notifier import check_mail_cfg, send_text_msg  # noqa: E402
from core.scanner import send_heartbeat, setup_logging, today_tokyo  # noqa: E402

logger = logging.getLogger(__name__)


def _maybe_send_summary(state: dict, total: int) -> None:
    if state.get("summary_sent"):
        return
    if not day_work_finished(state, total):
        return
    done_n = len(list(state.get("done") or []))
    subject, body = build_summary_mail(done_n, total)
    check_mail_cfg()
    ok = bool(send_text_msg(subject, body))
    if ok:
        state["summary_sent"] = True
        save_sync_state(state)
        logger.info(
            "summary mailed done=%s total=%s attempts=%s",
            done_n,
            total,
            state.get("attempts"),
        )
    else:
        logger.error("summary mail send failed; will retry next run")


def _run(force_ref: str) -> None:
    """Run one sync attempt / quiet exit. May raise SystemExit on bad config."""
    today = today_tokyo()
    state = load_sync_state(PATH_SYNC_STATE, today=today)

    if state.get("summary_sent"):
        logger.info("sync calendar already finished today; quiet exit")
        return

    groups = load_groups()
    refs = sorted(groups.keys())
    total = len(refs)
    if total <= 0:
        raise SystemExit("local/groups.json has no groups")

    if day_work_finished(state, total):
        _maybe_send_summary(state, total)
        return

    done = set(str(x) for x in (state.get("done") or []))

    if force_ref:
        if force_ref not in groups:
            raise SystemExit(f"unknown group-ref: {force_ref}")
        group_ref = force_ref
    else:
        remaining = [r for r in refs if r not in done]
        if not remaining:
            _maybe_send_summary(state, total)
            return
        group_ref = random.choice(remaining)

    state["attempts"] = int(state.get("attempts") or 0) + 1
    save_sync_state(state)
    logger.info(
        "sync attempt group=%s attempts=%s done=%s/%s forced=%s",
        group_ref,
        state["attempts"],
        len(done),
        total,
        bool(force_ref),
    )

    try:
        sync_one_group(group_ref, headless=True, today=today)
    except Exception:
        logger.exception("sync failed group=%s", group_ref)
    else:
        done_list = list(state.get("done") or [])
        if group_ref not in done_list:
            done_list.append(group_ref)
        state["done"] = done_list
        save_sync_state(state)
        logger.info("sync ok group=%s done=%s/%s", group_ref, len(done_list), total)
        try:
            update_stats_calendar(today=today, groups=groups)
        except Exception:
            logger.exception("stats calendar update failed after sync group=%s", group_ref)

    # Re-check finish condition after this attempt
    state = load_sync_state(PATH_SYNC_STATE, today=today)
    _maybe_send_summary(state, total)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--group-ref",
        default="",
        metavar="REF",
        help=(
            "Force sync this group_ref (e.g. GROUP1), even if already in "
            "today's done list. Still updates attempts/done and may send "
            "the daily summary when finish conditions are met."
        ),
    )
    args = parser.parse_args()
    force_ref = str(args.group_ref or "").strip()

    load_dotenv(ROOT / ".env")
    setup_logging()
    _run(force_ref)
    # Normal completion (including quiet/finished early exits). Not reached on
    # uncaught exceptions / SystemExit from bad config.
    send_heartbeat(env_key="CFG_D2")


if __name__ == "__main__":
    main()
