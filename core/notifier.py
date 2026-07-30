"""SMTP helper."""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import date
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_HOST = "smtp.gmail.com"
_PORT = 465


def send_msg(
    body_lines: list[str],
    flag_x: bool,
    *,
    when: date | None = None,
) -> bool:
    a1 = os.getenv("CFG_A1", "").strip()
    a2 = os.getenv("CFG_A2", "").strip()
    a3 = os.getenv("CFG_A3", "").strip()
    if not a1 or not a2 or not a3:
        logger.error("CFG_A1 / CFG_A2 / CFG_A3 missing")
        return False

    d = when or date.today()
    subject = f"N({d.month}/{d.day})"
    body = "\n".join(body_lines)
    logger.info("send flag_x=%s subject=%s", flag_x, subject)

    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = a1
    msg["To"] = a3

    try:
        with smtplib.SMTP_SSL(_HOST, _PORT) as server:
            server.login(a1, a2)
            server.sendmail(a1, [a3], msg.as_string())
        return True
    except Exception:
        logger.exception("send failed")
        return False
