"""GoPlus: GET /api/v1/solana/token_security. No single score; score_norm = count of risk flags."""

from __future__ import annotations

import os
from typing import Any

from rugwatch.collector.scanners import ScannerScore

# Authority-style fields: {"status": "1"} means the risky capability is present.
STATUS_FLAGS = (
    "mintable", "freezable", "closable", "balance_mutable_authority", "metadata_mutable",
    "transfer_fee_upgradable", "transfer_hook_upgradable", "default_account_state_upgradable",
)


class GoPlus:
    name = "goplus"
    base_url = "https://api.gopluslabs.io/api/v1"

    def request(self, mint: str) -> tuple[str, dict[str, str]]:
        headers = {"accept": "application/json"}
        # An access token (not the app key) goes in Authorization; without one the public tier is used.
        if token := os.environ.get("GOPLUS_ACCESS_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        return f"{self.base_url}/solana/token_security?contract_addresses={mint}", headers

    def normalise(self, payload: Any) -> ScannerScore:
        """Flags triggered, sorted; score_raw = score_norm = their count. None if the token is absent."""
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or not result:
            return ScannerScore(None, None)
        token = next(iter(result.values()))
        if not isinstance(token, dict):
            return ScannerScore(None, None)
        flags: list[str] = []
        for name in STATUS_FLAGS:
            item = token.get(name)
            if isinstance(item, dict) and str(item.get("status")) == "1":
                flags.append(name)
        fee = token.get("transfer_fee")
        if isinstance(fee, dict) and (fee.get("current_fee_rate") or fee.get("scheduled_fee_rate")):
            flags.append("transfer_fee")
        if token.get("transfer_hook"):
            flags.append("transfer_hook")
        if str(token.get("none_transferable")) == "1":
            flags.append("none_transferable")
        if str(token.get("default_account_state")) == "2":
            flags.append("default_account_frozen")
        creators: list[Any] = token.get("creators") if isinstance(token.get("creators"), list) else []  # type: ignore[assignment]
        if any(isinstance(c, dict) and str(c.get("malicious_address")) == "1" for c in creators):
            flags.append("malicious_creator")
        flags.sort()
        return ScannerScore(float(len(flags)), float(len(flags)), flags)
