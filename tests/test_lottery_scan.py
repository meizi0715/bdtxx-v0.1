"""Tests for lottery_scan heartbeat on normal completion."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def lottery_scan():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    path = ROOT / "scripts" / "lottery_scan.py"
    spec = importlib.util.spec_from_file_location("lottery_scan_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lottery_scan_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestLotteryScanHeartbeat:
    def test_main_pings_after_unchanged_run(
        self, lottery_scan, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lottery_scan, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(lottery_scan, "setup_lottery_logging", lambda: None)
        monkeypatch.setattr(lottery_scan, "check_mail_cfg", lambda: None)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(
            lottery_scan, "load_venues", lambda *_a, **_k: {"v01": {}}
        )
        monkeypatch.setattr(
            lottery_scan,
            "collect_all",
            lambda *_a, **_k: (2026, 10, []),
        )
        monkeypatch.setattr(lottery_scan, "load_lottery_prev", lambda: {"k": 1})
        monkeypatch.setattr(
            lottery_scan, "entries_to_snapshot", lambda *_a, **_k: {"k": 1}
        )
        monkeypatch.setattr(lottery_scan, "lottery_changed", lambda *_a, **_k: False)
        monkeypatch.setattr(
            lottery_scan, "build_lottery_subject", lambda *_a, **_k: "subj"
        )
        monkeypatch.setattr(
            lottery_scan, "build_lottery_body", lambda *_a, **_k: "body\n"
        )
        monkeypatch.setattr(lottery_scan, "save_lottery_prev", lambda *_a, **_k: None)
        monkeypatch.setattr(sys, "argv", ["lottery_scan.py"])
        ping = MagicMock()
        monkeypatch.setattr(lottery_scan, "send_heartbeat", ping)
        lottery_scan.main()
        ping.assert_called_once_with(env_key="CFG_D3")

    def test_main_skips_heartbeat_when_scan_fails(
        self, lottery_scan, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lottery_scan, "load_dotenv", lambda *_a, **_k: None)
        monkeypatch.setattr(lottery_scan, "setup_lottery_logging", lambda: None)
        monkeypatch.setattr(lottery_scan, "check_mail_cfg", lambda: None)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(
            lottery_scan, "load_venues", lambda *_a, **_k: {"v01": {}}
        )

        def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("scan boom")

        monkeypatch.setattr(lottery_scan, "collect_all", boom)
        monkeypatch.setattr(sys, "argv", ["lottery_scan.py"])
        ping = MagicMock()
        monkeypatch.setattr(lottery_scan, "send_heartbeat", ping)
        with pytest.raises(SystemExit):
            lottery_scan.main()
        ping.assert_not_called()
