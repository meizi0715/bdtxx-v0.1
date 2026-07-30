#!/usr/bin/env python3
"""Entry for daily task."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.scanner import run_task  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    load_dotenv(ROOT / ".env")
    result = run_task()
    logging.getLogger(__name__).info(
        "done end=%s n=%s changed=%s mailed=%s",
        result["end"],
        len(result["lines"]),
        result["changed"],
        result["mailed"],
    )


if __name__ == "__main__":
    main()
