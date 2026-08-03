"""Tests for booking error handling and result mail formatting."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.booking import (
    MSG_ACCEPT_TIMEOUT,
    MSG_LOGIN_FAIL,
    MSG_PURPOSE_MISSING,
    MSG_SLOT_GONE,
    MSG_UI_FAIL,
    MSG_UNEXPECTED,
    SiteDialogBlocked,
    format_book_error,
    format_results_mail,
    run_tasks,
    user_error_message,
)


class TestUserErrorMessage:
    def test_login(self) -> None:
        assert (
            user_error_message("login rejected by page", log_detail=False)
            == MSG_LOGIN_FAIL
        )
        assert format_book_error(RuntimeError("login failed")) == MSG_LOGIN_FAIL

    def test_slot_gone(self) -> None:
        assert (
            user_error_message("slot no longer available", log_detail=False)
            == MSG_SLOT_GONE
        )
        assert (
            user_error_message(
                "target slot unavailable after polling", log_detail=False
            )
            == MSG_SLOT_GONE
        )

    def test_checkbox_ui(self) -> None:
        assert (
            user_error_message(
                "checkbox click did not register after retry", log_detail=False
            )
            == MSG_UI_FAIL
        )
        assert format_book_error(SiteDialogBlocked()) == MSG_UI_FAIL

    def test_purpose(self) -> None:
        assert (
            user_error_message(
                "purpose option not found: バドミントン", log_detail=False
            )
            == MSG_PURPOSE_MISSING
        )

    def test_accept_timeout(self) -> None:
        assert (
            user_error_message(
                "9am wait aborted: too early (>10 minutes before target)",
                log_detail=False,
            )
            == MSG_ACCEPT_TIMEOUT
        )

        class TimeoutError(Exception):
            pass

        assert format_book_error(TimeoutError("waiting for selector")) == MSG_ACCEPT_TIMEOUT

    def test_unexpected_fallback(self) -> None:
        assert (
            user_error_message(
                "injected unexpected error for test", log_detail=False
            )
            == MSG_UNEXPECTED
        )
        assert format_book_error(RuntimeError("weird boom")) == MSG_UNEXPECTED

    def test_idempotent_japanese(self) -> None:
        assert (
            user_error_message(MSG_LOGIN_FAIL, log_detail=False) == MSG_LOGIN_FAIL
        )


class TestFormatResultsMail:
    def _ok(self, **kw: Any) -> dict[str, Any]:
        base = {
            "success": True,
            "venue_code": "v08",
            "venue_name": "中央公民館",
            "date": "2026-08-09",
            "time_slot": "17:00-21:00",
            "group_id": "gid1",
            "group_name": "チーム1",
            "error_message": "",
        }
        base.update(kw)
        return base

    def _ng(self, **kw: Any) -> dict[str, Any]:
        base = {
            "success": False,
            "venue_code": "v02",
            "venue_name": "体育館",
            "date": "2026-08-28",
            "time_slot": "17:00-19:00",
            "group_id": "gid2",
            "group_name": "チーム2",
            "error_message": MSG_SLOT_GONE,
        }
        base.update(kw)
        return base

    def test_both_success_and_failure(self) -> None:
        _, body = format_results_mail(
            [
                self._ok(),
                self._ng(),
                self._ng(
                    time_slot="11:00-13:00",
                    error_message=MSG_LOGIN_FAIL,
                    venue_name="公民館",
                ),
            ]
        )
        assert "合計: 3件" in body
        assert "✅ 成功：1件" in body
        assert "❌ 失敗：2件" in body
        assert MSG_SLOT_GONE in body
        assert MSG_LOGIN_FAIL in body
        assert "login rejected" not in body
        assert "slot no longer available" not in body
        assert "🕒 時間帯: 17:00～21:00" in body

    def test_success_only_skips_failure_section(self) -> None:
        _, body = format_results_mail([self._ok()])
        assert "合計: 1件" in body
        assert "✅ 成功：1件" in body
        assert "❌ 失敗：0件" in body
        assert "✅ 成功\n【1】" in body
        assert "❌ 失敗\n【" not in body

    def test_failure_only_skips_success_section(self) -> None:
        _, body = format_results_mail([self._ng()])
        assert "合計: 1件" in body
        assert "✅ 成功：0件" in body
        assert "❌ 失敗：1件" in body
        assert "❌ 失敗\n【1】" in body
        assert "✅ 成功\n【" not in body

    def test_empty_results(self) -> None:
        _, body = format_results_mail([])
        assert "合計: 0件" in body
        assert "✅ 成功：0件" in body
        assert "❌ 失敗：0件" in body
        assert "✅ 成功\n【" not in body
        assert "❌ 失敗\n【" not in body

    def test_missing_group_name_fallback(self) -> None:
        _, body = format_results_mail([self._ng(group_name="")])
        assert "👤 団体: gid2 (未登録)" in body


class TestRunTasksIsolation:
    def test_escaped_exception_becomes_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(task: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("should not escape")

        monkeypatch.setattr("core.booking.book_one", boom)
        tasks = [
            {
                "venue_code": "v08",
                "date": "2026-08-09",
                "time_slots": ["09:00-11:00"],
                "group_id": "g1",
                "group_name": "団1",
                "password": "x",
            },
            {
                "venue_code": "v02",
                "date": "2026-08-09",
                "time_slots": ["17:00-19:00"],
                "group_id": "g2",
                "group_name": "団2",
                "password": "y",
            },
        ]
        results = asyncio.run(run_tasks(tasks, wait_9am=False))
        assert len(results) == 2
        assert all(r["success"] is False for r in results)
        assert all(r.get("error_message") == MSG_UNEXPECTED for r in results)
        subject, body = format_results_mail(results)
        assert "❌ 失敗：2件" in body
        assert MSG_UNEXPECTED in body
        assert "should not escape" not in body
        assert subject.startswith("自動予約結果")
