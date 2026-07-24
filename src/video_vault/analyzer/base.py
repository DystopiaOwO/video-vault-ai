from __future__ import annotations

from typing import Protocol


class Analyzer(Protocol):
    provider: str
    model: str

    def analyze(self, video: dict) -> dict: ...
