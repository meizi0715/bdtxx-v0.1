"""Tests for consecutive failure alerts."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import fail_watch as fw
from core import scanner as sc


class TestShouldAlert:
    def test_under_threshold(self) -> None:
        assert fw.should_alert(0) is False
        assert fw.should_alert(1) is False
        assert fw.should_alert(2) is False

    def test_first_at_three(self) -> None:
        assert fw.should_alert(3) is True

    def test_every_six_after(self) -> None:
        for n in range(4, 9):
            assert fw.should_alert(n) is False
        assert fw.should_alert(9) is True
        for n in range(10, 15):
            assert fw.should_alert(n) is False
        assert fw.should_alert(15) is True


class TestApplyOutcome:
    def test_increment_no_alert_before_three(self, tmp_path: Path) -> None:
        p = tmp_path / "fail_count.json"
        n, alert = fw.apply_outcome(False, p)
        assert n == 1 and alert is False
        n, alert = fw.apply_outcome(False, p)
        assert n == 2 and alert is False
        assert json.loads(p.read_text(encoding="utf-8")) == {"n": 2}

    def test_alert_at_three(self, tmp_path: Path) -> None:
        p = tmp_path / "fail_count.json"
        fw.save_count(p, 2)
        n, alert = fw.apply_outcome(False, p)
        assert n == 3 and alert is True

    def test_alert_every_six_after_first(self, tmp_path: Path) -> None:
        p = tmp_path / "fail_count.json"
        fw.save_count(p, 8)
        n, alert = fw.apply_outcome(False, p)
        assert n == 9 and alert is True
        n, alert = fw.apply_outcome(False, p)
        assert n == 10 and alert is False

    def test_ok_resets(self, tmp_path: Path) -> None:
        p = tmp_path / "fail_count.json"
        fw.save_count(p, 5)
        n, alert = fw.apply_outcome(True, p)
        assert n == 0 and alert is False
        assert fw.load_count(p) == 0


class TestOverallFail:
    def test_all_venues_months_failed(self) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}, {"code": "v02", "fid": 2, "sid": 2}]
        failed = {
            ("v01", "2026-07"),
            ("v01", "2026-08"),
            ("v02", "2026-07"),
            ("v02", "2026-08"),
        }
        assert (
            sc.is_overall_fail(
                items,
                failed,
                date(2026, 7, 31),
                date(2026, 8, 31),
                base_url="https://x",
                tenant="t",
            )
            is True
        )

    def test_one_venue_ok_not_overall(self) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}, {"code": "v02", "fid": 2, "sid": 2}]
        failed = {("v01", "2026-07"), ("v01", "2026-08"), ("v02", "2026-07")}
        # v02 August not failed → not overall
        assert (
            sc.is_overall_fail(
                items,
                failed,
                date(2026, 7, 31),
                date(2026, 8, 31),
                base_url="https://x",
                tenant="t",
            )
            is False
        )

    def test_missing_base_is_fail(self) -> None:
        assert (
            sc.is_overall_fail(
                [{"code": "v01", "fid": 1, "sid": 1}],
                set(),
                date(2026, 7, 1),
                date(2026, 8, 31),
                base_url="",
                tenant="t",
            )
            is True
        )


class TestRunTaskFailWatch:
    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        failed: set[tuple[str, str]],
        items: list[dict],
    ) -> dict:
        alerts: list[int] = []
        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: True)
        monkeypatch.setattr(
            "core.notifier.send_alert_msg",
            lambda n: alerts.append(n) or True,
        )
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(sc, "collect_all", lambda *a, **k: (set(), failed))
        (tmp_path / "daily_sent.json").write_text(
            '{"last_sent_date": "2026-07-31"}', encoding="utf-8"
        )
        r = sc.run_task(
            datetime(2026, 7, 31, 10, 0, 0),
            client=MagicMock(),
            items=items,
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=tmp_path / "daily_sent.json",
            send=True,
        )
        r["_alerts"] = alerts
        return r

    def test_three_fails_trigger_alert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}]
        failed = {("v01", "2026-07"), ("v01", "2026-08")}
        alerts_all: list[int] = []
        for _ in range(3):
            r = self._run(tmp_path, monkeypatch, failed=failed, items=items)
            alerts_all.extend(r["_alerts"])
        assert r["fail_count"] == 3
        assert r["overall_ok"] is False
        assert alerts_all == [3]

    def test_two_fails_no_alert(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}]
        failed = {("v01", "2026-07"), ("v01", "2026-08")}
        alerts_all: list[int] = []
        for _ in range(2):
            r = self._run(tmp_path, monkeypatch, failed=failed, items=items)
            alerts_all.extend(r["_alerts"])
        assert r["fail_count"] == 2
        assert alerts_all == []

    def test_alert_again_at_nine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}]
        failed = {("v01", "2026-07"), ("v01", "2026-08")}
        alerts_all: list[int] = []
        for _ in range(9):
            r = self._run(tmp_path, monkeypatch, failed=failed, items=items)
            alerts_all.extend(r["_alerts"])
        assert r["fail_count"] == 9
        assert alerts_all == [3, 9]

    def test_recover_resets(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [{"code": "v01", "fid": 1, "sid": 1}]
        failed = {("v01", "2026-07"), ("v01", "2026-08")}
        for _ in range(2):
            self._run(tmp_path, monkeypatch, failed=failed, items=items)
        # recover: no month failures
        r = self._run(tmp_path, monkeypatch, failed=set(), items=items)
        assert r["overall_ok"] is True
        assert r["fail_count"] == 0
        assert r["_alerts"] == []
        # fail again — counter starts over, no alert yet
        r2 = self._run(tmp_path, monkeypatch, failed=failed, items=items)
        assert r2["fail_count"] == 1
        assert r2["_alerts"] == []

    def test_exception_counts_as_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        alerts: list[int] = []
        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: True)
        monkeypatch.setattr(
            "core.notifier.send_alert_msg",
            lambda n: alerts.append(n) or True,
        )
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")

        def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("boom")

        monkeypatch.setattr(sc, "collect_all", boom)
        pf = tmp_path / "fail_count.json"
        (tmp_path / "daily_sent.json").write_text(
            '{"last_sent_date": "2026-07-31"}', encoding="utf-8"
        )
        for _ in range(3):
            with pytest.raises(RuntimeError):
                sc.run_task(
                    datetime(2026, 7, 31, 10, 0, 0),
                    client=MagicMock(),
                    items=[{"code": "v01", "fid": 1, "sid": 1}],
                    path_t1=tmp_path / "t1.json",
                    path_t2=tmp_path / "t2.json",
                    path_sup=tmp_path / "sup.json",
                    path_fail=pf,
                    path_daily_sent=tmp_path / "daily_sent.json",
                    send=True,
                )
        assert fw.load_count(pf) == 3
        assert alerts == [3]
