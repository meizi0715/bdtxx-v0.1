"""Tests for core.notifier body formatting and SMTP send."""

from __future__ import annotations

import json
from datetime import date
from email import message_from_string, policy
from email.header import decode_header, make_header
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.notifier import _slot_dates, build_mail_body, build_mail_subject, send_msg

_NAMES = {
    "v01": {"name": "体育武道センター", "letter": "A"},
    "v02": {"name": "西スポーツセンター", "letter": "B"},
    "v08": {"name": "青木公民館", "letter": "H"},
    "v12": {"name": "横曽根公民館", "letter": "L"},
}

_TMPL = {
    "subject": "バトミントン予約可能時間帯({mmdd})",
    "header": (
        "ご担当者様\n\nお疲れ様です。\n"
        "掲題の件につきまして、翌月末まで川口市各施設バトミントン予約可能時間帯を送ります。\n"
        "※対象時間帯：平日19:00~21:00、祝日/週末終日※"
    ),
}


def _subj(raw: str) -> str:
    return str(make_header(decode_header(raw)))


class TestBuildMailBody:
    def test_merge_slots_date_format_and_partial_venues(self) -> None:
        lines = [
            "v08|2026-08-01 09:00-11:00",
            "v08|2026-08-01 19:00-21:00",
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-05 19:30-20:00",
            "v12|2026-08-02 10:00-12:00",
        ]
        body = build_mail_body(lines, names=_NAMES, template=_TMPL, load_cal=False, venue_list="")

        assert "ご担当者様" in body
        assert "翌月末まで川口市各施設バトミントン予約可能時間帯を送ります。" in body
        assert "----------予約可能----------" in body

        assert "【B.西スポーツセンター】" not in body
        assert "【西スポーツセンター】" not in body
        pos_a = body.index("【体育武道センター】")
        pos_h = body.index("【青木公民館】")
        pos_l = body.index("【横曽根公民館】")
        assert pos_a < pos_h < pos_l

        assert "・8月5日（水） - 19:00～19:30、19:30～20:00" in body
        assert "・8月1日（土） - 9:00～11:00、19:00～21:00" in body
        assert "・8月2日（日） - 10:00～12:00" in body

        # blank line between venue blocks; module closes with separator
        assert "・8月5日（水） - 19:00～19:30、19:30～20:00\n\n【青木公民館】" in body
        assert "・8月1日（土） - 9:00～11:00、19:00～21:00\n\n【横曽根公民館】" in body
        assert body.rstrip("\n").endswith("-----------------------------")
        assert "・8月2日（日） - 10:00～12:00\n-----------------------------" in body
        assert not body.endswith("\n\n")
        assert "(★)" not in body

    def test_new_slot_marked_star_unchanged_unmarked(self) -> None:
        lines = [
            "v08|2026-08-03 19:00-21:00",  # added this round
            "v01|2026-08-05 19:00-19:30",  # already in t2
        ]
        added = {"v08|2026-08-03 19:00-21:00"}
        body = build_mail_body(
            lines,
            names=_NAMES,
            template=_TMPL,
            load_cal=False,
            venue_list="",
            added=added,
        )
        assert "・8月3日（月） - 19:00～21:00(★)" in body
        assert "・8月5日（水） - 19:00～19:30\n\n【青木公民館】" in body
        assert "・8月5日（水） - 19:00～19:30(★)" not in body

    def test_merged_day_line_star_if_any_added(self) -> None:
        lines = [
            "v08|2026-08-08 13:00-15:00",  # added
            "v08|2026-08-08 17:00-19:00",  # already in t2
        ]
        added = {"v08|2026-08-08 13:00-15:00"}
        body = build_mail_body(
            lines,
            names=_NAMES,
            template=_TMPL,
            load_cal=False,
            venue_list="",
            added=added,
        )
        assert "・8月8日（土） - 13:00～15:00、17:00～19:00(★)" in body

        # none in added → no star
        body2 = build_mail_body(
            lines,
            names=_NAMES,
            template=_TMPL,
            load_cal=False,
            venue_list="",
            added=set(),
        )
        assert "・8月8日（土） - 13:00～15:00、17:00～19:00\n" in body2 or (
            "・8月8日（土） - 13:00～15:00、17:00～19:00" in body2
            and "(★)" not in body2.split("----------予約可能----------", 1)[1]
        )

    def test_matched_dates_exclude_fully_suppressed_days(self) -> None:
        lines = [
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-05 19:30-20:00",
            "v08|2026-08-03 19:00-21:00",
        ]
        # 8/5 fully suppressed → omitted; 8/3 fresh → kept
        suppressed = {
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-05 19:30-20:00",
        }
        assert _slot_dates(lines, suppressed=suppressed) == {date(2026, 8, 3)}

    def test_matched_dates_keep_day_if_any_key_fresh(self) -> None:
        lines = [
            "v08|2026-08-08 13:00-15:00",
            "v08|2026-08-08 17:00-19:00",
        ]
        suppressed = {"v08|2026-08-08 17:00-19:00"}
        assert _slot_dates(lines, suppressed=suppressed) == {date(2026, 8, 8)}

    def test_matched_dates_unchanged_when_nothing_suppressed(self) -> None:
        lines = [
            "v08|2026-08-03 19:00-21:00",
            "v01|2026-08-05 19:00-19:30",
        ]
        expected = {date(2026, 8, 3), date(2026, 8, 5)}
        assert _slot_dates(lines) == expected
        assert _slot_dates(lines, suppressed=set()) == expected
        assert _slot_dates(lines, suppressed=None) == expected

    def test_build_mail_body_passes_filtered_dates_to_get_matched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[set[date]] = []

        def fake_matched(dates: set[date], **_k):  # noqa: ANN003
            captured.append(set(dates))
            return {d: [f"booked {d.isoformat()}"] for d in dates}

        monkeypatch.setattr("core.calendar_read.get_matched", fake_matched)
        monkeypatch.setattr(
            "core.calendar_read.get_recent",
            lambda days, **_k: {d: [] for d in days},
        )
        monkeypatch.setattr("core.calendar_read.get_counts", lambda months: {})

        lines = [
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-05 19:30-20:00",
            "v08|2026-08-03 19:00-21:00",
        ]
        suppressed = {
            "v01|2026-08-05 19:00-19:30",
            "v01|2026-08-05 19:30-20:00",
        }
        # ★ uses added (independent of suppressed date filter)
        added = {"v08|2026-08-03 19:00-21:00"}
        body = build_mail_body(
            lines,
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 8, 1),
            scan_end=date(2026, 8, 31),
            load_cal=True,
            venue_list="",
            suppressed=suppressed,
            added=added,
            count_rows={},
        )
        assert captured == [{date(2026, 8, 3)}]
        # 予約可能 still shows the suppressed day; ★ only from added
        assert "・8月5日（水） - 19:00～19:30、19:30～20:00" in body
        assert "・8月5日（水） - 19:00～19:30、19:30～20:00(★)" not in body
        assert "・8月3日（月） - 19:00～21:00(★)" in body
        assert "----------予約済み----------" in body
        assert "booked 2026-08-03" in body
        assert "booked 2026-08-05" not in body

        # suppressed alone does not drive ★
        body_no_added = build_mail_body(
            lines,
            names=_NAMES,
            template=_TMPL,
            when=date(2026, 8, 1),
            scan_end=date(2026, 8, 31),
            load_cal=True,
            venue_list="",
            suppressed=suppressed,
            count_rows={},
        )
        assert "(★)" not in body_no_added
        assert "booked 2026-08-05" not in body_no_added
        assert captured[-1] == {date(2026, 8, 3)}

    def test_empty_slots_still_has_header(self) -> None:
        body = build_mail_body([], names=_NAMES, template=_TMPL, load_cal=False, venue_list="")
        assert body.startswith("ご担当者様")
        assert "----------予約可能----------" in body
        assert "-----------------------------" in body
        assert "【" not in body.split("----------予約可能----------", 1)[1].split("-----------------------------", 1)[0]

    def test_footer_appended_raw(self, tmp_path: Path) -> None:
        p = tmp_path / "mail_content.json"
        p.write_text(
            json.dumps(
                {
                    "subject": "S({mmdd})",
                    "header": "H",
                    "footer": (
                        "----------target list----------\n"
                        "・item A\n・item B\n-----------------------------"
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        body = build_mail_body(
            ["v08|2026-08-01 09:00-11:00"],
            names=_NAMES,
            template_path=p,
            load_cal=False,
        )
        assert body.rstrip("\n").endswith(
            "----------target list----------\n・item A\n・item B\n-----------------------------"
        )
        assert "\n\n----------target list----------\n" in body

    def test_footer_missing_file_uses_defaults(
        self, tmp_path: Path, caplog
    ) -> None:  # noqa: ANN001
        missing = tmp_path / "nope.json"
        with caplog.at_level("ERROR"):
            body = build_mail_body(
                [],
                names=_NAMES,
                template_path=missing,
                load_cal=False,
            )
        assert "target list" not in body
        assert "mail content missing" in caplog.text

    def test_merged_content_matches_legacy_header_and_footer(self) -> None:
        """header/footer from mail_content.json match pre-merge sources."""
        root = Path(__file__).resolve().parents[1]
        content_path = root / "local" / "mail_content.json"
        if not content_path.exists():
            pytest.skip("local/mail_content.json not present")
        content = json.loads(content_path.read_text(encoding="utf-8"))
        assert content["subject"] == "バトミントン予約可能時間帯({mmdd})"
        assert content["header"] == (
            "ご担当者様\n\nお疲れ様です。\n"
            "掲題の件につきまして、翌月末まで川口市各施設バトミントン予約可能時間帯を送ります。\n"
            "※対象時間帯：平日19:00~21:00、祝日/週末終日※"
        )
        assert content["footer"] == (
            "----------対象施設----------\n"
            "・西公民館\n"
            "・並木公民館\n"
            "・幸栄公民館\n"
            "・青木東公民館\n"
            "・上青木公民館\n"
            "・朝日東公民館\n"
            "・横曽根公民館\n"
            "・体育武道センター\n"
            "・西スポーツセンター\n"
            "・芝スポーツセンター\n"
            "・中央ふれあい館ホール1\n"
            "・中央ふれあい館ホール2\n"
            "-----------------------------"
        )
        body = build_mail_body(
            [],
            names=_NAMES,
            template_path=content_path,
            load_cal=False,
        )
        assert body.startswith(content["header"].rstrip("\n"))
        assert body.rstrip("\n").endswith(content["footer"])
        assert "\n\n----------対象施設----------\n" in body
        assert "・中央ふれあい館ホール1\n・中央ふれあい館ホール2\n" in body

    def test_subject_mmdd(self) -> None:
        assert build_mail_subject(date(2026, 7, 30), template=_TMPL) == (
            "バトミントン予約可能時間帯(7/30)"
        )


def test_send_ok(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CFG_A1", "a@x.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "b@y.com")

    mock_smtp = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_smtp
    mock_cm.__exit__.return_value = False

    with patch("core.notifier.smtplib.SMTP_SSL", return_value=mock_cm) as cls:
        ok = send_msg(
            ["v08|2026-08-01 09:00-11:00"],
            True,
            when=date(2026, 7, 30),
            names=_NAMES,
            template=_TMPL,
            load_cal=False,
        )

    assert ok is True
    cls.assert_called_once_with("smtp.gmail.com", 465)
    raw = mock_smtp.sendmail.call_args[0][2]
    parsed = message_from_string(raw, policy=policy.default)
    assert _subj(parsed["Subject"]) == "バトミントン予約可能時間帯(7/30)"
    content = parsed.get_content()
    assert "【青木公民館】" in content
    assert "9:00～11:00" in content
    assert "v08|" not in content


def test_send_multiple_recipients(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CFG_A1", "a@x.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", " b@y.com , c@z.com, ,d@w.com ")

    mock_smtp = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_smtp
    mock_cm.__exit__.return_value = False

    with patch("core.notifier.smtplib.SMTP_SSL", return_value=mock_cm):
        ok = send_msg(
            ["v08|2026-08-01 09:00-11:00"],
            True,
            when=date(2026, 7, 30),
            names=_NAMES,
            template=_TMPL,
            load_cal=False,
            venue_list="",
        )

    assert ok is True
    to_arg = mock_smtp.sendmail.call_args[0][1]
    assert to_arg == ["b@y.com", "c@z.com", "d@w.com"]
    raw = mock_smtp.sendmail.call_args[0][2]
    parsed = message_from_string(raw, policy=policy.default)
    assert parsed["To"] == "b@y.com, c@z.com, d@w.com"


def test_send_missing_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("CFG_A1", raising=False)
    monkeypatch.delenv("CFG_A2", raising=False)
    monkeypatch.delenv("CFG_A3", raising=False)
    assert send_msg(["x"], True) is False


def test_send_error(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CFG_A1", "a@x.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "b@y.com")
    with patch("core.notifier.smtplib.SMTP_SSL", side_effect=OSError("x")):
        assert send_msg(["x"], True, names=_NAMES, template=_TMPL, load_cal=False) is False


def test_check_mail_cfg_rejects_multi_sender(monkeypatch) -> None:  # noqa: ANN001
    from core.notifier import check_mail_cfg

    monkeypatch.setenv("CFG_A1", "a@x.com, b@y.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "c@z.com")
    with pytest.raises(SystemExit, match="CFG_A1 应为单个发件邮箱"):
        check_mail_cfg()


def test_check_mail_cfg_allows_multi_recipient(monkeypatch) -> None:  # noqa: ANN001
    from core.notifier import check_mail_cfg

    monkeypatch.setenv("CFG_A1", "a@x.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "b@y.com, c@z.com")
    check_mail_cfg()  # no raise


def test_send_rejects_multi_sender(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CFG_A1", "a@x.com,b@y.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "c@z.com")
    assert send_msg(["x"], True, names=_NAMES, template=_TMPL, load_cal=False) is False
