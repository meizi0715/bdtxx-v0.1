"""Tests for lottery scan (month+2 waiting counts)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core import lottery as lot
from core.lottery import (
    LotteryEntry,
    entries_to_snapshot,
    load_lottery_prev,
    lottery_changed,
    save_lottery_prev,
)
from core.notifier import (
    build_lottery_body,
    build_lottery_subject,
    lottery_start_hh,
)

_TZ = ZoneInfo("Asia/Tokyo")


class TestTargetMonth:
    def test_same_year(self) -> None:
        assert lot.target_month(date(2026, 8, 5)) == (2026, 10)

    def test_cross_year_nov(self) -> None:
        assert lot.target_month(date(2026, 11, 1)) == (2027, 1)

    def test_cross_year_dec(self) -> None:
        assert lot.target_month(date(2026, 12, 15)) == (2027, 2)

    def test_datetime_tokyo(self) -> None:
        when = datetime(2026, 12, 31, 23, 0, tzinfo=_TZ)
        assert lot.target_month(when) == (2027, 2)


class TestCalendarDays:
    def test_is_current_month_only_ignores_status(self) -> None:
        cal = [
            {
                "date": "2026-09-30",
                "isCurrentMonth": False,
                "spaces": [{"spaceId": 1, "status": "available"}],
            },
            {
                "date": "2026-10-01",
                "isCurrentMonth": True,
                "spaces": [{"spaceId": 1, "status": "available"}],
            },
            {
                "date": "2026-10-02",
                "isCurrentMonth": True,
                "spaces": [{"spaceId": 1, "status": "unavailable"}],
            },
            {
                "date": "2026-11-01",
                "isCurrentMonth": False,
                "spaces": [{"spaceId": 1, "status": "available"}],
            },
        ]
        assert lot.days_from_calendar(cal) == [
            date(2026, 10, 1),
            date(2026, 10, 2),
        ]


class TestFaceFilterAndExtract:
    def _gym_areas(self) -> list[dict]:
        return [
            {"areaId": 1, "areaName": "専用", "blocks": [1, 2]},
            {"areaId": 2, "areaName": "共用A", "blocks": [1]},
            {"areaId": 3, "areaName": "共用B", "blocks": [2]},
        ]

    def test_excludes_exclusive_face_letters_by_area_id(self) -> None:
        faces = lot.shared_face_map(self._gym_areas(), "gym")
        assert 1 not in faces and "1" not in faces
        assert faces[2] == "A"
        assert faces[3] == "B"

    def test_hall_single_shared_is_a(self) -> None:
        areas = [
            {"areaId": 10, "areaName": "ホール１+２", "blocks": [1, 2]},
            {"areaId": 11, "areaName": "ホール１", "blocks": [1]},
        ]
        faces = lot.shared_face_map(areas, "hall")
        assert 10 not in faces
        assert faces[11] == "A"

    def test_three_faces_same_name_sorted_by_area_id(self) -> None:
        """芝スポーツセンター-style: three faces may share the same areaName."""
        areas = [
            {"areaId": 30, "areaName": "バドミントン", "blocks": [1, 2, 3]},
            {"areaId": 33, "areaName": "バドミントン", "blocks": [3]},
            {"areaId": 31, "areaName": "バドミントン", "blocks": [1]},
            {"areaId": 32, "areaName": "バドミントン", "blocks": [2]},
        ]
        faces = lot.shared_face_map(areas, "gym")
        assert 30 not in faces
        assert faces[31] == "A"
        assert faces[32] == "B"
        assert faces[33] == "C"

    def test_all_unavailable_day_excluded(self) -> None:
        areas = self._gym_areas()
        table = [
            {
                "timeString": f"{h:02d}:00-{h + 2:02d}:00",
                "details": [
                    {
                        "areaId": 2,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                    {
                        "areaId": 3,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                ],
            }
            for h in (9, 11, 13, 15, 17, 19)
        ]
        # Saturday so time filter keeps all six slots
        rows = lot.extract_entries(
            "v03", date(2026, 10, 4), areas, table, "gym"
        )
        assert rows == []

    def test_partial_available_keeps_only_available(self) -> None:
        areas = self._gym_areas()
        table = [
            {
                "timeString": "09:00-11:00",
                "details": [
                    {
                        "areaId": 2,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                    {
                        "areaId": 3,
                        "status": "available",
                        "lotteryWaitingCount": 2,
                    },
                ],
            },
            {
                "timeString": "11:00-13:00",
                "details": [
                    {
                        "areaId": 2,
                        "status": "available",
                        "lotteryWaitingCount": 1,
                    },
                    {
                        "areaId": 3,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                ],
            },
            {
                "timeString": "13:00-15:00",
                "details": [
                    {
                        "areaId": 2,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                    {
                        "areaId": 3,
                        "status": "unavailable",
                        "lotteryWaitingCount": 0,
                    },
                ],
            },
        ]
        rows = lot.extract_entries(
            "v03", date(2026, 10, 4), areas, table, "gym"
        )
        by_key = {(r.time_string, r.face, r.count) for r in rows}
        assert by_key == {
            ("09:00-11:00", "B", 2),
            ("11:00-13:00", "A", 1),
        }

    def test_all_available_day_kept_complete(self) -> None:
        areas = self._gym_areas()
        table = [
            {
                "timeString": "17:00-19:00",
                "details": [
                    {
                        "areaId": 1,
                        "status": "available",
                        "lotteryWaitingCount": 99,
                    },
                    {
                        "areaId": 2,
                        "status": "available",
                        "lotteryWaitingCount": 0,
                    },
                    {
                        "areaId": 3,
                        "status": "available",
                        "lotteryWaitingCount": 3,
                    },
                ],
            }
        ]
        rows = lot.extract_entries(
            "v01", date(2026, 10, 5), areas, table, "gym"
        )
        by_face = {(r.face, r.count) for r in rows}
        assert by_face == {("A", 0), ("B", 3)}
        assert all(r.count != 99 for r in rows)


class TestSlotDayFilter:
    def _full_day_table(self) -> list[dict]:
        hours = [9, 11, 13, 15, 17, 19]
        return [
            {
                "timeString": f"{h:02d}:00-{h + 2:02d}:00",
                "details": [
                    {
                        "areaId": 11,
                        "status": "available",
                        "lotteryWaitingCount": 0,
                    }
                ],
            }
            for h in hours
        ]

    def _areas(self) -> list[dict]:
        return [{"areaId": 11, "blocks": [1]}]

    def test_weekday_only_17_and_19(self) -> None:
        # 2026-10-05 Monday (not a JP holiday)
        day = date(2026, 10, 5)
        assert day.weekday() < 5
        rows = lot.extract_entries(
            "v08", day, self._areas(), self._full_day_table(), "hall"
        )
        hours = sorted({lot._start_hour(r.time_string) for r in rows})
        assert hours == [17, 19]
        assert len(rows) == 2

    def test_weekend_keeps_all_six(self) -> None:
        # 2026-10-10 Saturday
        day = date(2026, 10, 10)
        assert day.weekday() >= 5
        rows = lot.extract_entries(
            "v08", day, self._areas(), self._full_day_table(), "hall"
        )
        hours = sorted({lot._start_hour(r.time_string) for r in rows})
        assert hours == [9, 11, 13, 15, 17, 19]
        assert len(rows) == 6

    def test_holiday_weekday_keeps_all_six(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force a Monday to be treated as holiday via jpholiday path in helper_3
        day = date(2026, 10, 5)  # Monday
        assert day.weekday() < 5
        monkeypatch.setattr(
            "core.scanner.jpholiday.is_holiday", lambda d: d == day
        )
        rows = lot.extract_entries(
            "v08", day, self._areas(), self._full_day_table(), "hall"
        )
        hours = sorted({lot._start_hour(r.time_string) for r in rows})
        assert hours == [9, 11, 13, 15, 17, 19]
        assert len(rows) == 6

    def test_keep_slot_helper(self) -> None:
        mon = date(2026, 10, 5)
        sat = date(2026, 10, 10)
        assert lot.keep_slot(mon, "09:00-11:00") is False
        assert lot.keep_slot(mon, "17:00-19:00") is True
        assert lot.keep_slot(mon, "19:00-21:00") is True
        assert lot.keep_slot(sat, "09:00-11:00") is True


class TestMailFormat:
    def test_start_hour_zero_padded_no_end(self) -> None:
        assert lottery_start_hh("9:00-11:00") == "09"
        assert lottery_start_hh("09:00-11:00") == "09"
        assert lottery_start_hh("19:00～21:00") == "19"

    def test_single_face_merges_same_day(self) -> None:
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "11:00-13:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "13:00-15:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "15:00-17:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "17:00-19:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "19:00-21:00", "A", 0),
        ]
        names = {"v08": {"name": "青木公民館", "letter": "H"}}
        tmpl = {"subject": "S", "header": ""}
        body = build_lottery_body(
            entries, year=2026, month=10, names=names, template=tmpl
        )
        assert (
            "・10月10日(土) 09(0)、11(0)、13(0)、15(0)、17(0)、19(0)" in body
        )
        assert "----------抽選可能----------" in body

    def test_two_faces_merges_with_letters(self) -> None:
        entries = [
            LotteryEntry("v01", date(2026, 10, 10), "09:00-11:00", "A", 0),
            LotteryEntry("v01", date(2026, 10, 10), "09:00-11:00", "B", 0),
            LotteryEntry("v01", date(2026, 10, 10), "11:00-13:00", "A", 1),
            LotteryEntry("v01", date(2026, 10, 10), "11:00-13:00", "B", 0),
        ]
        names = {"v01": {"name": "体育武道センター", "letter": "A"}}
        tmpl = {"subject": "S", "header": ""}
        body = build_lottery_body(
            entries, year=2026, month=10, names=names, template=tmpl
        )
        assert "・10月10日(土) 09(A0/B0)、11(A1/B0)" in body

    def test_three_faces_same_name_venue(self) -> None:
        entries = [
            LotteryEntry("v03", date(2026, 10, 10), "09:00-11:00", "A", 0),
            LotteryEntry("v03", date(2026, 10, 10), "09:00-11:00", "B", 1),
            LotteryEntry("v03", date(2026, 10, 10), "09:00-11:00", "C", 0),
            LotteryEntry("v03", date(2026, 10, 10), "11:00-13:00", "A", 0),
            LotteryEntry("v03", date(2026, 10, 10), "11:00-13:00", "B", 0),
            LotteryEntry("v03", date(2026, 10, 10), "11:00-13:00", "C", 2),
        ]
        names = {"v03": {"name": "芝スポーツセンター", "letter": "C"}}
        tmpl = {
            "subject": "{yyyy}年{mm}月バトミントン抽選数",
            "header": (
                "ご担当者様\n\nお疲れ様です。\n"
                "掲題の件につきまして、次回に川口市各施設バトミントン抽選可能時間帯を送ります。\n"
                "※対象時間帯：平日17:00~21:00、祝日/週末終日※"
            ),
        }
        body = build_lottery_body(
            entries, year=2026, month=10, names=names, template=tmpl
        )
        assert (
            build_lottery_subject(2026, 10, template=tmpl)
            == "2026年10月バトミントン抽選数"
        )
        assert body.startswith("ご担当者様\n\nお疲れ様です。")
        assert "----------抽選可能----------" in body
        assert "【芝スポーツセンター】" in body
        assert "・10月10日(土) 09(A0/B1/C0)、11(A0/B0/C2)" in body
        assert (
            "※対象時間帯：平日17:00~21:00、祝日/週末終日※\n\n"
            "----------抽選可能----------\n"
        ) in body

    def test_empty_venue_group_skipped(self) -> None:
        names = {
            "v01": {"name": "体育武道センター", "letter": "A"},
            "v08": {"name": "青木公民館", "letter": "H"},
        }
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
        ]
        tmpl = {"subject": "S", "header": ""}
        body = build_lottery_body(
            entries, year=2026, month=10, names=names, template=tmpl
        )
        assert "【青木公民館】" in body
        assert "【体育武道センター】" not in body

    def test_blank_line_between_venues(self) -> None:
        entries = [
            LotteryEntry("v01", date(2026, 10, 5), "17:00-19:00", "A", 0),
            LotteryEntry("v01", date(2026, 10, 5), "17:00-19:00", "B", 0),
            LotteryEntry("v08", date(2026, 10, 5), "17:00-19:00", "A", 1),
        ]
        names = {
            "v01": {"name": "体育武道センター", "letter": "A"},
            "v08": {"name": "青木公民館", "letter": "H"},
        }
        body = build_lottery_body(
            entries,
            year=2026,
            month=10,
            names=names,
            template={"subject": "S", "header": ""},
        )
        assert "\n\n【青木公民館】" in body

    def test_missing_template_file_uses_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / "nope.json"
        with caplog.at_level(logging.WARNING):
            subj = build_lottery_subject(2026, 10, template_path=missing)
            body = build_lottery_body(
                [],
                year=2026,
                month=10,
                names={"v01": {"name": "X", "letter": "A"}},
                template_path=missing,
            )
        assert subj == "Lottery (2026-10)"
        assert body.startswith("----------抽選可能----------")
        assert "mail content missing" in caplog.text
        assert "lottery defaults" in caplog.text

    def test_missing_lottery_key_uses_defaults(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "mail_content.json"
        p.write_text(
            json.dumps(
                {
                    "scan": {
                        "subject": "S({mmdd})",
                        "header": "SH",
                        "footer": "",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING):
            subj = build_lottery_subject(2026, 10, template_path=p)
            body = build_lottery_body(
                [],
                year=2026,
                month=10,
                names={},
                template_path=p,
            )
        assert subj == "Lottery (2026-10)"
        assert "missing 'lottery' key" in caplog.text
        assert not body.startswith("SH")

    def test_reads_lottery_section_from_merged_file_not_hardcoded(
        self, tmp_path: Path
    ) -> None:
        """Distinct file copy must appear — proves file read, not leftover hardcode."""
        marker_subj = "UNIQUE-LOTTERY-SUBJ-{yyyy}-{mm}-XYZ"
        marker_hdr = "UNIQUE-LOTTERY-HEADER-MARKER-ABC123"
        p = tmp_path / "mail_content.json"
        p.write_text(
            json.dumps(
                {
                    "scan": {
                        "subject": "scan({mmdd})",
                        "header": "scan-header",
                        "footer": "",
                    },
                    "lottery": {
                        "subject": marker_subj,
                        "header": marker_hdr,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
        ]
        names = {"v08": {"name": "青木公民館", "letter": "H"}}
        subj = build_lottery_subject(2026, 10, template_path=p)
        body = build_lottery_body(
            entries, year=2026, month=10, names=names, template_path=p
        )
        assert subj == "UNIQUE-LOTTERY-SUBJ-2026-10-XYZ"
        assert body.startswith(marker_hdr + "\n\n----------抽選可能----------")
        assert "バトミントン抽選数" not in subj
        assert "ご担当者様" not in body
        assert "scan-header" not in body


class TestLotteryPrevSnapshot:
    def test_first_run_empty_prev_is_changed(self, tmp_path: Path) -> None:
        p = tmp_path / "lottery_prev.json"
        prev = load_lottery_prev(p)
        assert prev == {}
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
        ]
        cur = entries_to_snapshot(entries)
        assert lottery_changed(cur, prev) is True

    def test_identical_snapshot_not_changed(self, tmp_path: Path) -> None:
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "11:00-13:00", "A", 1),
        ]
        cur = entries_to_snapshot(entries)
        p = tmp_path / "lottery_prev.json"
        save_lottery_prev(cur, p)
        assert load_lottery_prev(p) == cur
        assert lottery_changed(cur, load_lottery_prev(p)) is False

    def test_partial_count_change_is_changed(self) -> None:
        prev = {"v08|2026-10-10|09|A": 0, "v08|2026-10-10|11|A": 0}
        cur = {"v08|2026-10-10|09|A": 0, "v08|2026-10-10|11|A": 2}
        assert lottery_changed(cur, prev) is True

    def test_save_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "lottery_prev.json"
        snap = {"v03|2026-10-01|17|A": 1, "v03|2026-10-01|17|B": 0}
        save_lottery_prev(snap, p)
        assert load_lottery_prev(p) == snap

    def test_star_only_on_changed_day_lines(self) -> None:
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 10), "11:00-13:00", "A", 1),
            LotteryEntry("v08", date(2026, 10, 11), "09:00-11:00", "A", 0),
            LotteryEntry("v08", date(2026, 10, 11), "11:00-13:00", "A", 0),
        ]
        prev = {
            "v08|2026-10-10|09|A": 0,
            "v08|2026-10-10|11|A": 0,  # was 0, now 1 → changed
            "v08|2026-10-11|09|A": 0,
            "v08|2026-10-11|11|A": 0,  # unchanged
        }
        names = {"v08": {"name": "青木公民館", "letter": "H"}}
        body = build_lottery_body(
            entries,
            year=2026,
            month=10,
            names=names,
            template={"subject": "S", "header": ""},
            previous=prev,
        )
        assert "・10月10日(土) 09(0)、11(1)(★)" in body
        assert "・10月11日(日) 09(0)、11(0)\n" in body or (
            "・10月11日(日) 09(0)、11(0)" in body
            and "・10月11日(日) 09(0)、11(0)(★)" not in body
        )

    def test_first_run_all_days_starred(self) -> None:
        entries = [
            LotteryEntry("v08", date(2026, 10, 10), "09:00-11:00", "A", 0),
        ]
        body = build_lottery_body(
            entries,
            year=2026,
            month=10,
            names={"v08": {"name": "青木公民館", "letter": "H"}},
            template={"subject": "S", "header": ""},
            previous={},
        )
        assert "(★)" in body
        assert "・10月10日(土) 09(0)(★)" in body


class TestCollectIgnoresCalendarStatus:
    def test_still_scans_day_marked_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venues = {"v08": {"fid": 26, "sid": 181, "type": "hall"}}

        async def fake_cal(client, base, tenant, base_date, fid, sid):  # noqa: ANN001
            assert base_date == date(2026, 10, 1)
            return [date(2026, 10, 3)]

        async def fake_space(client, base, tenant, fid, sid, day):  # noqa: ANN001
            assert day == date(2026, 10, 3)
            areas = [
                {"areaId": 100, "blocks": [1, 2]},
                {"areaId": 101, "blocks": [1]},
            ]
            table = [
                {
                    "timeString": "13:00-15:00",
                    "details": [
                        {
                            "areaId": 100,
                            "status": "available",
                            "lotteryWaitingCount": 5,
                        },
                        {
                            "areaId": 101,
                            "status": "available",
                            "lotteryWaitingCount": 0,
                        },
                    ],
                }
            ]
            return areas, table

        monkeypatch.setattr(lot, "fetch_calendar_days", fake_cal)
        monkeypatch.setattr(lot, "fetch_space_day", fake_space)
        monkeypatch.setattr(lot, "_PACING_S", 0.0)

        rows = asyncio.run(
            lot.collect_all_async(
                "https://example.invalid",
                "kawaguchi-city",
                venues,
                2026,
                10,
                concurrency=2,
            )
        )
        assert len(rows) == 1
        assert rows[0].face == "A"
        assert rows[0].count == 0
        assert rows[0].day == date(2026, 10, 3)
