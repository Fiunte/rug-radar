"""Solana Tracker: GET /tokens/{mint}; risk.score is 1-10, higher = riskier. Needs an API key."""

from __future__ import annotations

import os
from typing import Any

from rugwatch.collector.scanners import ScannerScore


class SolanaTracker:
    name = "solanatracker"
    base_url = "https://data.solanatracker.io"

    def request(self, mint: str) -> tuple[str, dict[str, str]]:
        return f"{self.base_url}/tokens/{mint}", {"accept": "application/json",
                                                  "x-api-key": os.environ.get("SOLANATRACKER_API_KEY", "")}

    def normalise(self, payload: Any) -> ScannerScore:
        risk = payload.get("risk") if isinstance(payload, dict) else None
        if not isinstance(risk, dict):
            return ScannerScore(None, None)
        score = risk.get("score")
        risks: list[Any] = risk.get("risks") if isinstance(risk.get("risks"), list) else []  # type: ignore[assignment]
        flags = sorted(str(r.get("name")) for r in risks if isinstance(r, dict) and r.get("name"))
        if risk.get("rugged"):
            flags.append("rugged")
        value = float(score) if isinstance(score, int | float) else None
        return ScannerScore(value, value, flags)
