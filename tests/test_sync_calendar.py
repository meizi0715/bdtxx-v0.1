"""Tests for sync_calendar heartbeat, wrap-up, failures, and summary mail."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.booking import (
    MSG_LOGIN_FAIL,
    MSG_SESSION_EXPIRED,
    MSG_UNEXPECTED,
)
from core.calendar_sync import (
    build_summary_mail,
    day_work_finished,
    load_sync_state,
    save_sync_state,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sync_calendar():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    path = ROOT / "scripts" / "sync_calendar.py"
    spec = importlib.util.spec_from_file_location("sync_calendar_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sync_calendar_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


_GROUPS = {
    "GROUP1": {"login_id": "a", "name": "甲", "password": "x", "type": "gym"},
    "GROUP2": {"login_id": "b", "name": "乙", "password": "y", "type": "hall"},
}


def _write_state(
    path: Path,
    *,
    day: date,
    done: list[str],
    attempts: int,
    summary_sent: bool,
    failures: dict[str, str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date": day.isoformat(),
                "done": done,
                "attempts": attempts,
                "summary_sent": summary_sent,
                "failures": failures or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def sync_run_env(sync_calendar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Common stubs for ``_run`` unit tests (state file + groups + sync)."""
    import core.calendar_sync as calendar_sync

    today = date(2026, 8, 7)
    state_path = tmp_path / "sync_calendar_state.json"
    # ``_run`` passes PATH_SYNC_STATE into load_*; bare save_* uses the
    # calendar_sync module default — patch both so tests never touch data/.
    monkeypatch.setattr(sync_calendar, "PATH_SYNC_STATE", state_path)
    monkeypatch.setattr(calendar_sync, "PATH_SYNC_STATE", state_path)
    monkeypatch.setattr(sync_calendar, "today_tokyo", lambda: today)
    monkeypatch.setattr(sync_calendar, "load_groups", lambda: dict(_GROUPS))
    sync_mock = MagicMock(return_value={"ok": True, "error": ""})
    monkeypatch.setattr(sync_calendar, "sync_one_group", sync_mock)
    monkeypatch.setattr(sync_calendar, "update_stats_calendar", MagicMock())
    monkeypatch.setattr(sync_calendar, "_maybe_send_summary", MagicMock())
    return {
        "mod": sync_calendar,
        "today": today,
        "state_path": state_path,
        "sync_mock": sync_mock,
    }


class TestDayWorkFinished:
    def test_attempts_threshold_is_total_plus_four(self) -> None:
        total = 14
        assert not day_work_finished({"done": [], "attempts": total + 3}, total)
        assert day_work_finished({"done": [], "attempts": total + 4}, total)

    def test_done_covers_all_groups(self) -> None:
        assert day_work_finished(
            {"done": [f"G{i}" for i in range(14)], "attempts": 1}, 14
        )


class TestSummaryMailFailures:
    def _groups_14(self) -> dict[str, dict[str, str]]:
        # Names chosen so first chars match the mail example style.
        names = {
            "GROUP4": ("Ｆクラブ", "gym"),
            "GROUP9": ("エクラブ", "hall"),
            "GROUP13": ("星クラブ", "hall"),
        }
        out: dict[str, dict[str, str]] = {}
        for i in range(1, 15):
            ref = f"GROUP{i}"
            if ref in names:
                name, gtype = names[ref]
            else:
                name, gtype = (f"{i}クラブ", "gym" if i <= 7 else "hall")
            out[ref] = {
                "login_id": f"id{i}",
                "name": name,
                "password": "x",
                "type": gtype,
            }
        return out

    def test_all_success_hides_failure_list(self) -> None:
        groups = self._groups_14()
        refs = sorted(groups.keys())
        subject, body = build_summary_mail(
            14,
            14,
            when=date(2026, 8, 7),
            done_refs=refs,
            all_refs=refs,
            failures={},
            groups=groups,
        )
        assert subject == "予約履歴カレンダー同期完了(8/7)"
        assert body == (
            "本日の同期結果\n"
            "成功: 14件 / 全14件\n"
            "失敗: 0件\n"
        )
        assert "・GROUP" not in body

    def test_lists_three_failures_with_type_initial_reason(self) -> None:
        groups = self._groups_14()
        refs = sorted(groups.keys())
        failed = {"GROUP4", "GROUP9", "GROUP13"}
        done = [r for r in refs if r not in failed]
        failures = {
            "GROUP4": MSG_LOGIN_FAIL,
            "GROUP9": MSG_UNEXPECTED,
            "GROUP13": MSG_SESSION_EXPIRED,
        }
        _subject, body = build_summary_mail(
            len(done),
            14,
            when=date(2026, 8, 7),
            done_refs=done,
            all_refs=refs,
            failures=failures,
            groups=groups,
        )
        assert "成功: 11件 / 全14件" in body
        assert "失敗: 3件" in body
        assert (
            f"・GROUP4（体育館・Ｆ）：{MSG_LOGIN_FAIL}" in body
        )
        assert (
            f"・GROUP9（公民館・エ）：{MSG_UNEXPECTED}" in body
        )
        assert (
            f"・GROUP13（公民館・星）：{MSG_SESSION_EXPIRED}" in body
        )
        # Order follows all_refs (sorted keys), not insertion order of failures.
        pos4 = body.index("・GROUP4")
        pos9 = body.index("・GROUP9")
        pos13 = body.index("・GROUP13")
        assert pos13 < pos4 < pos9  # GROUP13 < GROUP4 < GROUP9 lexicographically


