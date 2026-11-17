"""RugCheck: GET /v1/tokens/{mint}/report/summary. Works without a key (shared public quota)."""

from __future__ import annotations

import os
from typing import Any

from rugwatch.collector.scanners import ScannerScore


class RugCheck:
    name = "rugcheck"
    base_url = "https://api.rugcheck.xyz/v1"

    def request(self, mint: str) -> tuple[str, dict[str, str]]:
        headers = {"accept": "application/json"}
        if key := os.environ.get("RUGCHECK_API_KEY"):
            headers["X-API-KEY"] = key
        return f"{self.base_url}/tokens/{mint}/report/summary", headers

    def normalise(self, payload: Any) -> ScannerScore:
        """score_raw = `score`; score_norm = `score_normalised` (RugCheck's own scale, higher = riskier)."""
        if not isinstance(payload, dict):
            return ScannerScore(None, None)
        raw, norm = payload.get("score"), payload.get("score_normalised")
        risks: list[Any] = payload.get("risks") if isinstance(payload.get("risks"), list) else []  # type: ignore[assignment]
        flags = sorted(str(r.get("name")) for r in risks if isinstance(r, dict) and r.get("name"))
        return ScannerScore(float(raw) if isinstance(raw, int | float) else None,
                            float(norm) if isinstance(norm, int | float) else None, flags)
