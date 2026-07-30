"""Tests for core.notifier.send_msg."""

from __future__ import annotations

from datetime import date
from email import message_from_string, policy
from email.header import decode_header, make_header
from unittest.mock import MagicMock, patch

from core.notifier import send_msg


def _subj(raw: str) -> str:
    return str(make_header(decode_header(raw)))


def test_send_ok(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("CFG_A1", "a@x.com")
    monkeypatch.setenv("CFG_A2", "secret")
    monkeypatch.setenv("CFG_A3", "b@y.com")

    mock_smtp = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_smtp
    mock_cm.__exit__.return_value = False

    with patch("core.notifier.smtplib.SMTP_SSL", return_value=mock_cm) as cls:
        ok = send_msg(["v01|2026-08-01 19:00-19:30"], True, when=date(2026, 7, 30))

    assert ok is True
    cls.assert_called_once_with("smtp.gmail.com", 465)
    raw = mock_smtp.sendmail.call_args[0][2]
    parsed = message_from_string(raw, policy=policy.default)
    assert _subj(parsed["Subject"]) == "N(7/30)"
    assert "v01|" in parsed.get_content()


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
        assert send_msg(["x"], True) is False