class TestFailureStateRecording:
    def test_failure_reason_saved_and_overwritten(
        self, sync_run_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=[],
            attempts=0,
            summary_sent=False,
            failures={"GROUP2": MSG_LOGIN_FAIL},
        )
        env["sync_mock"].return_value = {
            "ok": False,
            "error": MSG_SESSION_EXPIRED,
        }
        env["mod"]._run("GROUP2")
        st = _read_state(env["state_path"])
        assert st["failures"]["GROUP2"] == MSG_SESSION_EXPIRED
        assert "GROUP2" not in st["done"]
        assert st["attempts"] == 1

    def test_success_clears_prior_failure(self, sync_run_env) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=[],
            attempts=1,
            summary_sent=False,
            failures={"GROUP1": MSG_LOGIN_FAIL},
        )
        env["sync_mock"].return_value = {"ok": True, "error": ""}
        env["mod"]._run("GROUP1")
        st = _read_state(env["state_path"])
        assert st["done"] == ["GROUP1"]
        assert "GROUP1" not in st.get("failures", {})

    def test_load_save_roundtrip_failures(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        day = date(2026, 8, 7)
        state = {
            "date": day.isoformat(),
            "done": ["GROUP1"],
            "attempts": 3,
            "summary_sent": False,
            "failures": {"GROUP2": MSG_UNEXPECTED},
        }
        save_sync_state(state, path)
        loaded = load_sync_state(path, today=day)
        assert loaded["failures"] == {"GROUP2": MSG_UNEXPECTED}


class TestSyncCalendarHeartbeat:
    def test_main_pings_after_quiet_exit(
        self, sync_calendar, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sync_calendar, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(sync_calendar, "setup_logging", lambda: None)
        monkeypatch.setattr(sync_calendar, "_run", lambda *_a, **_k: None)
        monkeypatch.setattr(sys, "argv", ["sync_calendar.py"])
        ping = MagicMock()
        monkeypatch.setattr(sync_calendar, "send_heartbeat", ping)
        sync_calendar.main()
        ping.assert_called_once_with(env_key="CFG_D2")

    def test_main_skips_heartbeat_when_run_raises(
        self, sync_calendar, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sync_calendar, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(sync_calendar, "setup_logging", lambda: None)

        def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("sync boom")

        monkeypatch.setattr(sync_calendar, "_run", boom)
        monkeypatch.setattr(sys, "argv", ["sync_calendar.py"])
        ping = MagicMock()
        monkeypatch.setattr(sync_calendar, "send_heartbeat", ping)
        with pytest.raises(RuntimeError, match="sync boom"):
            sync_calendar.main()
        ping.assert_not_called()


class TestGroupRefBypassesWrapUp:
    def test_random_mode_quiet_exits_when_summary_sent(self, sync_run_env) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=["GROUP1", "GROUP2"],
            attempts=5,
            summary_sent=True,
        )
        env["mod"]._run("")
        env["sync_mock"].assert_not_called()

    def test_random_mode_quiet_exits_when_attempts_exhausted(self, sync_run_env) -> None:
        env = sync_run_env
        # total=2 → finish when attempts >= 6 (total+4)
        _write_state(
            env["state_path"],
            day=env["today"],
            done=["GROUP1"],
            attempts=6,
            summary_sent=False,
        )
        env["mod"]._run("")
        env["sync_mock"].assert_not_called()

    def test_random_mode_still_runs_before_new_attempts_cap(self, sync_run_env) -> None:
        env = sync_run_env
        # total=2 → total+3=5 is still under the new cap of total+4=6
        _write_state(
            env["state_path"],
            day=env["today"],
            done=["GROUP1"],
            attempts=5,
            summary_sent=False,
        )
        env["mod"]._run("")
        env["sync_mock"].assert_called_once()

    def test_group_ref_runs_when_summary_sent(self, sync_run_env) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=["GROUP1", "GROUP2"],
            attempts=5,
            summary_sent=True,
        )
        env["mod"]._run("GROUP1")
        env["sync_mock"].assert_called_once_with(
            "GROUP1", headless=True, today=env["today"]
        )
        st = _read_state(env["state_path"])
        assert st["attempts"] == 6
        assert "GROUP1" in st["done"]

    def test_group_ref_runs_when_attempts_exhausted(self, sync_run_env) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=[],
            attempts=6,
            summary_sent=False,
        )
        env["mod"]._run("GROUP2")
        env["sync_mock"].assert_called_once_with(
            "GROUP2", headless=True, today=env["today"]
        )
        st = _read_state(env["state_path"])
        assert st["attempts"] == 7
        assert st["done"] == ["GROUP2"]

    def test_group_ref_retries_group_already_in_done(self, sync_run_env) -> None:
        env = sync_run_env
        _write_state(
            env["state_path"],
            day=env["today"],
            done=["GROUP1"],
            attempts=1,
            summary_sent=False,
        )
        env["mod"]._run("GROUP1")
        env["sync_mock"].assert_called_once_with(
            "GROUP1", headless=True, today=env["today"]
        )
        st = _read_state(env["state_path"])
        assert st["attempts"] == 2
        assert st["done"] == ["GROUP1"]


