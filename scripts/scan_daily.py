#!/usr/bin/env python3
"""Entry for daily task."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.notifier import check_mail_cfg  # noqa: E402
from core.scanner import run_task, setup_logging  # noqa: E402

logger = logging.getLogger(__name__)


def send_heartbeat(ping_url: str | None = None) -> None:
    """Ping CFG_D1 after a successful scan run. Never raises."""
    url = (ping_url if ping_url is not None else os.getenv("CFG_D1", "")).strip()
    if not url:
        logger.debug("heartbeat未配置，跳过")
        return
    try:
        httpx.get(url, timeout=10.0)
    except Exception as e:
        logger.warning("heartbeat ping failed: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force-mail",
        action="store_true",
        help="Always send mail with full current_keys snapshot (bypass 4h/suppressed gate)",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    setup_logging()
    check_mail_cfg()
    result = run_task(force_mail=args.force_mail)
    logger.debug(
        "done end=%s keys=%s n=%s changed=%s mailed=%s ok=%s fail_n=%s",
        result["end"],
        len(result["keys"]),
        len(result["lines"]),
        result["changed"],
        result["mailed"],
        result.get("overall_ok"),
        result.get("fail_count"),
    )
    send_heartbeat()


if __name__ == "__main__":
    main()
