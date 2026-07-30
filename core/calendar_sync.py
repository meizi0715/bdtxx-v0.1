"""Calendar stub."""

from __future__ import annotations

from typing import Any


def load_cal_cfg(path: str = "config/cal.json") -> dict[str, Any]:
    raise NotImplementedError


def get_svc(cred: str | None = None) -> Any:
    raise NotImplementedError


def list_ev(cal_id: str, *, tmin: str, tmax: str, svc: Any | None = None) -> list[dict[str, Any]]:
    raise NotImplementedError


def add_ev(cal_id: str, row: dict[str, Any], *, svc: Any | None = None) -> dict[str, Any]:
    raise NotImplementedError


def upd_ev(cal_id: str, eid: str, row: dict[str, Any], *, svc: Any | None = None) -> dict[str, Any]:
    raise NotImplementedError


def del_ev(cal_id: str, eid: str, *, svc: Any | None = None) -> bool:
    raise NotImplementedError


def sync_rows(rows: list[dict[str, Any]], *, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    raise NotImplementedError
