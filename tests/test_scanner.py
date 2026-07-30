"""Tests for core.scanner helpers and HTTP collect path."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import jpholiday
import pytest

from core import scanner as m


class TestHelper1:
    def test_mid(self) -> None:
        assert m.helper_1(date(2026, 7, 15)) == date(2026, 8, 31)

    def test_cross_year(self) -> None:
        assert m.helper_1(date(2026, 12, 31)) == date(2027, 1, 31)

    def test_leap(self) -> None:
        assert m.helper_1(date(2024, 1, 10)) == date(2024, 2, 29)


class TestHelper2:
    def test_mid(self) -> None:
        assert m.helper_2(date(2026, 7, 15)) == date(2026, 9, 30)

    def test_cross_year(self) -> None:
        assert m.helper_2(date(2026, 12, 31)) == date(2027, 2, 28)

    def test_leap_boundary(self) -> None:
        assert m.helper_2(date(2023, 12, 15)) == date(2024, 2, 29)


class TestHelper3:
    def test_weekday(self) -> None:
        d = date(2026, 7, 15)
        assert d.weekday() < 5 and not jpholiday.is_holiday(d)
        assert m.helper_3(d) is False

    def test_sat(self) -> None:
        assert m.helper_3(date(2026, 7, 18)) is True

    def test_holiday(self) -> None:
        assert m.helper_3(date(2026, 1, 1)) is True


class TestHelper4:
    def test_normal(self) -> None:
        assert m.helper_4(datetime(2026, 7, 15, 18, 0)) == date(2026, 8, 31)

    def test_last_before_cut(self) -> None:
        assert m.helper_4(datetime(2026, 7, 31, 16, 59)) == date(2026, 8, 31)

    def test_last_at_cut(self) -> None:
        assert m.helper_4(datetime(2026, 7, 31, 17, 0)) == date(2026, 9, 30)

    def test_cross_year_switch(self) -> None:
        assert m.helper_4(datetime(2026, 12, 31, 17, 0)) == date(2027, 2, 28)

    def test_cross_year_before(self) -> None:
        assert m.helper_4(datetime(2026, 12, 31, 10, 0)) == date(2027, 1, 31)

    def test_date_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _F(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001, ANN206
                return cls(2026, 7, 31, 18, 0, 0)

        monkeypatch.setattr(m, "datetime", _F)
        assert m.helper_4(date(2026, 7, 31)) == date(2026, 9, 30)


class TestHelper5:
    def test_weekday_before_bound(self) -> None:
        # Wed
        assert m.helper_5(date(2026, 7, 15), "18:30-19:00") is False
        assert m.helper_5(date(2026, 7, 15), "17:00-17:30") is False

    def test_weekday_at_bound(self) -> None:
        assert m.helper_5(date(2026, 7, 15), "19:00-19:30") is True
        assert m.helper_5(date(2026, 7, 15), "21:00-21:30") is True

    def test_weekend_all_day(self) -> None:
        assert m.helper_5(date(2026, 7, 18), "09:00-09:30") is True


class TestHelper6:
    def test_span(self) -> None:
        assert m.helper_6(date(2026, 7, 30), date(2026, 9, 30)) == [
            date(2026, 7, 1),
            date(2026, 8, 1),
            date(2026, 9, 1),
        ]

    def test_cross_year(self) -> None:
        assert m.helper_6(date(2026, 12, 1), date(2027, 2, 28)) == [
            date(2026, 12, 1),
            date(2027, 1, 1),
            date(2027, 2, 1),
        ]


class TestProcBC:
    def test_ready_after_delta(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        key = "v01|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({key}, t0, path_t1=p1)
        assert m.proc_c({key}, t0 + timedelta(hours=3, minutes=59), path_t1=p1) == set()
        assert m.proc_c({key}, t0 + timedelta(hours=4), path_t1=p1) == {key}

    def test_drop_missing(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        a = "v01|2026-08-01 19:00-19:30"
        b = "v02|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({a, b}, t0, path_t1=p1)
        m.proc_b({a}, t0 + timedelta(hours=1), path_t1=p1)
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert a in data and b not in data

    def test_stamp_stable(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        key = "v01|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({key}, t0, path_t1=p1)
        m.proc_b({key}, t0 + timedelta(hours=2), path_t1=p1)
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert data[key] == "2026-07-30T10:00:00"


class TestDiff:
    def test_change_detect(self, tmp_path: Path) -> None:
        p2 = tmp_path / "t2.json"
        assert m.proc_e(["a"], path_t2=p2) is True
        m.proc_f(["a"], path_t2=p2)
        assert m.proc_e(["a"], path_t2=p2) is False
        assert m.proc_e(["a", "b"], path_t2=p2) is True


class TestCollect:
    def test_collect_one_filters(self) -> None:
        client = MagicMock()

        def fake_a(client, base, tenant, base_date, fid, sid):  # noqa: ANN001, ANN202
            return [
                {
                    "date": "2026-08-05",
                    "isPast": False,
                    "spaces": [{"spaceId": 2, "status": "available"}],
                },
                {
                    "date": "2026-08-06",
                    "isPast": False,
                    "spaces": [{"spaceId": 2, "status": "available"}],
                },
                {
                    "date": "2026-08-07",
                    "isPast": False,
                    "spaces": [{"spaceId": 2, "status": "unavailable"}],
                },
            ]

        def fake_b(client, base, tenant, fid, sid, days):  # noqa: ANN001, ANN202
            # 2026-08-05 Wed, 2026-08-06 Thu
            return [
                {
                    "timeString": "18:00-18:30",
                    "details": [{"status": "available"}],
                },
                {
                    "timeString": "19:00-19:30",
                    "details": [{"status": "available"}],
                },
                {
                    "timeString": "09:00-09:30",
                    "details": [{"status": "unavailable"}],
                },
            ]

        # patch module-level fetchers
        import core.scanner as sc

        orig_a, orig_b = sc.fetch_a, sc.fetch_b
        sc.fetch_a = fake_a  # type: ignore[assignment]
        sc.fetch_b = fake_b  # type: ignore[assignment]
        try:
            keys = sc.collect_one(
                client,
                "https://example.test",
                "t1",
                {"code": "v01", "fid": 1, "sid": 2},
                date(2026, 8, 1),
                date(2026, 8, 31),
            )
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert keys == {
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-06 19:00-19:30",
        }

    def test_weekend_keeps_morning(self) -> None:
        client = MagicMock()

        def fake_a(client, base, tenant, base_date, fid, sid):  # noqa: ANN001, ANN202
            return [
                {
                    "date": "2026-08-08",  # Sat
                    "isPast": False,
                    "spaces": [{"spaceId": 2, "status": "available"}],
                }
            ]

        def fake_b(client, base, tenant, fid, sid, days):  # noqa: ANN001, ANN202
            return [
                {"timeString": "09:00-09:30", "details": [{"status": "available"}]},
                {"timeString": "19:00-19:30", "details": [{"status": "available"}]},
            ]

        import core.scanner as sc

        orig_a, orig_b = sc.fetch_a, sc.fetch_b
        sc.fetch_a = fake_a  # type: ignore[assignment]
        sc.fetch_b = fake_b  # type: ignore[assignment]
        try:
            keys = sc.collect_one(
                client,
                "https://example.test",
                "t1",
                {"code": "v03", "fid": 1, "sid": 2},
                date(2026, 8, 1),
                date(2026, 8, 31),
            )
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert keys == {
            "v03|2026-08-08 09:00-09:30",
            "v03|2026-08-08 19:00-19:30",
        }


class TestRunTask:
    def test_pipeline_mail_on_change(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        sent: list[list[str]] = []

        def fake_send(lines, flag_x, when=None):  # noqa: ANN001, ANN202
            sent.append(list(lines))
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")

        t0 = datetime(2026, 7, 30, 10, 0, 0)
        key = "v01|2026-08-01 19:00-19:30"

        def fake_collect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            return {key}

        monkeypatch.setattr(m, "collect_all", fake_collect)

        # first run: not ready yet (<4h)
        r1 = m.run_task(
            t0,
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            send=True,
        )
        assert r1["ready"] == set()
        assert sent == []

        # second run: ready + changed -> mail
        r2 = m.run_task(
            t0 + timedelta(hours=4),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            send=True,
        )
        assert r2["ready"] == {key}
        assert r2["mailed"] is True
        assert sent == [[key]]

        # third run: unchanged -> no mail
        r3 = m.run_task(
            t0 + timedelta(hours=5),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            send=True,
        )
        assert r3["changed"] is False
        assert r3["mailed"] is False
        assert len(sent) == 1
