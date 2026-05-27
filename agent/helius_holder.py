from __future__ import annotations

from typing import Any

import aiohttp

from agent.models import RiskNarrativeReport, TokenCandidate
from config import Settings


HELIUS_RPC_BASE = "https://mainnet.helius-rpc.com/"


def _enabled(settings: Settings) -> bool:
    return bool(settings.wallet_risk_enabled and settings.helius_api_key)


async def _rpc(settings: Settings, method: str, params: list[Any]) -> Any:
    url = f"{HELIUS_RPC_BASE}?api-key={settings.helius_api_key}"
    payload = {"jsonrpc": "2.0", "id": "newlite", "method": method, "params": params}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
    if "error" in data:
        message = data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
        raise RuntimeError(str(message))
    return data.get("result")


def _amount(account: dict[str, Any]) -> float:
    try:
        return float(account.get("uiAmount") or 0)
    except (TypeError, ValueError):
        return 0.0


async def enrich_helius_holder_concentration(
    settings: Settings,
    candidate: TokenCandidate,
    risk: RiskNarrativeReport,
) -> RiskNarrativeReport:
    if not _enabled(settings):
        return risk

    try:
        supply_result = await _rpc(settings, "getTokenSupply", [candidate.mint])
        largest_result = await _rpc(settings, "getTokenLargestAccounts", [candidate.mint])
    except Exception as exc:
        risk.data_gaps.append(f"Helius holder concentration check failed: {exc.__class__.__name__}.")
        return risk

    try:
        supply = float((supply_result or {}).get("value", {}).get("uiAmount") or 0)
        accounts = (largest_result or {}).get("value") or []
    except (TypeError, ValueError, AttributeError):
        risk.data_gaps.append("Helius holder concentration response was not usable.")
        return risk

    if supply <= 0 or not accounts:
        risk.data_gaps.append("Helius holder concentration returned no supply/accounts.")
        return risk

    amounts = [_amount(account) for account in accounts]
    top1 = (amounts[0] / supply) * 100 if amounts else 0
    top5 = (sum(amounts[:5]) / supply) * 100
    top10 = (sum(amounts[:10]) / supply) * 100

    if top1 >= 35 or top10 >= 70:
        signal = "high"
    elif top1 >= 20 or top10 >= 50:
        signal = "medium"
    else:
        signal = "low"

    risk.holder_concentration_signal = signal
    risk.holder_concentration_notes.append(
        f"Helius top token accounts: top1 {top1:.1f}%, top5 {top5:.1f}%, top10 {top10:.1f}%."
    )
    risk.data_gaps = [
        item
        for item in risk.data_gaps
        if not item.startswith("Holder concentration")
        and "holder concentration" not in item.lower()
    ]
    return risk
