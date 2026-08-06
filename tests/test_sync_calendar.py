"""Tests for sync_calendar heartbeat on normal completion."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
