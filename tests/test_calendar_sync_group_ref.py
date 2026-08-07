"""Tests for calendar_sync group_ref-based event replace/delete."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from core import calendar_sync as cs


def _ev(eid: str, *, group_ref: str = "", group_name: str = "ＢＭチーム") -> dict[str, Any]:
    priv: dict[str, str] = {
        "reservation_id": eid,
        "group_name": group_name,
    }
    if group_ref:
        priv["group_ref"] = group_ref
    return {
        "id": eid,
        "extendedProperties": {"private": priv},
    }


class TestEventMatchesGroup:
    def test_prefers_group_ref_over_same_name(self) -> None:
        priv = {
            "group_ref": "GROUP3",
            "group_name": "ＢＭチーム",
            "reservation_id": "1",
        }
        assert cs._event_matches_group(
            priv, group_ref="GROUP3", group_name="ＢＭチーム"
        )
        assert not cs._event_matches_group(
            priv, group_ref="GROUP8", group_name="ＢＭチーム"
        )

    def test_legacy_without_group_ref_falls_back_to_name(self) -> None:
        priv = {"group_name": "ＢＭチーム", "reservation_id": "1"}
        assert cs._event_matches_group(
            priv, group_ref="GROUP8", group_name="ＢＭチーム"
        )
        assert not cs._event_matches_group(
            priv, group_ref="GROUP8", group_name="別チーム"
        )


class TestDeleteByGroupRefNoCrossDelete:
    def test_same_name_different_ref_only_deletes_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = [
            _ev("gym-bm", group_ref="GROUP3", group_name="ＢＭチーム"),
            _ev("hall-bm", group_ref="GROUP8", group_name="ＢＭチーム"),
            _ev("other", group_ref="GROUP1", group_name="猪(小)倶楽部"),
        ]
        deleted: list[str] = []

        client = MagicMock()

        def fake_list_range(_client, _cal_id, _tmin, _tmax):  # noqa: ANN001
            return list(events)

        monkeypatch.setattr(cs, "_list_range", fake_list_range)

        def fake_delete(*, calendarId, eventId):  # noqa: ANN001
            deleted.append(eventId)
            return MagicMock(execute=MagicMock(return_value={}))

        client.events.return_value.delete.side_effect = (
            lambda **kw: MagicMock(execute=lambda: fake_delete(**kw) or {})
        )
        # Simpler: intercept delete().execute via side_effect on delete
        def delete_call(**kwargs):  # noqa: ANN003
            deleted.append(kwargs["eventId"])
            m = MagicMock()
            m.execute.return_value = {}
            return m

        client.events.return_value.delete.side_effect = delete_call

        n = cs._delete_group_synced_events(
            client,
            "cal-reg",
            "GROUP8",
            "ＢＭチーム",
            "2026-08-01T00:00:00+09:00",
            "2026-10-01T00:00:00+09:00",
        )
        assert n == 1
        assert deleted == ["hall-bm"]
        assert "gym-bm" not in deleted

    def test_replace_writes_group_ref_and_spares_other_same_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CFG_C1", "local/service-account.json")
        monkeypatch.setenv("CFG_C2", "cal-reg")
        monkeypatch.setenv("CFG_C3", "cal-lot")

        existing = [
            _ev("gym-bm", group_ref="GROUP3", group_name="ＢＭチーム"),
            _ev("hall-bm", group_ref="GROUP8", group_name="ＢＭチーム"),
        ]
        deleted: list[tuple[str, str]] = []
        inserted: list[dict[str, Any]] = []

        class _Events:
            def delete(self, **kwargs):  # noqa: ANN003
                deleted.append((kwargs["calendarId"], kwargs["eventId"]))
                m = MagicMock()
                m.execute.return_value = {}
                return m

            def insert(self, **kwargs):  # noqa: ANN003
                inserted.append(
                    {
                        "calendarId": kwargs["calendarId"],
                        "body": kwargs["body"],
                    }
                )
                m = MagicMock()
                m.execute.return_value = {"id": "new"}
                return m

        class _Svc:
            def events(self):
                return _Events()

        monkeypatch.setattr(cs, "_cal_svc", lambda *_a, **_k: _Svc())
        monkeypatch.setattr(
            cs, "_list_range", lambda *_a, **_k: list(existing)
        )
        monkeypatch.setattr(cs, "load_venue_short_names", lambda: {})
        monkeypatch.setattr(
            cs,
            "merge_consecutive_reservations",
            lambda _rows: [
                {
                    "start_dt": datetime(2026, 8, 10, 9, 0, tzinfo=cs._TZ),
                    "end_dt": datetime(2026, 8, 10, 11, 0, tzinfo=cs._TZ),
                    "reservation_ids": ["r-new"],
                    "inferred_type": "regular",
                    "receptionDate": "2026-07-01",
                    "facilitiesName": "西スポーツセンター",
                    "facilityId": "9",
                    "status": "6",
                    "screeningResult": "",
                }
            ],
        )

        n = cs.replace_group_events(
            [],
            group_ref="GROUP8",
            group_name="ＢＭチーム",
            start=date(2026, 8, 1),
            end=date(2026, 9, 30),
            svc=_Svc(),
        )
        assert n == 1
        assert deleted == [("cal-reg", "hall-bm"), ("cal-lot", "hall-bm")]
        assert all(eid != "gym-bm" for _cal, eid in deleted)
        priv = inserted[0]["body"]["extendedProperties"]["private"]
        assert priv["group_ref"] == "GROUP8"
        assert priv["group_name"] == "ＢＭチーム"
