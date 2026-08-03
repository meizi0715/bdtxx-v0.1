"""Tests for 予約申込へ apply-retry loop."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import booking as bk


def _page_mock() -> MagicMock:
    page = MagicMock()
    page.url = "https://example.test/facility/time-select"
    page.wait_for_timeout = AsyncMock()
    return page


class TestApplyForSlotsWithRetry:
    def test_success_first_attempt_no_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _page_mock()
        calls = {"nav": 0, "select": 0, "click": 0}

        async def avail(page: Any, slots: list[str], venue_type: str) -> bool:
            return True

        async def select(page: Any, slot: str, venue_type: str, **kwargs: Any) -> bool:
            calls["select"] += 1
            return True

        async def click(page: Any, **kwargs: Any) -> bool:
            calls["click"] += 1
            return True

        async def wait_after(page: Any, *, timeout_ms: int = 0) -> str:
            return "proceed"

        async def can_input(page: Any) -> bool:
            # After first click, next step appears
            return calls["click"] >= 1

        async def hint(page: Any) -> bool:
            return False

        async def disabled(page: Any) -> bool:
            return False

        async def clickable(page: Any) -> bool:
            return False

        async def nav(
            page: Any, venue_name: str, sid: int, day: date, **kwargs: Any
        ) -> None:
            calls["nav"] += 1

        monkeypatch.setattr(bk, "_time_slots_available_on_page", avail)
        monkeypatch.setattr(bk, "_select_time_cell", select)
        monkeypatch.setattr(bk, "_click_yoyaku_moshikomi", click)
        monkeypatch.setattr(bk, "wait_after_yoyaku_moshikomi", wait_after)
        monkeypatch.setattr(bk, "_can_click_yoyaku_input", can_input)
        monkeypatch.setattr(bk, "_page_has_unavailable_hint", hint)
        monkeypatch.setattr(bk, "_yoyaku_button_disabled", disabled)
        monkeypatch.setattr(bk, "_yoyaku_button_clickable", clickable)
        monkeypatch.setattr(bk, "_navigate_to_time_select", nav)

        asyncio.run(
            bk.apply_for_slots_with_retry(
                page,
                slots=["09:00-11:00"],
                venue_type="gym",
                venue_name="体育館",
                sid=1,
                day=date(2026, 9, 1),
                max_retries=20,
                interval_s=0.01,
            )
        )
        assert calls["click"] == 1
        assert calls["nav"] == 0
        assert calls["select"] == 1

    def test_retry_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _page_mock()
        state = {"clicks": 0, "nav": 0}

        async def avail(page: Any, slots: list[str], venue_type: str) -> bool:
            return True

        async def select(page: Any, slot: str, venue_type: str, **kwargs: Any) -> bool:
            return True

        async def click(page: Any, **kwargs: Any) -> bool:
            state["clicks"] += 1
            return True

        async def wait_after(page: Any, *, timeout_ms: int = 0) -> str:
            return "proceed" if state["clicks"] >= 2 else "unavailable"

        async def can_input(page: Any) -> bool:
            return state["clicks"] >= 2

        async def hint(page: Any) -> bool:
            return state["clicks"] < 2

        async def disabled(page: Any) -> bool:
            return False

        async def clickable(page: Any) -> bool:
            return False

        async def nav(
            page: Any, venue_name: str, sid: int, day: date, **kwargs: Any
        ) -> None:
            state["nav"] += 1

        monkeypatch.setattr(bk, "_time_slots_available_on_page", avail)
        monkeypatch.setattr(bk, "_select_time_cell", select)
        monkeypatch.setattr(bk, "_click_yoyaku_moshikomi", click)
        monkeypatch.setattr(bk, "wait_after_yoyaku_moshikomi", wait_after)
        monkeypatch.setattr(bk, "_can_click_yoyaku_input", can_input)
        monkeypatch.setattr(bk, "_page_has_unavailable_hint", hint)
        monkeypatch.setattr(bk, "_yoyaku_button_disabled", disabled)
        monkeypatch.setattr(bk, "_yoyaku_button_clickable", clickable)
        monkeypatch.setattr(bk, "_navigate_to_time_select", nav)

        steps: list[str] = []

        def capture_step(group_id: str, venue_name: str, message: str, **kwargs: Any) -> None:
            steps.append(message)

        monkeypatch.setattr(bk, "_step_log", capture_step)

        asyncio.run(
            bk.apply_for_slots_with_retry(
                page,
                slots=["17:00-19:00"],
                venue_type="hall",
                venue_name="公民館",
                sid=2,
                day=date(2026, 9, 15),
                group_id="GID",
                max_retries=5,
                interval_s=0.01,
            )
        )
        assert state["clicks"] == 2
        assert state["nav"] == 1
        assert any(s.startswith("再試行 1/5: まだ予約不可") for s in steps)

    def test_exhaust_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _page_mock()
        nav_n = {"n": 0}

        async def avail(page: Any, slots: list[str], venue_type: str) -> bool:
            return False

        async def select(page: Any, slot: str, venue_type: str, **kwargs: Any) -> bool:
            return False

        async def click(page: Any, **kwargs: Any) -> bool:
            return False

        async def wait_after(page: Any, *, timeout_ms: int = 0) -> str:
            return "timeout"

        async def can_input(page: Any) -> bool:
            return False

        async def hint(page: Any) -> bool:
            return True

        async def disabled(page: Any) -> bool:
            return True

        async def clickable(page: Any) -> bool:
            return False

        async def nav(
            page: Any, venue_name: str, sid: int, day: date, **kwargs: Any
        ) -> None:
            nav_n["n"] += 1

        monkeypatch.setattr(bk, "_time_slots_available_on_page", avail)
        monkeypatch.setattr(bk, "_select_time_cell", select)
        monkeypatch.setattr(bk, "_click_yoyaku_moshikomi", click)
        monkeypatch.setattr(bk, "wait_after_yoyaku_moshikomi", wait_after)
        monkeypatch.setattr(bk, "_can_click_yoyaku_input", can_input)
        monkeypatch.setattr(bk, "_page_has_unavailable_hint", hint)
        monkeypatch.setattr(bk, "_yoyaku_button_disabled", disabled)
        monkeypatch.setattr(bk, "_yoyaku_button_clickable", clickable)
        monkeypatch.setattr(bk, "_navigate_to_time_select", nav)

        with pytest.raises(RuntimeError, match=bk.MSG_APPLY_RETRY_EXHAUSTED):
            asyncio.run(
                bk.apply_for_slots_with_retry(
                    page,
                    slots=["09:00-11:00"],
                    venue_type="gym",
                    venue_name="体育館",
                    sid=1,
                    day=date(2026, 9, 1),
                    max_retries=3,
                    interval_s=0.01,
                )
            )
        # refreshed between attempts 1→2 and 2→3 (not after final)
        assert nav_n["n"] == 2

    def test_session_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _page_mock()
        page.url = "https://example.test/login"

        with pytest.raises(RuntimeError, match=bk.MSG_SESSION_EXPIRED):
            asyncio.run(
                bk.apply_for_slots_with_retry(
                    page,
                    slots=["09:00-11:00"],
                    venue_type="gym",
                    venue_name="体育館",
                    sid=1,
                    day=date(2026, 9, 1),
                    max_retries=5,
                    interval_s=0.01,
                )
            )

    def test_session_expired_after_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _page_mock()

        async def avail(page: Any, slots: list[str], venue_type: str) -> bool:
            return True

        async def select(page: Any, slot: str, venue_type: str, **kwargs: Any) -> bool:
            return True

        async def click(page: Any, **kwargs: Any) -> bool:
            return True

        async def wait_after(page: Any, *, timeout_ms: int = 0) -> str:
            return "login"

        async def can_input(page: Any) -> bool:
            return False

        async def hint(page: Any) -> bool:
            return False

        async def disabled(page: Any) -> bool:
            return False

        async def clickable(page: Any) -> bool:
            return False

        monkeypatch.setattr(bk, "_time_slots_available_on_page", avail)
        monkeypatch.setattr(bk, "_select_time_cell", select)
        monkeypatch.setattr(bk, "_click_yoyaku_moshikomi", click)
        monkeypatch.setattr(bk, "wait_after_yoyaku_moshikomi", wait_after)
        monkeypatch.setattr(bk, "_can_click_yoyaku_input", can_input)
        monkeypatch.setattr(bk, "_page_has_unavailable_hint", hint)
        monkeypatch.setattr(bk, "_yoyaku_button_disabled", disabled)
        monkeypatch.setattr(bk, "_yoyaku_button_clickable", clickable)

        with pytest.raises(RuntimeError, match=bk.MSG_SESSION_EXPIRED):
            asyncio.run(
                bk.apply_for_slots_with_retry(
                    page,
                    slots=["09:00-11:00"],
                    venue_type="gym",
                    venue_name="体育館",
                    sid=1,
                    day=date(2026, 9, 1),
                    max_retries=5,
                    interval_s=0.01,
                )
            )

    def test_skip_cell_reselect_when_moshikomi_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _page_mock()
        calls = {"select": 0, "click": 0}

        async def avail(page: Any, slots: list[str], venue_type: str) -> bool:
            return True

        async def select(page: Any, slot: str, venue_type: str, **kwargs: Any) -> bool:
            calls["select"] += 1
            return True

        async def click(page: Any, **kwargs: Any) -> bool:
            calls["click"] += 1
            return True

        async def wait_after(page: Any, *, timeout_ms: int = 0) -> str:
            return "proceed"

        async def can_input(page: Any) -> bool:
            return calls["click"] >= 1

        async def hint(page: Any) -> bool:
            return False

        async def disabled(page: Any) -> bool:
            return False

        async def clickable(page: Any) -> bool:
            return True

        monkeypatch.setattr(bk, "_time_slots_available_on_page", avail)
        monkeypatch.setattr(bk, "_select_time_cell", select)
        monkeypatch.setattr(bk, "_click_yoyaku_moshikomi", click)
        monkeypatch.setattr(bk, "wait_after_yoyaku_moshikomi", wait_after)
        monkeypatch.setattr(bk, "_can_click_yoyaku_input", can_input)
        monkeypatch.setattr(bk, "_page_has_unavailable_hint", hint)
        monkeypatch.setattr(bk, "_yoyaku_button_disabled", disabled)
        monkeypatch.setattr(bk, "_yoyaku_button_clickable", clickable)

        asyncio.run(
            bk.apply_for_slots_with_retry(
                page,
                slots=["09:00-11:00"],
                venue_type="gym",
                venue_name="体育館",
                sid=1,
                day=date(2026, 9, 1),
                max_retries=5,
                interval_s=0.01,
            )
        )
        assert calls["click"] == 1
        assert calls["select"] == 0

    def test_user_error_maps_jp(self) -> None:
        assert (
            bk.user_error_message(bk.MSG_APPLY_RETRY_EXHAUSTED, log_detail=False)
            == bk.MSG_APPLY_RETRY_EXHAUSTED
        )
        assert (
            bk.user_error_message(bk.MSG_SESSION_EXPIRED, log_detail=False)
            == bk.MSG_SESSION_EXPIRED
        )
        assert (
            bk.user_error_message(bk.MSG_CONFIRM_UNCLEAR, log_detail=False)
            == bk.MSG_CONFIRM_UNCLEAR
        )
