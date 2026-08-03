"""Tests for core.scanner helpers and HTTP collect path."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import jpholiday
import pytest

from core import scanner as m


def _mark_daily_sent(tmp_path: Path, day: str) -> Path:
    """Pre-mark daily_sent so run_task tests are not forced by first-of-day mail."""
    path = tmp_path / "daily_sent.json"
    path.write_text(
        json.dumps({"last_sent_date": day}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


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
        assert m.helper_4(datetime(2026, 7, 31, 14, 59)) == date(2026, 8, 31)

    def test_last_at_cut(self) -> None:
        assert m.helper_4(datetime(2026, 7, 31, 15, 0)) == date(2026, 9, 30)

    def test_cross_year_switch(self) -> None:
        assert m.helper_4(datetime(2026, 12, 31, 15, 0)) == date(2027, 2, 28)

    def test_cross_year_before(self) -> None:
        assert m.helper_4(datetime(2026, 12, 31, 10, 0)) == date(2027, 1, 31)

    def test_date_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(m, "now_tokyo", lambda: datetime(2026, 7, 31, 18, 0, 0))
        assert m.helper_4(date(2026, 7, 31)) == date(2026, 9, 30)

    def test_aware_datetime_uses_tokyo(self) -> None:
        # 2026-07-31 16:30 UTC == 2026-08-01 01:30 JST -> not last day of July in Tokyo
        from zoneinfo import ZoneInfo

        utc = ZoneInfo("UTC")
        aware = datetime(2026, 7, 31, 16, 30, tzinfo=utc)
        assert m.helper_4(aware) == date(2026, 9, 30)  # Aug 1 JST -> end of next month Sep


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

    def test_saturday_2026_08_01_morning(self) -> None:
        d = date(2026, 8, 1)
        assert d.weekday() == 5
        assert m.helper_3(d) is True
        assert m.helper_5(d, "09:00-11:00") is True
        assert m.helper_5(d, "09:00-09:30") is True
        # weekday same morning must fail the bound
        wed = date(2026, 7, 15)
        assert m.helper_3(wed) is False
        assert m.helper_5(wed, "09:00-11:00") is False


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


class TestLoadJson:
    def test_load_map_missing_empty_blank(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        empty = tmp_path / "empty.json"
        blank = tmp_path / "blank.json"
        empty.write_text("", encoding="utf-8")
        blank.write_text("  \n\t  \n", encoding="utf-8")
        assert m._load_map(missing) == {}
        assert m._load_map(empty) == {}
        assert m._load_map(blank) == {}

    def test_load_lines_missing_empty_blank(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        empty = tmp_path / "empty.json"
        blank = tmp_path / "blank.json"
        empty.write_text("", encoding="utf-8")
        blank.write_text("  \n\t  \n", encoding="utf-8")
        assert m._load_lines(missing) == []
        assert m._load_lines(empty) == []
        assert m._load_lines(blank) == []

    def test_load_map_valid_and_corrupt(self, tmp_path: Path) -> None:
        ok = tmp_path / "ok.json"
        bad = tmp_path / "bad.json"
        ok.write_text('{"a": "1"}', encoding="utf-8")
        bad.write_text('{"a":', encoding="utf-8")
        assert m._load_map(ok) == {"a": "1"}
        with pytest.raises(json.JSONDecodeError):
            m._load_map(bad)

    def test_load_lines_valid_and_corrupt(self, tmp_path: Path) -> None:
        ok = tmp_path / "ok.json"
        bad = tmp_path / "bad.json"
        ok.write_text('["x", "y"]', encoding="utf-8")
        bad.write_text("[1,", encoding="utf-8")
        assert m._load_lines(ok) == ["x", "y"]
        with pytest.raises(json.JSONDecodeError):
            m._load_lines(bad)


class TestProcState:
    def test_drop_missing(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        ps = tmp_path / "sup.json"
        a = "v01|2026-08-01 19:00-19:30"
        b = "v02|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({a, b}, t0, path_t1=p1, path_sup=ps)
        m.proc_b({a}, t0 + timedelta(hours=1), path_t1=p1, path_sup=ps)
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert a in data and b not in data

    def test_stamp_stable(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        ps = tmp_path / "sup.json"
        key = "v01|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({key}, t0, path_t1=p1, path_sup=ps)
        m.proc_b({key}, t0 + timedelta(hours=2), path_t1=p1, path_sup=ps)
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert data[key] == "2026-07-30T10:00:00"

    def test_failed_scope_keeps_history(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        ps = tmp_path / "sup.json"
        keep = "v08|2026-08-01 09:00-11:00"
        gone = "v01|2026-08-01 19:00-21:00"
        ok = "v02|2026-08-02 19:00-21:00"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({keep, gone, ok}, t0, path_t1=p1, path_sup=ps)
        m.proc_b(
            {ok},
            t0 + timedelta(hours=1),
            path_t1=p1,
            path_sup=ps,
            failed_scope={("v08", "2026-08-01")},
        )
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert data[keep] == "2026-07-30T10:00:00"
        assert gone not in data
        assert data[ok] == "2026-07-30T10:00:00"

    def test_failed_scope_month_protects_day_keys(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        ps = tmp_path / "sup.json"
        keep = "v08|2026-08-15 09:00-11:00"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({keep}, t0, path_t1=p1, path_sup=ps)
        m.proc_b(
            set(),
            t0 + timedelta(hours=1),
            path_t1=p1,
            path_sup=ps,
            failed_scope={("v08", "2026-08")},
        )
        data = json.loads(p1.read_text(encoding="utf-8"))
        assert data[keep] == "2026-07-30T10:00:00"

    def test_promote_after_four_hours(self, tmp_path: Path) -> None:
        p1 = tmp_path / "t1.json"
        ps = tmp_path / "sup.json"
        key = "v01|2026-08-01 19:00-19:30"
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        m.proc_b({key}, t0, path_t1=p1, path_sup=ps)
        assert m.proc_promote({key}, t0 + timedelta(hours=3, minutes=59), path_t1=p1, path_sup=ps) == set()
        promoted = m.proc_promote({key}, t0 + timedelta(hours=4), path_t1=p1, path_sup=ps)
        assert promoted == {key}
        assert key not in json.loads(p1.read_text(encoding="utf-8"))
        assert key in json.loads(ps.read_text(encoding="utf-8"))

    def test_collect_records_fetch_b_failure(self) -> None:
        client = MagicMock()

        def fake_a(client, base, tenant, base_date, fid, sid):  # noqa: ANN001, ANN202
            return [
                {
                    "date": "2026-08-01",
                    "isPast": False,
                    "spaces": [{"spaceId": 181, "status": "available"}],
                }
            ]

        def fake_b(client, base, tenant, fid, sid, days):  # noqa: ANN001, ANN202
            raise TimeoutError("boom")

        import core.scanner as sc

        orig_a, orig_b = sc.fetch_a, sc.fetch_b
        sc.fetch_a = fake_a  # type: ignore[assignment]
        sc.fetch_b = fake_b  # type: ignore[assignment]
        try:
            keys, failed = sc.collect_one(
                client,
                "https://example.test",
                "t1",
                {"code": "v08", "fid": 26, "sid": 181},
                date(2026, 7, 30),
                date(2026, 8, 31),
            )
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert keys == set()
        assert ("v08", "2026-08-01") in failed


class TestDiffNotify:
    def test_new_key_triggers_send(self, tmp_path: Path) -> None:
        p2 = tmp_path / "t2.json"
        m.proc_f(["old"], path_t2=p2)
        added, removed, _old = m.proc_diff({"old", "new"}, path_t2=p2)
        assert added == {"new"}
        assert removed == set()
        assert m.should_send(added, removed, {}) is True

    def test_suppressed_reappear_no_send(self, tmp_path: Path) -> None:
        p2 = tmp_path / "t2.json"
        m.proc_f([], path_t2=p2)
        key = "v01|2026-08-01 19:00-19:30"
        added, removed, _ = m.proc_diff({key}, path_t2=p2)
        assert added == {key}
        assert m.should_send(added, removed, {key: "x"}) is False

    def test_suppressed_gone_no_send(self, tmp_path: Path) -> None:
        p2 = tmp_path / "t2.json"
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=p2)
        added, removed, _ = m.proc_diff(set(), path_t2=p2)
        assert removed == {key}
        assert m.should_send(added, removed, {key: "x"}) is False

    def test_failed_scope_not_removed(self, tmp_path: Path) -> None:
        p2 = tmp_path / "t2.json"
        keep = "v08|2026-08-01 09:00-11:00"
        m.proc_f([keep], path_t2=p2)
        added, removed, _ = m.proc_diff(
            set(),
            path_t2=p2,
            failed_scope={("v08", "2026-08-01")},
        )
        assert removed == set()
        assert m.should_send(added, removed, {}) is False


class TestHelper7:
    def _blocks3(self) -> dict:
        # len2 exclusive + two len1 halves; names unused by logic
        return {
            1: [1, 2],
            2: [1],
            3: [2],
            "1": [1, 2],
            "2": [1],
            "3": [2],
        }

    def test_single_area_ok(self) -> None:
        ab = {10: [1], "10": [1]}
        details = [{"areaId": 10, "status": "available"}]
        assert m.helper_7(details, ab) is True

    def test_single_area_ng(self) -> None:
        ab = {10: [1], "10": [1]}
        details = [{"areaId": 10, "status": "unavailable"}]
        assert m.helper_7(details, ab) is False

    def test_len2_ignored_half_ok(self) -> None:
        details = [
            {"areaId": 1, "status": "unavailable"},
            {"areaId": 2, "status": "available"},
            {"areaId": 3, "status": "unavailable"},
        ]
        assert m.helper_7(details, self._blocks3()) is True

    def test_len2_ok_halves_ng(self) -> None:
        details = [
            {"areaId": 1, "status": "available"},
            {"areaId": 2, "status": "unavailable"},
            {"areaId": 3, "status": "unavailable"},
        ]
        assert m.helper_7(details, self._blocks3()) is False

    def test_all_ng(self) -> None:
        details = [
            {"areaId": 1, "status": "unavailable"},
            {"areaId": 2, "status": "unavailable"},
            {"areaId": 3, "status": "unavailable"},
        ]
        assert m.helper_7(details, self._blocks3()) is False

    def test_name_agnostic_senyo_style(self) -> None:
        # same blocks layout; names would be 専用/共用A/共用B but are not consulted
        ab = self._blocks3()
        details = [
            {"areaId": 1, "areaName": "専用", "status": "available"},
            {"areaId": 2, "areaName": "共用A", "status": "unavailable"},
            {"areaId": 3, "areaName": "共用B", "status": "available"},
        ]
        assert m.helper_7(details, ab) is True

    def test_name_agnostic_hall_style(self) -> None:
        ab = self._blocks3()
        details = [
            {"areaId": 1, "areaName": "ホール１+２", "status": "available"},
            {"areaId": 2, "areaName": "ホール１", "status": "unavailable"},
            {"areaId": 3, "areaName": "ホール２", "status": "unavailable"},
        ]
        assert m.helper_7(details, ab) is False


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
            ab = {10: [1], "10": [1]}
            table = [
                {
                    "timeString": "18:00-18:30",
                    "details": [{"areaId": 10, "status": "available"}],
                },
                {
                    "timeString": "19:00-19:30",
                    "details": [{"areaId": 10, "status": "available"}],
                },
                {
                    "timeString": "09:00-09:30",
                    "details": [{"areaId": 10, "status": "unavailable"}],
                },
            ]
            return ab, table

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
            )[0]
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
            ab = {10: [1], "10": [1]}
            table = [
                {
                    "timeString": "09:00-09:30",
                    "details": [{"areaId": 10, "status": "available"}],
                },
                {
                    "timeString": "19:00-19:30",
                    "details": [{"areaId": 10, "status": "available"}],
                },
            ]
            return ab, table

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
            )[0]
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert keys == {
            "v03|2026-08-08 09:00-09:30",
            "v03|2026-08-08 19:00-19:30",
        }

    def test_collect_ignores_len2_only(self) -> None:
        client = MagicMock()

        def fake_a(client, base, tenant, base_date, fid, sid):  # noqa: ANN001, ANN202
            return [
                {
                    "date": "2026-08-05",
                    "isPast": False,
                    "spaces": [{"spaceId": 2, "status": "available"}],
                }
            ]

        def fake_b(client, base, tenant, fid, sid, days):  # noqa: ANN001, ANN202
            ab = {
                1: [1, 2],
                2: [1],
                3: [2],
                "1": [1, 2],
                "2": [1],
                "3": [2],
            }
            table = [
                {
                    "timeString": "19:00-19:30",
                    "details": [
                        {"areaId": 1, "status": "available"},
                        {"areaId": 2, "status": "unavailable"},
                        {"areaId": 3, "status": "unavailable"},
                    ],
                },
                {
                    "timeString": "19:30-20:00",
                    "details": [
                        {"areaId": 1, "status": "unavailable"},
                        {"areaId": 2, "status": "available"},
                        {"areaId": 3, "status": "unavailable"},
                    ],
                },
            ]
            return ab, table

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
            )[0]
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert keys == {"v01|2026-08-05 19:30-20:00"}

    def test_collect_saturday_morning_kept(self) -> None:
        """2026-08-01 Sat + single area blocks=[1] + 09:00-11:00 available."""
        client = MagicMock()

        def fake_a(client, base, tenant, base_date, fid, sid):  # noqa: ANN001, ANN202
            return [
                {
                    "date": "2026-08-01",
                    "isPast": False,
                    "spaces": [{"spaceId": 181, "status": "available"}],
                }
            ]

        def fake_b(client, base, tenant, fid, sid, days):  # noqa: ANN001, ANN202
            ab = {310: [1], "310": [1]}
            table = [
                {
                    "timeString": "09:00-11:00",
                    "details": [{"areaId": 310, "status": "available"}],
                },
                {
                    "timeString": "11:00-13:00",
                    "details": [{"areaId": 310, "status": "unavailable"}],
                },
            ]
            return ab, table

        import core.scanner as sc

        orig_a, orig_b = sc.fetch_a, sc.fetch_b
        sc.fetch_a = fake_a  # type: ignore[assignment]
        sc.fetch_b = fake_b  # type: ignore[assignment]
        try:
            keys = sc.collect_one(
                client,
                "https://example.test",
                "t1",
                {"code": "v08", "fid": 26, "sid": 181},
                date(2026, 7, 30),
                date(2026, 8, 31),
            )[0]
        finally:
            sc.fetch_a, sc.fetch_b = orig_a, orig_b

        assert "v08|2026-08-01 09:00-11:00" in keys
        assert "v08|2026-08-01 11:00-13:00" not in keys


class TestRunTask:
    def test_new_key_mails_immediately(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        sent: list[list[str]] = []

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            sent.append(list(lines))
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")

        key = "v01|2026-08-01 19:00-19:30"
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: ({key}, set()))
        t0 = datetime(2026, 7, 30, 10, 0, 0)
        r = m.run_task(
            t0,
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
        )
        assert r["mailed"] is True
        assert r["changed"] is True
        assert sent == [[key]]
        assert json.loads(p2.read_text(encoding="utf-8")) == [key]

    def test_suppressed_reappear_no_mail_but_in_body_when_other_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        old = "v01|2026-08-01 19:00-19:30"
        fresh = "v02|2026-08-02 19:00-19:30"
        # previous snapshot empty; suppressed knows old
        m._save_map(ps, {old: "2026-07-01T00:00:00"})
        m.proc_f([], path_t2=p2)

        sent: list[list[str]] = []

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            sent.append(list(lines))
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: ({old, fresh}, set()))

        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
        )
        # fresh triggers send; old is suppressed so alone wouldn't, but body has both
        assert r["mailed"] is True
        assert set(sent[0]) == {old, fresh}
        assert old in r["added"]
        assert m.should_send({old}, set(), {old: "x"}) is False

    def test_suppressed_gone_alone_no_mail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        key = "v01|2026-08-01 19:00-19:30"
        m._save_map(ps, {key: "2026-07-01T00:00:00"})
        m.proc_f([key], path_t2=p2)
        sent: list[list[str]] = []

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            sent.append(list(lines))
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: (set(), set()))

        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
        )
        assert r["mailed"] is False
        assert sent == []

    def test_unchanged_no_mail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=p2)
        sent: list[list[str]] = []
        monkeypatch.setattr(
            "core.notifier.send_msg",
            lambda *a, **k: sent.append(a[0]) or True,
        )
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: ({key}, set()))
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
        )
        assert r["changed"] is False
        assert r["mailed"] is False
        assert sent == []

    def test_force_mail_bypasses_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        key = "v01|2026-08-01 19:00-19:30"
        m._save_map(ps, {key: "2026-07-01T00:00:00"})
        m.proc_f([key], path_t2=p2)
        sent: list[list[str]] = []

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            sent.append(list(lines))
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: ({key}, set()))
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
            force_mail=True,
        )
        assert r["changed"] is False
        assert r["mailed"] is True
        assert sent == [[key]]


class TestDailyFirstMail:
    def _env(self, monkeypatch: pytest.MonkeyPatch, keys: set[str]) -> None:
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(m, "collect_all", lambda *a, **k: (keys, set()))

    def test_missing_daily_sent_forces_mail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=tmp_path / "t2.json")
        sent: list[dict] = []

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            sent.append({"lines": list(lines), "kwargs": dict(kwargs)})
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        self._env(monkeypatch, {key})
        pdaily = tmp_path / "daily_sent.json"
        assert not pdaily.exists()
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=pdaily,
            send=True,
        )
        assert r["changed"] is False
        assert r["mailed"] is True
        assert sent and sent[0]["lines"] == [key]
        # Full Calendar path: send_msg must not disable load_cal
        assert sent[0]["kwargs"].get("load_cal", True) is True
        assert json.loads(pdaily.read_text(encoding="utf-8")) == {
            "last_sent_date": "2026-07-30"
        }

    def test_today_already_sent_no_force(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=tmp_path / "t2.json")
        sent: list[list[str]] = []
        monkeypatch.setattr(
            "core.notifier.send_msg",
            lambda *a, **k: sent.append(a[0]) or True,
        )
        self._env(monkeypatch, {key})
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=_mark_daily_sent(tmp_path, "2026-07-30"),
            send=True,
        )
        assert r["changed"] is False
        assert r["mailed"] is False
        assert sent == []

    def test_yesterday_forces_mail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=tmp_path / "t2.json")
        sent: list[list[str]] = []
        monkeypatch.setattr(
            "core.notifier.send_msg",
            lambda *a, **k: sent.append(list(a[0])) or True,
        )
        self._env(monkeypatch, {key})
        pdaily = _mark_daily_sent(tmp_path, "2026-07-29")
        r = m.run_task(
            datetime(2026, 7, 30, 8, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=pdaily,
            send=True,
        )
        assert r["mailed"] is True
        assert sent == [[key]]
        assert json.loads(pdaily.read_text(encoding="utf-8"))["last_sent_date"] == (
            "2026-07-30"
        )

    def test_send_failure_does_not_update_daily_sent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=tmp_path / "t2.json")
        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: False)
        self._env(monkeypatch, {key})
        pdaily = tmp_path / "daily_sent.json"
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=pdaily,
            send=True,
        )
        assert r["mailed"] is False
        assert not pdaily.exists()

        # Next run still forces
        sent: list[list[str]] = []
        monkeypatch.setattr(
            "core.notifier.send_msg",
            lambda *a, **k: sent.append(list(a[0])) or True,
        )
        r2 = m.run_task(
            datetime(2026, 7, 30, 13, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=pdaily,
            send=True,
        )
        assert r2["mailed"] is True
        assert sent == [[key]]
        assert json.loads(pdaily.read_text(encoding="utf-8"))["last_sent_date"] == (
            "2026-07-30"
        )

    def test_force_only_does_not_mutate_t1_sup_t2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = "v01|2026-08-01 19:00-19:30"
        p1 = tmp_path / "t1.json"
        p2 = tmp_path / "t2.json"
        ps = tmp_path / "sup.json"
        m._save_map(ps, {key: "2026-07-01T00:00:00"})
        m.proc_f([key], path_t2=p2)
        m._save_map(p1, {})
        t2_before = p2.read_text(encoding="utf-8")
        sup_before = ps.read_text(encoding="utf-8")

        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: True)
        self._env(monkeypatch, {key})
        r = m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=p1,
            path_t2=p2,
            path_sup=ps,
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=tmp_path / "daily_sent.json",
            send=True,
        )
        assert r["changed"] is False
        assert r["mailed"] is True
        # force-only must not rewrite t2 snapshot
        assert p2.read_text(encoding="utf-8") == t2_before
        # suppressed entry stays (no promote churn); key skipped in t1
        assert ps.read_text(encoding="utf-8") == sup_before
        assert key not in json.loads(p1.read_text(encoding="utf-8"))

    def test_force_mail_includes_calendar_sections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Daily force uses send_msg default load_cal=True (full body)."""
        from datetime import date as date_cls

        key = "v01|2026-08-01 19:00-19:30"
        m.proc_f([key], path_t2=tmp_path / "t2.json")
        captured: dict[str, object] = {}

        def fake_send(lines, flag_x, when=None, **kwargs):  # noqa: ANN001, ANN002, ANN202
            captured["lines"] = list(lines)
            captured["kwargs"] = dict(kwargs)
            captured["when"] = when
            return True

        monkeypatch.setattr("core.notifier.send_msg", fake_send)
        # Also verify build path would include 直近予定 when load_cal works
        monkeypatch.setattr(
            "core.calendar_read.get_recent",
            lambda days: {days[0]: ["・予定A"], days[1]: []},
        )
        monkeypatch.setattr("core.calendar_read.get_matched", lambda ds: {})
        monkeypatch.setattr("core.calendar_read.get_counts", lambda months: {})
        from core.notifier import build_mail_body

        body = build_mail_body(
            [key],
            names={"v01": {"name": "会場A", "letter": "A"}},
            template={"subject": "S", "header": "H", "footer": ""},
            when=date_cls(2026, 7, 30),
            load_cal=True,
            venue_list="",
        )
        assert "直近予定" in body

        self._env(monkeypatch, {key})
        m.run_task(
            datetime(2026, 7, 30, 12, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=tmp_path / "daily_sent.json",
            send=True,
        )
        assert captured.get("lines") == [key]
        assert captured["kwargs"].get("load_cal", True) is True
