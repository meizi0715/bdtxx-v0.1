"""Tests for open-wait vs immediate modes, settle wait, concurrency, CLI tasks."""

from __future__ import annotations

import asyncio
import importlib.util
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from core.booking import (
    MSG_CONFIRM_UNCLEAR,
    MSG_SLOT_UNAVAILABLE,
    display_time_range,
    expand_booking_tasks,
    is_open_wait_mode,
    run_tasks,
    tasks_from_cli,
    wait_for_slots_settled,
)

TZ = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]

_VENUES = {
    "v04": {"fid": 4, "sid": 4, "type": "hall"},
    "v08": {"fid": 8, "sid": 8, "type": "hall"},
}
_GROUPS = {
    "GROUP3": {"id": "gid3", "name": "ＢＭチーム", "password": "pw3"},
    "GROUP1": {"id": "gid1", "name": "チーム1", "password": "pw1"},
}


def _load_auto_book():
    spec = importlib.util.spec_from_file_location(
        "auto_book_under_test", ROOT / "scripts" / "auto_book.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestOpenWaitMode:
    def test_first_before_nine_is_open_wait(self) -> None:
        assert is_open_wait_mode(datetime(2026, 9, 1, 8, 59, 59, tzinfo=TZ)) is True
        assert is_open_wait_mode(datetime(2026, 9, 1, 0, 0, 0, tzinfo=TZ)) is True

    def test_first_at_or_after_nine_is_immediate(self) -> None:
        assert is_open_wait_mode(datetime(2026, 9, 1, 9, 0, 0, tzinfo=TZ)) is False
        assert is_open_wait_mode(datetime(2026, 9, 1, 10, 0, 0, tzinfo=TZ)) is False

    def test_non_first_is_immediate(self) -> None:
        assert is_open_wait_mode(datetime(2026, 9, 2, 8, 0, 0, tzinfo=TZ)) is False
        assert is_open_wait_mode(datetime(2026, 8, 15, 8, 0, 0, tzinfo=TZ)) is False


class TestWaitForSlotsSettled:
    def test_available_immediate(self) -> None:
        page = MagicMock()
        handle = MagicMock()
        handle.json_value = AsyncMock(return_value="available")
        page.wait_for_function = AsyncMock(return_value=handle)
        assert (
            asyncio.run(
                wait_for_slots_settled(
                    page, ["09:00-11:00"], "hall", timeout_ms=1000
                )
            )
            == "available"
        )

    def test_unavailable_after_settle(self) -> None:
        page = MagicMock()
        handle = MagicMock()
        handle.json_value = AsyncMock(return_value="unavailable")
        page.wait_for_function = AsyncMock(return_value=handle)
        assert (
            asyncio.run(
                wait_for_slots_settled(
                    page, ["19:00-21:00"], "hall", timeout_ms=1000
                )
            )
            == "unavailable"
        )

    def test_timeout_unclear(self) -> None:
        page = MagicMock()
        page.wait_for_function = AsyncMock(side_effect=TimeoutError("slow"))
        assert (
            asyncio.run(
                wait_for_slots_settled(
                    page, ["19:00-21:00"], "hall", timeout_ms=50
                )
            )
            == "timeout"
        )


class TestRunTasksConcurrency:
    def test_same_group_runs_fully_concurrent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started: list[float] = []
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def slow_book(task: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
                started.append(time.monotonic())
            await asyncio.sleep(0.15)
            async with lock:
                active -= 1
            return {
                "success": True,
                "venue_code": task["venue_code"],
                "group_id": task["group_id"],
            }

        monkeypatch.setattr("core.booking.book_one", slow_book)
        monkeypatch.setattr("core.booking.load_venues", lambda: _VENUES)
        monkeypatch.setattr("core.booking.load_names", lambda: {})
        tasks = [
            {
                "venue_code": "v04",
                "date": "2026-09-20",
                "time_slots": ["19:00-21:00"],
                "group_id": "same",
                "group_name": "団",
                "password": "x",
            },
            {
                "venue_code": "v08",
                "date": "2026-09-21",
                "time_slots": ["19:00-21:00"],
                "group_id": "same",
                "group_name": "団",
                "password": "x",
            },
            {
                "venue_code": "v04",
                "date": "2026-09-22",
                "time_slots": ["17:00-19:00"],
                "group_id": "same",
                "group_name": "団",
                "password": "x",
            },
        ]
        t0 = time.monotonic()
        results = asyncio.run(run_tasks(tasks, wait_9am=False))
        elapsed = time.monotonic() - t0
        assert len(results) == 3
        assert max_active == 3
        # Sequential would be ~0.45s; concurrent ~0.15s
        assert elapsed < 0.4


class TestCliTaskExpand:
    def test_hours_17_19_hall_merges_like_json(self) -> None:
        now = datetime(2026, 8, 1, 10, 0, tzinfo=TZ)
        raw = {
            "venue_code": "v04",
            "day": 20,
            "hours": "17,19",
            "group_ref": "GROUP3",
        }
        from_json = expand_booking_tasks(
            [raw], now=now, venues=_VENUES, groups=_GROUPS
        )
        from_cli = tasks_from_cli(
            venue_code="v04",
            day=20,
            hours="17,19",
            group_ref="GROUP3",
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert len(from_json) == 1
        assert len(from_cli) == 1
        assert from_cli[0]["time_slots"] == from_json[0]["time_slots"]
        assert display_time_range(from_cli[0]["time_slots"]) == "17:00-21:00"
        assert from_cli[0]["date"] == from_json[0]["date"] == "2026-09-20"
        assert from_cli[0]["group_id"] == "gid3"

    def test_missing_cli_flags_message(self) -> None:
        ab = _load_auto_book()
        missing = ab.missing_cli_task_flags(
            venue_code="v04",
            day=20,
            hours=None,
            group_ref="GROUP3",
        )
        assert missing == ["--hours"]
        with pytest.raises(ValueError, match="missing: --hours"):
            ab.resolve_tasks_for_run(
                venue_code="v04",
                day=20,
                hours=None,
                group_ref="GROUP3",
            )

    def test_cli_mode_skips_booking_tasks_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ab = _load_auto_book()
        called = {"load": 0}

        def boom_load(*a: Any, **k: Any) -> list[dict[str, Any]]:
            called["load"] += 1
            raise AssertionError("load_tasks should not be called in CLI mode")

        monkeypatch.setattr(ab, "load_tasks", boom_load)
        monkeypatch.setattr(
            ab,
            "tasks_from_cli",
            lambda **kw: [
                {
                    "venue_code": "v04",
                    "date": "2026-09-20",
                    "time_slots": ["19:00-21:00"],
                    "group_id": "gid3",
                    "group_name": "ＢＭチーム",
                    "password": "pw3",
                }
            ],
        )
        tasks = ab.resolve_tasks_for_run(
            venue_code="v04",
            day=20,
            hours="19",
            group_ref="GROUP3",
        )
        assert called["load"] == 0
        assert len(tasks) == 1
        assert tasks[0]["venue_code"] == "v04"

    def test_no_cli_flags_uses_load_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ab = _load_auto_book()
        monkeypatch.setattr(
            ab,
            "load_tasks",
            lambda: [{"venue_code": "v09", "date": "2026-09-08"}],
        )
        tasks = ab.resolve_tasks_for_run()
        assert tasks[0]["venue_code"] == "v09"

    def test_main_incomplete_cli_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ab = _load_auto_book()
        monkeypatch.setattr(ab, "load_dotenv", lambda *a, **k: None)
        monkeypatch.setattr(ab, "setup_booking_logging", lambda: None)
        code = ab.main(
            ["--venue-code", "v04", "--day", "20", "--group-ref", "GROUP3"]
        )
        assert code == 2
        err = capsys.readouterr().err
        assert "--hours" in err


class TestSlotUnavailableMessage:
    def test_constant_text(self) -> None:
        assert "現在予約できません" in MSG_SLOT_UNAVAILABLE
        assert "空きがありません" in MSG_SLOT_UNAVAILABLE
        assert "確認ができませんでした" in MSG_CONFIRM_UNCLEAR
