"""Tests for calendar_read and mail sections."""

from __future__ import annotations

from datetime import date
from email import message_from_string, policy
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

from core import calendar_read as cr
from core.notifier import build_mail_body, send_msg

_NAMES = {
    "v01": {"name": "SiteA", "letter": "A"},
    "v08": {"name": "SiteH", "letter": "H"},
}
_TMPL = {
    "subject": "Report ({mmdd})",
    "header": "Hello,\n\nPlease find the report below.",
}


def _subj(raw: str) -> str:
    return str(make_header(decode_header(raw)))


def _stats_ev(
    month_key: str,
    gym: list[dict] | None = None,
    hall: list[dict] | None = None,
) -> dict:
    import json

    return {
        "summary": f"{int(month_key.split('-')[1])}月度予約統計",
        "start": {"date": f"{month_key}-28"},
        "extendedProperties": {
            "private": {
                "stats_kind": "monthly_reservation_stats",
                "stats_month": month_key,
                "gym": json.dumps(gym or [], ensure_ascii=False),
                "hall": json.dumps(hall or [], ensure_ascii=False),
            }
        },
    }


class TestCalendarRead:
    def test_get_recent_ok(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C2", "cal-a")
        monkeypatch.setenv("CFG_C3", "cal-b")

        def fake_list(svc, cal_id, d):  # noqa: ANN001, ANN202
            if cal_id == "cal-a" and d == date(2026, 7, 31):
                return [
                    {
                        "summary": "Alpha",
                        "start": {"dateTime": "2026-07-31T19:00:00+09:00"},
                    }
                ]
            if cal_id == "cal-b" and d == date(2026, 7, 31):
                return [
                    {
                        "summary": "Beta",
                        "start": {"dateTime": "2026-07-31T10:00:00+09:00"},
                    }
                ]
            return []

        monkeypatch.setattr(cr, "_list_day", fake_list)
        rows = cr.get_recent([date(2026, 7, 31), date(2026, 8, 1)], svc=object())
        assert rows[date(2026, 7, 31)] == [
            "10:00 Beta（抽選）",
            "19:00 Alpha（予約）",
        ]
        assert rows[date(2026, 8, 1)] == []

    def test_get_matched_empty_day_omitted_by_caller(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C2", "cal-a")
        monkeypatch.setenv("CFG_C3", "cal-b")
        monkeypatch.setattr(cr, "_list_day", lambda *a, **k: [])
        rows = cr.get_matched({date(2026, 8, 1)}, svc=object())
        assert rows[date(2026, 8, 1)] == []

    def test_get_counts_from_c4_stats(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C4", "cal-stats")

        def fake_month(svc, cal_id, year, month):  # noqa: ANN001, ANN202
            assert cal_id == "cal-stats"
            if (year, month) != (2026, 8):
                return []
            return [
                _stats_ev(
                    "2026-08",
                    gym=[{"name": "B", "count": 2}, {"name": "A", "count": 2}],
                    hall=[{"name": "D", "count": 1}, {"name": "G", "count": 3}],
                )
            ]

        monkeypatch.setattr(cr, "_list_month", fake_month)
        rows = cr.get_counts([(2026, 8)], svc=object())
        assert rows[(2026, 8)]["体育館"] == [("A", 2), ("B", 2)]
        assert rows[(2026, 8)]["公民館"] == [("D", 1), ("G", 3)]

    def test_get_counts_missing_month_empty(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C4", "cal-stats")
        monkeypatch.setattr(cr, "_list_month", lambda *a, **k: [])
        rows = cr.get_counts([(2026, 7)], svc=object())
        assert rows[(2026, 7)]["体育館"] == []
        assert rows[(2026, 7)]["公民館"] == []

    def test_get_counts_skips_wrong_month_key(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C4", "cal-stats")

        def fake_month(svc, cal_id, year, month):  # noqa: ANN001, ANN202
            return [
                _stats_ev(
                    "2026-09",
                    gym=[{"name": "X", "count": 9}],
                    hall=[],
                )
            ]

        monkeypatch.setattr(cr, "_list_month", fake_month)
        rows = cr.get_counts([(2026, 8)], svc=object())
        assert rows[(2026, 8)]["体育館"] == []
        assert rows[(2026, 8)]["公民館"] == []

    def test_get_counts_three_months(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C4", "cal-stats")

        def fake_month(svc, cal_id, year, month):  # noqa: ANN001, ANN202
            return [
                _stats_ev(
                    f"{year:04d}-{month:02d}",
                    gym=[],
                    hall=[{"name": f"M{month}", "count": month}],
                )
            ]

        monkeypatch.setattr(cr, "_list_month", fake_month)
        months = [(2026, 7), (2026, 8), (2026, 9)]
        rows = cr.get_counts(months, svc=object())
        assert list(rows.keys()) == months
        assert rows[(2026, 7)]["公民館"] == [("M7", 7)]
        assert rows[(2026, 8)]["公民館"] == [("M8", 8)]
        assert rows[(2026, 9)]["公民館"] == [("M9", 9)]


class TestMailCalSections:
    def test_body_with_recent_and_matched(self) -> None:
        slots = ["v08|2026-08-01 09:00-11:00"]
        recent = {
            date(2026, 7, 31): ["19:00 Foo（予約）"],
            date(2026, 8, 1): [],
        }
        matched = {date(2026, 8, 1): ["10:00 Bar（抽選）"]}
        counts = {
            (2026, 7): {"体育館": [("A", 1)], "公民館": []},
            (2026, 8): {"体育館": [("B", 2)], "公民館": [("C", 3)]},
        }
        body = build_mail_body(
            slots,
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 7, 31),
            scan_end=date(2026, 8, 31),
            recent_rows=recent,
            matched_rows=matched,
            count_rows=counts,
            load_cal=False,
            venue_list="",
        )
        assert "----------直近予定----------" in body
        assert "【7月31日（金）】　今日" in body
        assert "・19:00 Foo（予約）" in body
        assert "【8月1日（土）】　明日" in body
        assert "・なし" in body
        assert "今日" in body and "明日" in body
        # blank line between 今日 / 明日
        assert "・19:00 Foo（予約）\n\n【8月1日（土）】　明日" in body
        assert "----------予約可能----------" in body
        assert "【SiteH】" in body
        assert "----------予約済み----------" in body
        assert "・10:00 Bar（抽選）" in body
        assert "----------予約件数----------" in body
        assert "【7月】\n・体育館：A(1)" in body
        assert "【8月】\n・体育館：B(2)\n・公民館：C(3)" in body
        assert "公民館：" not in body.split("【7月】")[1].split("【8月】")[0]
        assert "-----------------------------\n\n----------予約可能----------\n" in body
        assert "-----------------------------\n\n----------予約済み----------\n" in body
        assert "-----------------------------\n\n----------予約件数----------\n" in body
        # 予約件数 truncates name to first char
        body2 = build_mail_body(
            slots,
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 7, 31),
            scan_end=date(2026, 8, 31),
            recent_rows={date(2026, 7, 31): [], date(2026, 8, 1): []},
            matched_rows={
                date(2026, 8, 1): ["10:00 A"],
                date(2026, 8, 2): ["11:00 B"],
            },
            count_rows={(2026, 8): {"体育館": [("アルファ", 2)], "公民館": []}},
            load_cal=False,
            venue_list="",
        )
        assert "・体育館：ア(2)" in body2
        assert "10:00 A\n\n【8月2日" in body2

    def test_counts_skip_empty_month_and_group(self) -> None:
        body = build_mail_body(
            ["v01|2026-08-05 19:00-21:00"],
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 7, 31),
            scan_end=date(2026, 9, 30),
            recent_rows={date(2026, 7, 31): [], date(2026, 8, 1): []},
            matched_rows={},
            count_rows={
                (2026, 7): {"体育館": [], "公民館": []},
                (2026, 8): {"体育館": [("X", 1)], "公民館": []},
                (2026, 9): {"体育館": [], "公民館": [("Y", 2)]},
            },
            load_cal=False,
            venue_list="",
        )
        assert "【7月】" not in body
        assert "【8月】\n・体育館：X(1)" in body
        assert "・公民館：" not in body.split("【8月】")[1].split("【9月】")[0]
        assert "【9月】\n・公民館：Y(2)" in body

    def test_matched_skips_empty_days(self) -> None:
        body = build_mail_body(
            ["v01|2026-08-05 19:00-21:00"],
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 7, 31),
            recent_rows={date(2026, 7, 31): [], date(2026, 8, 1): []},
            matched_rows={date(2026, 8, 5): []},
            count_rows={},
            load_cal=False,
            venue_list="",
        )
        assert "----------直近予定----------" in body
        assert "・なし" in body
        assert "----------予約済み----------" not in body
        assert "----------予約件数----------" not in body

    def test_cal_failure_still_sends_avail(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setenv("CFG_A1", "a@x.com")
        monkeypatch.setenv("CFG_A2", "secret")
        monkeypatch.setenv("CFG_A3", "b@y.com")

        def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("cal down")

        monkeypatch.setattr("core.calendar_read.get_recent", boom)
        monkeypatch.setattr("core.calendar_read.get_matched", boom)
        monkeypatch.setattr("core.calendar_read.get_counts", boom)

        mock_smtp = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_smtp
        mock_cm.__exit__.return_value = False

        with patch("core.notifier.smtplib.SMTP_SSL", return_value=mock_cm):
            ok = send_msg(
                ["v08|2026-08-01 09:00-11:00"],
                True,
                when=date(2026, 7, 31),
                names=_NAMES,
                template=_TMPL,
                load_cal=True,
                venue_list="",
            )
        assert ok is True
        raw = mock_smtp.sendmail.call_args[0][2]
        content = message_from_string(raw, policy=policy.default).get_content()
        assert "----------予約可能----------" in content
        assert "【SiteH】" in content
        assert "直近予定" not in content
        assert "予約済み" not in content
        assert "予約件数" not in content