class TestSummaryMailUsesStateFailures:
    def test_maybe_send_summary_includes_failure_details(
        self, sync_calendar, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import core.calendar_sync as calendar_sync

        today = date(2026, 8, 7)
        state_path = tmp_path / "sync_calendar_state.json"
        monkeypatch.setattr(sync_calendar, "PATH_SYNC_STATE", state_path)
        monkeypatch.setattr(calendar_sync, "PATH_SYNC_STATE", state_path)
        monkeypatch.setattr(sync_calendar, "today_tokyo", lambda: today)

        groups = {
            "GROUP4": {"name": "Ｆクラブ", "type": "gym"},
            "GROUP9": {"name": "エクラブ", "type": "hall"},
            "GROUP13": {"name": "星クラブ", "type": "hall"},
        }
        for i in range(1, 15):
            ref = f"GROUP{i}"
            if ref not in groups:
                groups[ref] = {"name": f"{i}名", "type": "gym"}

        failed = {"GROUP4", "GROUP9", "GROUP13"}
        done = [r for r in sorted(groups) if r not in failed]
        state = {
            "date": today.isoformat(),
            "done": done,
            "attempts": 18,  # total+4 for 14
            "summary_sent": False,
            "failures": {
                "GROUP4": MSG_LOGIN_FAIL,
                "GROUP9": MSG_UNEXPECTED,
                "GROUP13": MSG_SESSION_EXPIRED,
            },
        }
        save_sync_state(state, state_path)

        sent: list[tuple[str, str]] = []

        def fake_send(subject: str, body: str) -> bool:
            sent.append((subject, body))
            return True

        monkeypatch.setattr(sync_calendar, "check_mail_cfg", lambda: None)
        monkeypatch.setattr(sync_calendar, "send_text_msg", fake_send)

        sync_calendar._maybe_send_summary(state, 14, groups)
        assert len(sent) == 1
        _subject, body = sent[0]
        assert "成功: 11件 / 全14件" in body
        assert "失敗: 3件" in body
        assert f"・GROUP4（体育館・Ｆ）：{MSG_LOGIN_FAIL}" in body
        assert f"・GROUP9（公民館・エ）：{MSG_UNEXPECTED}" in body
        assert f"・GROUP13（公民館・星）：{MSG_SESSION_EXPIRED}" in body
        assert _read_state(state_path)["summary_sent"] is True
