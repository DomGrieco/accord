from __future__ import annotations

from typing import Any


class XurlPublisher:
    def __init__(self, executable: str = "xurl") -> None:
        self.executable = executable

    def publish(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
