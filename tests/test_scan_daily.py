"""Tests for scan_daily heartbeat."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from core.scanner import send_heartbeat

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def scan_daily():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    path = ROOT / "scripts" / "scan_daily.py"
    spec = importlib.util.spec_from_file_location("scan_daily_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scan_daily_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestSendHeartbeat:
    def test_calls_get_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: dict[str, object] = {}

        def fake_get(url: str, timeout: float = 10.0):  # noqa: ANN202
            called["url"] = url
            called["timeout"] = timeout
            return MagicMock()

        monkeypatch.setattr("core.scanner.httpx.get", fake_get)
        send_heartbeat("https://hc.example/ping")
        assert called == {"url": "https://hc.example/ping", "timeout": 10.0}

    def test_uses_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: dict[str, object] = {}

        def fake_get(url: str, timeout: float = 10.0):  # noqa: ANN202
            called["url"] = url
            return MagicMock()

        monkeypatch.setenv("CFG_D2", "https://hc.example/d2")
        monkeypatch.setattr("core.scanner.httpx.get", fake_get)
        send_heartbeat(env_key="CFG_D2")
        assert called["url"] == "https://hc.example/d2"

    def test_ping_failure_logs_warning_no_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            raise httpx.TimeoutException("timeout")

        monkeypatch.setattr("core.scanner.httpx.get", boom)
        with caplog.at_level(logging.WARNING, logger="core.scanner"):
            send_heartbeat("https://hc.example/ping")
        assert any("heartbeat ping failed:" in r.message for r in caplog.records)

    def test_missing_cfg_skips_debug(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("CFG_D1", raising=False)
        spy = MagicMock()
        monkeypatch.setattr("core.scanner.httpx.get", spy)
        with caplog.at_level(logging.DEBUG, logger="core.scanner"):
            send_heartbeat(env_key="CFG_D1")
        spy.assert_not_called()
        assert any("heartbeat未配置，跳过" in r.message for r in caplog.records)


class TestMainHeartbeat:
    def test_main_pings_after_successful_run(
        self, scan_daily, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scan_daily, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(scan_daily, "setup_logging", lambda: None)
        monkeypatch.setattr(scan_daily, "check_mail_cfg", lambda: None)
        monkeypatch.setattr(
            scan_daily,
            "run_task",
            lambda **_k: {
                "end": None,
                "keys": set(),
                "lines": [],
                "changed": False,
                "mailed": False,
                "overall_ok": True,
                "fail_count": 0,
            },
        )
        monkeypatch.setattr(sys, "argv", ["scan_daily.py"])
        ping = MagicMock()
        monkeypatch.setattr(scan_daily, "send_heartbeat", ping)
        scan_daily.main()
        ping.assert_called_once_with(env_key="CFG_D1")

    def test_main_skips_heartbeat_when_run_task_raises(
        self, scan_daily, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scan_daily, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(scan_daily, "setup_logging", lambda: None)
        monkeypatch.setattr(scan_daily, "check_mail_cfg", lambda: None)

        def boom(**_k):  # noqa: ANN003, ANN202
            raise RuntimeError("scan failed")

        monkeypatch.setattr(scan_daily, "run_task", boom)
        monkeypatch.setattr(sys, "argv", ["scan_daily.py"])
        ping = MagicMock()
        monkeypatch.setattr(scan_daily, "send_heartbeat", ping)
        with pytest.raises(RuntimeError, match="scan failed"):
            scan_daily.main()
        ping.assert_not_called()
