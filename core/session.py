"""Session stub."""

from __future__ import annotations

from typing import Any


class Sess1:
    def __init__(self, gid: str, u: str, p: str) -> None:
        self.gid = gid
        self.u = u
        self.p = p

    def login(self) -> bool:
        raise NotImplementedError

    def ensure_alive(self) -> bool:
        raise NotImplementedError

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def make_sess(gid: str) -> Sess1:
    raise NotImplementedError
