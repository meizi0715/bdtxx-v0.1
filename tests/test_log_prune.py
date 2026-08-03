"""Tests for log pruning and quiet INFO output."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import scanner as sc
from core.scanner import prune_log


class TestPruneLog:
    def test_keeps_last_three_days(self, tmp_path: Path) -> None:
        p = tmp_path / "scan.log"
        p.write_text(
            "\n".join(
                [
                    "2026-07-27 10:00:00,000 INFO x: old1",
                    "2026-07-28 10:00:00,000 INFO x: old2",
                    "2026-07-29 10:00:00,000 INFO x: keep1",
                    "2026-07-30 10:00:00,000 INFO x: keep2",
                    "2026-07-31 10:00:00,000 INFO x: keep3",
                    "2026-07-31 10:00:01,000 INFO x: keys=1 added=0 removed=0 changed=False mailed=False",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prune_log(p, keep_days=3, now=datetime(2026, 7, 31, 12, 0, 0))
        text = p.read_text(encoding="utf-8")
        assert "old1" not in text
        assert "old2" not in text
        assert "keep1" in text
        assert "keep2" in text
        assert "keep3" in text
        assert "keys=1" in text

    def test_empty_file_ok(self, tmp_path: Path) -> None:
        p = tmp_path / "scan.log"
        p.write_text("", encoding="utf-8")
        prune_log(p, keep_days=3, now=datetime(2026, 7, 31, 12, 0, 0))
        assert p.read_text(encoding="utf-8") == ""

    def test_prune_paths_independent(self, tmp_path: Path) -> None:
        scan = tmp_path / "scan.log"
        book = tmp_path / "booking.log"
        scan.write_text(
            "2026-07-27 10:00:00,000 INFO s: scan-old\n"
            "2026-07-31 10:00:00,000 INFO s: scan-new\n",
            encoding="utf-8",
        )
        book.write_text(
            "2026-07-27 10:00:00,000 INFO b: book-old\n"
            "2026-07-31 10:00:00,000 INFO b: book-new\n",
            encoding="utf-8",
        )
        now = datetime(2026, 7, 31, 12, 0, 0)
        prune_log(scan, keep_days=3, now=now)
        prune_log(book, keep_days=3, now=now)
        assert "scan-old" not in scan.read_text(encoding="utf-8")
        assert "scan-new" in scan.read_text(encoding="utf-8")
        assert "book-old" not in book.read_text(encoding="utf-8")
        assert "book-new" in book.read_text(encoding="utf-8")

    def test_changes_log_keeps_ninety_days(self, tmp_path: Path) -> None:
        p = tmp_path / "changes.log"
        p.write_text(
            "\n".join(
                [
                    "2026-04-01 10:00:00,000 INFO core.scanner.changes: too-old",
                    "2026-05-10 10:00:00,000 INFO core.scanner.changes: keep-edge",
                    "2026-07-31 10:00:00,000 INFO core.scanner.changes: keep-new",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        prune_log(p, keep_days=90, now=datetime(2026, 7, 31, 12, 0, 0))
        text = p.read_text(encoding="utf-8")
        assert "too-old" not in text
        assert "keep-edge" in text
        assert "keep-new" in text


class TestSetupLogSplit:
    def test_handlers_target_separate_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scan_path = tmp_path / "scan.log"
        book_path = tmp_path / "booking.log"
        changes_path = tmp_path / "changes.log"
        monkeypatch.setattr(sc, "PATH_LOG", scan_path)
        monkeypatch.setattr(sc, "PATH_BOOKING_LOG", book_path)
        monkeypatch.setattr(sc, "PATH_CHANGES_LOG", changes_path)
        monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sc, "_LOG_READY", False)
        monkeypatch.setattr(sc, "_BOOKING_LOG_READY", False)
        monkeypatch.setattr(sc, "_CHANGES_LOG_READY", False)

        # Clear leftover handlers from other tests
        for name in ("core.scanner", "core.booking", "core.scanner.changes"):
            log = logging.getLogger(name)
            for h in list(log.handlers):
                log.removeHandler(h)
                h.close()

        sc.setup_logging()
        sc.setup_booking_logging()

        logging.getLogger("core.scanner").info("marker-scan-only")
        # booking FileHandler only accepts INFO marked as booking_step
        logging.getLogger("core.booking").info(
            "marker-book-only", extra={"booking_step": True}
        )
        logging.getLogger("core.booking").info("noise-not-step")
        logging.getLogger("core.booking").debug("apply retry 1/20: detail")
        logging.getLogger("core.scanner.changes").info(
            "keys=1 added=1 removed=0 changed=True mailed=True"
        )

        for h in logging.getLogger("core.scanner").handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()
        for h in logging.getLogger("core.booking").handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()
        for h in logging.getLogger("core.scanner.changes").handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()

        scan_text = scan_path.read_text(encoding="utf-8")
        book_text = book_path.read_text(encoding="utf-8")
        changes_text = changes_path.read_text(encoding="utf-8")
        assert "marker-scan-only" in scan_text
        assert "marker-book-only" not in scan_text
        assert "marker-book-only" in book_text
        assert "marker-scan-only" not in book_text
        assert "noise-not-step" not in book_text
        assert "apply retry" not in book_text
        assert "keys=1 added=1 removed=0 changed=True mailed=True" in changes_text
        assert "marker-scan-only" not in changes_text


class TestChangesEventLog:
    def test_changed_and_mailed_writes_changes_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changes_path = tmp_path / "changes.log"
        monkeypatch.setattr(sc, "PATH_CHANGES_LOG", changes_path)
        monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sc, "_CHANGES_LOG_READY", False)
        log = logging.getLogger("core.scanner.changes")
        for h in list(log.handlers):
            log.removeHandler(h)
            h.close()
        sc.setup_logging()

        key = "v01|2026-09-01 19:00-19:30"
        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: True)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(sc, "collect_all", lambda *a, **k: ({key}, set()))

        r = sc.run_task(
            datetime(2026, 7, 30, 10, 0, 0),
            client=MagicMock(),
            items=[{"code": "v01", "fid": 1, "sid": 2}],
            path_t1=tmp_path / "t1.json",
            path_t2=tmp_path / "t2.json",
            path_sup=tmp_path / "sup.json",
            path_fail=tmp_path / "fail_count.json",
            path_daily_sent=tmp_path / "daily_sent.json",
            send=True,
        )
        assert r["changed"] is True
        assert r["mailed"] is True
        for h in log.handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()
        text = changes_path.read_text(encoding="utf-8")
        assert "changed=True" in text
        assert "mailed=True" in text

    def test_unchanged_skips_changes_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        changes_path = tmp_path / "changes.log"
        monkeypatch.setattr(sc, "PATH_CHANGES_LOG", changes_path)
        monkeypatch.setattr(sc, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sc, "_CHANGES_LOG_READY", False)
        log = logging.getLogger("core.scanner.changes")
        for h in list(log.handlers):
            log.removeHandler(h)
            h.close()
        sc.setup_logging()

        key = "v01|2026-09-01 19:00-19:30"
        sc.proc_f([key], path_t2=tmp_path / "t2.json")
        monkeypatch.setattr("core.notifier.send_msg", lambda *a, **k: True)
        monkeypatch.setenv("CFG_B1", "https://example.test")
        monkeypatch.setenv("CFG_B2", "tenant")
        monkeypatch.setattr(sc, "collect_all", lambda *a, **k: ({key}, set()))

        (tmp_path / "daily_sent.json").write_text(
            '{"last_sent_date": "2026-07-30"}', encoding="utf-8"
        )
        r = sc.run_task(
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
        assert r["changed"] is False
        assert r["mailed"] is False
        for h in log.handlers:
            if isinstance(h, logging.FileHandler):
                h.flush()
        if changes_path.exists():
            assert "changed=True" not in changes_path.read_text(encoding="utf-8")
