"""Tests for compact booking_tasks preprocessing."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.booking import (
    _format_step_when,
    _step_log,
    _team_initial,
    display_time_range,
    expand_booking_tasks,
    group_hours_for_venue,
    load_tasks,
    next_month_date,
    parse_hours,
    resolve_group_ref,
    time_slot_to_jp,
    warn_gym_adjacent_slots,
)

TZ = ZoneInfo("Asia/Tokyo")

_VENUES = {
    "v01": {"fid": 1, "sid": 1, "type": "gym"},
    "v02": {"fid": 2, "sid": 2, "type": "gym"},
    "v04": {"fid": 4, "sid": 4, "type": "hall"},
    "v08": {"fid": 8, "sid": 8, "type": "hall"},
}

_GROUPS = {
    "GROUP1": {"id": "gid1", "name": "チーム1", "password": "pw1"},
}


@pytest.fixture()
def groups_file(tmp_path: Path) -> Path:
    p = tmp_path / "groups.json"
    p.write_text(json.dumps(_GROUPS, ensure_ascii=False), encoding="utf-8")
    return p


class TestNextMonthDate:
    def test_same_year(self) -> None:
        now = datetime(2026, 7, 31, 12, 0, tzinfo=TZ)
        assert next_month_date(9, now=now) == "2026-08-09"

    def test_cross_year_december(self) -> None:
        now = datetime(2026, 12, 15, 8, 0, tzinfo=TZ)
        assert next_month_date(3, now=now) == "2027-01-03"


class TestParseHours:
    def test_single(self) -> None:
        assert parse_hours("9") == ["09:00-11:00"]

    def test_multiple(self) -> None:
        assert parse_hours("9,13") == ["09:00-11:00", "13:00-15:00"]

    def test_invalid_skipped_with_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        slots = parse_hours("9,10,13,abc")
        assert slots == ["09:00-11:00", "13:00-15:00"]
        err = capsys.readouterr().out
        assert "10" in err
        assert "abc" in err


class TestGroupHours:
    def test_gym_adjacent_never_merge(self) -> None:
        assert group_hours_for_venue([17, 19], "gym") == [[17], [19]]

    def test_hall_adjacent_pair_merges(self) -> None:
        assert group_hours_for_venue([17, 19], "hall") == [[17, 19]]

    def test_hall_three_contiguous_chunks(self) -> None:
        assert group_hours_for_venue([13, 15, 17], "hall") == [[13, 15], [17]]

    def test_hall_discontiguous_separate(self) -> None:
        assert group_hours_for_venue([9, 13], "hall") == [[9], [13]]


class TestDisplayTimeRange:
    def test_single(self) -> None:
        assert display_time_range(["17:00-19:00"]) == "17:00-19:00"

    def test_merged_pair(self) -> None:
        assert (
            display_time_range(["17:00-19:00", "19:00-21:00"]) == "17:00-21:00"
        )


class TestResolveGroupRef:
    def test_ok(self) -> None:
        login_id, name, password = resolve_group_ref("GROUP1", groups=_GROUPS)
        assert login_id == "gid1"
        assert name == "チーム1"
        assert password == "pw1"

    def test_missing_ref(self) -> None:
        with pytest.raises(ValueError, match=r"not found in local/groups.json"):
            resolve_group_ref("GROUP99", groups=_GROUPS)

    def test_missing_id(self) -> None:
        with pytest.raises(ValueError, match=r"missing field\(s\).*id"):
            resolve_group_ref(
                "GROUP1",
                groups={"GROUP1": {"id": "", "name": "n", "password": "p"}},
            )

    def test_missing_password(self) -> None:
        with pytest.raises(ValueError, match=r"missing field\(s\).*password"):
            resolve_group_ref(
                "GROUP1",
                groups={"GROUP1": {"id": "i", "name": "n", "password": ""}},
            )

    def test_missing_name(self) -> None:
        with pytest.raises(ValueError, match=r"missing field\(s\).*name"):
            resolve_group_ref(
                "GROUP1",
                groups={"GROUP1": {"id": "i", "name": "", "password": "p"}},
            )


class TestExpandBookingTasks:
    def test_gym_adjacent_splits(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=TZ)
        out = expand_booking_tasks(
            [
                {
                    "venue_code": "v02",
                    "day": 9,
                    "hours": "17,19",
                    "group_ref": "GROUP1",
                }
            ],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert [t["time_slots"] for t in out] == [
            ["17:00-19:00"],
            ["19:00-21:00"],
        ]
        assert all(t["password"] == "pw1" for t in out)
        assert all(t["group_name"] == "チーム1" for t in out)

    def test_hall_adjacent_merges(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=TZ)
        out = expand_booking_tasks(
            [
                {
                    "venue_code": "v08",
                    "day": 9,
                    "hours": "17,19",
                    "group_ref": "GROUP1",
                }
            ],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert len(out) == 1
        assert out[0]["time_slots"] == ["17:00-19:00", "19:00-21:00"]
        assert display_time_range(out[0]["time_slots"]) == "17:00-21:00"

    def test_hall_three_contiguous(self) -> None:
        now = datetime(2026, 7, 31, tzinfo=TZ)
        out = expand_booking_tasks(
            [
                {
                    "venue_code": "v04",
                    "day": 15,
                    "hours": "13,15,17",
                    "group_ref": "GROUP1",
                }
            ],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert [t["time_slots"] for t in out] == [
            ["13:00-15:00", "15:00-17:00"],
            ["17:00-19:00"],
        ]

    def test_hall_discontiguous(self) -> None:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=TZ)
        out = expand_booking_tasks(
            [
                {
                    "venue_code": "v08",
                    "day": 9,
                    "hours": "9,13",
                    "group_ref": "GROUP1",
                }
            ],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert out == [
            {
                "venue_code": "v08",
                "date": "2026-08-09",
                "time_slots": ["09:00-11:00"],
                "group_ref": "GROUP1",
                "group_id": "gid1",
                "group_name": "チーム1",
                "password": "pw1",
            },
            {
                "venue_code": "v08",
                "date": "2026-08-09",
                "time_slots": ["13:00-15:00"],
                "group_ref": "GROUP1",
                "group_id": "gid1",
                "group_name": "チーム1",
                "password": "pw1",
            },
        ]

    def test_invalid_hour_does_not_abort(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime(2026, 11, 1, tzinfo=TZ)
        out = expand_booking_tasks(
            [{"venue_code": "v01", "day": 2, "hours": "9,99", "group_ref": "GROUP1"}],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        assert len(out) == 1
        assert out[0]["time_slots"] == ["09:00-11:00"]
        assert out[0]["date"] == "2026-12-02"
        assert "99" in capsys.readouterr().out

    def test_missing_env_raises(self) -> None:
        with pytest.raises(ValueError, match=r"group_ref 'GROUP9'"):
            expand_booking_tasks(
                [{"venue_code": "v08", "day": 1, "hours": "9", "group_ref": "GROUP9"}],
                now=datetime(2026, 7, 1, tzinfo=TZ),
                venues=_VENUES,
                groups=_GROUPS,
            )

    def test_gym_adjacent_warn_across_tasks(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime(2026, 7, 31, tzinfo=TZ)
        expand_booking_tasks(
            [
                {
                    "venue_code": "v02",
                    "day": 28,
                    "hours": "17",
                    "group_ref": "GROUP1",
                },
                {
                    "venue_code": "v02",
                    "day": 28,
                    "hours": "19",
                    "group_ref": "GROUP1",
                },
            ],
            now=now,
            venues=_VENUES,
            groups=_GROUPS,
        )
        out = capsys.readouterr().out
        assert "v02" in out
        assert "2026-08-28" in out
        assert "17:00-19:00" in out and "19:00-21:00" in out


class TestWarnGymAdjacent:
    def test_no_warn_for_hall(self, capsys: pytest.CaptureFixture[str]) -> None:
        warn_gym_adjacent_slots(
            [
                {
                    "venue_code": "v08",
                    "date": "2026-08-28",
                    "time_slots": ["17:00-19:00", "19:00-21:00"],
                }
            ],
            venues=_VENUES,
        )
        assert "警告" not in capsys.readouterr().out


class TestLoadTasks:
    def test_loads_and_expands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        groups_path = tmp_path / "groups.json"
        groups_path.write_text(
            json.dumps(
                {"GROUP1": {"id": "u1", "name": "団1", "password": "p1"}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("core.booking.PATH_GROUPS", groups_path)
        p = tmp_path / "booking_tasks.json"
        p.write_text(
            json.dumps(
                [
                    {
                        "venue_code": "v08",
                        "day": 9,
                        "hours": "19",
                        "group_ref": "GROUP1",
                    }
                ]
            ),
            encoding="utf-8",
        )
        now = datetime(2026, 12, 20, tzinfo=TZ)
        tasks = load_tasks(p, now=now)
        assert len(tasks) == 1
        assert tasks[0]["date"] == "2027-01-09"
        assert tasks[0]["time_slots"] == ["19:00-21:00"]
        assert tasks[0]["group_id"] == "u1"
        assert tasks[0]["group_name"] == "団1"
        assert tasks[0]["password"] == "p1"

    def test_utf8_sig_bom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        groups_path = tmp_path / "groups.json"
        groups_path.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(
                {"GROUP1": {"id": "u1", "name": "団1", "password": "p1"}},
                ensure_ascii=False,
            ).encode("utf-8")
        )
        monkeypatch.setattr("core.booking.PATH_GROUPS", groups_path)
        p = tmp_path / "booking_tasks.json"
        p.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps(
                [
                    {
                        "venue_code": "v08",
                        "day": 9,
                        "hours": "19",
                        "group_ref": "GROUP1",
                        "headcount": 8,
                    }
                ]
            ).encode("utf-8")
        )
        now = datetime(2026, 7, 31, tzinfo=TZ)
        tasks = load_tasks(p, now=now)
        assert len(tasks) == 1
        assert tasks[0]["date"] == "2026-08-09"
        assert tasks[0]["headcount"] == 8

    def test_invalid_day_for_next_month(self) -> None:
        with pytest.raises(ValueError, match="day=31"):
            next_month_date(31, now=datetime(2026, 1, 15, tzinfo=TZ))


class TestCoreHelpersUnchanged:
    def test_time_slot_to_jp(self) -> None:
        assert time_slot_to_jp("09:00-11:00") == "9時～11時"
        assert time_slot_to_jp("19:00-21:00") == "19時～21時"


class TestStepLogPrefix:
    def test_team_initial_and_when_format(self) -> None:
        assert _team_initial(group_name="ネットワーククラブ") == "ネ"
        assert _team_initial(group_name="猪(小)倶楽部") == "猪"
        assert _team_initial(group_name="") == "-"
        assert _format_step_when("2026-09-06", "09:00-13:00") == "09-06 09:00-13:00"
        merged = display_time_range(["09:00-11:00", "11:00-13:00"])
        assert merged == "09:00-13:00"
        assert _format_step_when("2026-09-15", merged) == "09-15 09:00-13:00"

    def test_team_initial_from_groups_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "groups.json"
        p.write_text(
            json.dumps(
                {
                    "GROUP13": {
                        "id": "25128000007",
                        "name": "ネクサス",
                        "password": "x",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("core.booking.PATH_GROUPS", p)
        assert _team_initial(group_id="25128000007") == "ネ"

    def test_step_log_prefix_no_space(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="core.booking"):
            _step_log(
                "25128000007",
                "横曽根公民館",
                "予約開始: 2026-09-06 09:00-13:00",
                date_s="2026-09-06",
                time_slot="09:00-13:00",
                group_name="ネクサス",
            )
        assert any(
            r.getMessage()
            == "[ネ|横曽根公民館|09-06 09:00-13:00]予約開始: 2026-09-06 09:00-13:00"
            for r in caplog.records
        )
