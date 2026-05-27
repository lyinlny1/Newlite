from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import websockets

from agent.models import RiskNarrativeReport, TokenCandidate
from config import Settings


def _lower(value: object) -> str:
    return str(value or "").strip().lower()


def _is_sell(event: dict) -> bool:
    tx_type = _lower(event.get("txType") or event.get("type") or event.get("side") or event.get("action"))
    return tx_type == "sell" or "sell" in tx_type


def _trader(event: dict) -> str:
    for key in ("traderPublicKey", "trader", "wallet", "owner", "user", "publicKey"):
        value = event.get(key)
        if value:
            return str(value)
    return ""


async def check_dev_sold(settings: Settings, candidate: TokenCandidate, risk: RiskNarrativeReport) -> RiskNarrativeReport:
    if risk.dev_sold_signal in {"sold_seen", "not_seen"}:
        return risk
    if not settings.dev_sold_check_enabled:
        risk.dev_sold_notes.append("Dev-sold live check disabled. Set DEV_SOLD_CHECK_ENABLED=true to enable.")
        return risk
    if not settings.pumpportal_api_key:
        risk.dev_sold_signal = "not_available"
        risk.dev_sold_notes.append("PumpPortal API key is required for token trade stream.")
        return risk
    if not risk.dev_wallet:
        risk.dev_sold_signal = "unknown_dev_wallet"
        risk.dev_sold_notes.append("Cannot compare sells because dev wallet is unknown.")
        return risk

    params = urlencode({"api-key": settings.pumpportal_api_key})
    url = settings.pumpportal_ws_url + (f"?{params}" if params else "")
    dev = risk.dev_wallet
    dev_trade_count = 0

    try:
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [candidate.mint]}))
            end_at = asyncio.get_running_loop().time() + max(1, settings.dev_sold_check_seconds)
            while asyncio.get_running_loop().time() < end_at:
                remaining = max(0.1, end_at - asyncio.get_running_loop().time())
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue
                trader = _trader(event)
                if trader != dev:
                    continue
                dev_trade_count += 1
                if _is_sell(event):
                    sol_amount = event.get("solAmount") or event.get("amountSol") or event.get("amount")
                    suffix = f" Amount: {sol_amount}." if sol_amount is not None else ""
                    risk.dev_sold_signal = "sold_seen"
                    risk.dev_sold_notes.append(f"Dev wallet sell detected in live PumpPortal stream.{suffix}")
                    return risk
    except Exception as exc:
        risk.dev_sold_signal = "check_failed"
        risk.dev_sold_notes.append(f"Dev-sold stream check failed: {exc.__class__.__name__}.")
        return risk

    risk.dev_sold_signal = "not_seen"
    if dev_trade_count:
        risk.dev_sold_notes.append(f"Dev wallet had {dev_trade_count} live trade(s), but no sell was seen.")
    else:
        risk.dev_sold_notes.append(f"No dev-wallet sell seen during {settings.dev_sold_check_seconds}s live check.")
    return risk
