"""Task stub."""

from __future__ import annotations

from typing import Any

from core.session import Sess1


def load_tasks(path: str = "config/tasks.json") -> list[dict[str, Any]]:
    raise NotImplementedError


def do_one(sess: Sess1, a: str, b: str, d: str, t: str, **kwargs: Any) -> dict[str, Any]:
    raise NotImplementedError


def run_one(sess: Sess1, task: dict[str, Any]) -> dict[str, Any]:
    raise NotImplementedError


def run_many(sessions: dict[str, Sess1], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raise NotImplementedError
