"""Tests for CFG_C4 structured stats payload."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core import stats_calendar as sc


class TestBuildStatsStructuredRows:
    def test_sorted_initials_with_zeros(self) -> None:
        groups = {
            "G1": {"name": "東クラブ", "type": "gym"},
            "G2": {"name": "西クラブ", "type": "gym"},
            "G3": {"name": "南館", "type": "hall"},
        }
        rows = sc.build_stats_structured_rows(
            groups, "gym", {"東クラブ": 3, "西クラブ": 0}
        )
        assert rows == [
            {"name": "西", "count": 0},
            {"name": "東", "count": 3},
        ]


class TestUpsertStructuredPrivate:
    def test_insert_includes_gym_hall_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CFG_C1", "local/x.json")
        monkeypatch.setenv("CFG_C2", "cal-reg")
        monkeypatch.setenv("CFG_C3", "cal-lot")
        monkeypatch.setenv("CFG_C4", "cal-stats")

        inserted: list[dict] = []

        class _Events:
            def list(self, **_k):  # noqa: ANN003
                return self

            def insert(self, **kwargs):  # noqa: ANN003
                inserted.append(kwargs["body"])
                return self

            def execute(self):
                return {"items": [], "id": "ev1"}

        class _Svc:
            def events(self):
                return _Events()

        groups = {
            "G1": {"name": "FlyTeam", "type": "gym"},
            "G2": {"name": "Surizu", "type": "hall"},
        }
        monkeypatch.setattr(sc, "_fid_to_venue_type", lambda: {9: "gym", 29: "hall"})

        def fake_month(_svc, cal_id, year, month):  # noqa: ANN001, ANN202
            if cal_id == "cal-reg":
                return [
                    {
                        "summary": "x",
                        "extendedProperties": {
                            "private": {
                                "reservation_id": "1",
                                "group_name": "FlyTeam",
                                "facilityId": "9",
                            }
                        },
                    }
                ]
            if cal_id == "cal-lot":
                return [
                    {
                        "summary": "y",
                        "extendedProperties": {
                            "private": {
                                "reservation_id": "2",
                                "group_name": "Surizu",
                                "facilityId": "29",
                            }
                        },
                    }
                ]
            return []

        monkeypatch.setattr(sc, "_list_month", fake_month)

        sc.upsert_month_stats(
            2026,
            8,
            groups=groups,
            svc=_Svc(),
        )
        assert len(inserted) == 1
        priv = inserted[0]["extendedProperties"]["private"]
        assert priv["stats_kind"] == "monthly_reservation_stats"
        assert priv["stats_month"] == "2026-08"
        gym = json.loads(priv["gym"])
        hall = json.loads(priv["hall"])
        assert gym == [{"name": "F", "count": 1}]
        assert hall == [{"name": "S", "count": 1}]
        assert "【8月】" in inserted[0]["description"]
