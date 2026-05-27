from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import urlencode

import aiohttp

from agent.models import RiskNarrativeReport, TokenCandidate
from config import Settings
from storage.db import try_consume_monthly_api_usage


TRADE_PATH = "/api/v6/dex/market/trades"
MEMEPUMP_TOKEN_LIST_PATH = "/api/v6/dex/market/memepump/tokenList"
SOLANA_CHAIN_INDEX = "501"


def _auth_available(settings: Settings) -> bool:
    return bool(settings.okx_api_key and settings.okx_secret_key and settings.okx_passphrase)


def _enabled(settings: Settings) -> bool:
    return bool(
        settings.okx_wallet_risk_enabled
        and _auth_available(settings)
    )


def _smart_wallet_enabled(settings: Settings, candidate: TokenCandidate) -> bool:
    return bool(
        settings.smart_wallet_enabled
        or (
            settings.smart_wallet_on_migration_enabled
            and candidate.source == "pumpportal_migration"
        )
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _headers(settings: Settings, method: str, request_path_with_query: str) -> dict[str, str]:
    timestamp = _timestamp()
    prehash = f"{timestamp}{method}{request_path_with_query}"
    digest = hmac.new(settings.okx_secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return {
        "OK-ACCESS-KEY": settings.okx_api_key,
        "OK-ACCESS-SIGN": base64.b64encode(digest).decode("utf-8"),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": settings.okx_passphrase,
        "Content-Type": "application/json",
    }


async def _get_token_trades(settings: Settings, mint: str, tag_filter: str | None = None, limit: int = 100) -> list[dict]:
    if not try_consume_monthly_api_usage("okx", settings.okx_monthly_request_limit):
        raise RuntimeError("OKX monthly request limit reached")
    params = {
        "chainIndex": SOLANA_CHAIN_INDEX,
        "tokenContractAddress": mint,
        "limit": str(limit),
    }
    if tag_filter:
        params["tagFilter"] = tag_filter
    query = urlencode(params)
    request_path = f"{TRADE_PATH}?{query}"
    url = f"{settings.okx_base_url}{request_path}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url, headers=_headers(settings, "GET", request_path)) as resp:
            data = await resp.json(content_type=None)
    if str(data.get("code")) != "0":
        raise RuntimeError(str(data.get("msg") or data.get("code") or "OKX API error"))
    payload = data.get("data") or []
    if isinstance(payload, dict):
        payload = payload.get("trades") or []
    return payload if isinstance(payload, list) else []


async def fetch_memepump_token_list(
    settings: Settings,
    stage: str,
    limit: int = 30,
    min_market_cap_usd: int | None = None,
    max_token_age_minutes: int | None = None,
    min_volume_usd: int | None = None,
    min_tx_count: int | None = None,
    min_buy_tx_count: int | None = None,
) -> list[dict]:
    if not _auth_available(settings):
        return []
    if not try_consume_monthly_api_usage("okx", settings.okx_monthly_request_limit):
        raise RuntimeError("OKX monthly request limit reached")
    params = {
        "chainIndex": SOLANA_CHAIN_INDEX,
        "stage": stage,
        "sort": "createdTimestamp",
        "order": "desc",
        "limit": str(min(max(1, limit), 30)),
    }
    if min_market_cap_usd is not None:
        params["minMarketCapUsd"] = str(min_market_cap_usd)
    if max_token_age_minutes is not None:
        params["maxTokenAge"] = str(max_token_age_minutes)
    if min_volume_usd is not None:
        params["minVolumeUsd"] = str(min_volume_usd)
    if min_tx_count is not None:
        params["minTxCount"] = str(min_tx_count)
    if min_buy_tx_count is not None:
        params["minBuyTxCount"] = str(min_buy_tx_count)
    query = urlencode(params)
    request_path = f"{MEMEPUMP_TOKEN_LIST_PATH}?{query}"
    url = f"{settings.okx_base_url}{request_path}"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get(url, headers=_headers(settings, "GET", request_path)) as resp:
            data = await resp.json(content_type=None)
    if str(data.get("code")) != "0":
        raise RuntimeError(str(data.get("msg") or data.get("code") or "OKX API error"))
    payload = data.get("data") or {}
    items = payload.get("items") if isinstance(payload, dict) else []
    return items if isinstance(items, list) else []


def _trade_type(trade: dict) -> str:
    return str(trade.get("type") or trade.get("tradeType") or "").strip().lower()


def _buy_count(trades: list[dict]) -> int:
    return sum(1 for trade in trades if _trade_type(trade) in {"buy", "1"})


def _sell_count(trades: list[dict]) -> int:
    return sum(1 for trade in trades if _trade_type(trade) in {"sell", "2"})


async def enrich_okx_wallet_risk(settings: Settings, candidate: TokenCandidate, risk: RiskNarrativeReport) -> RiskNarrativeReport:
    if not _enabled(settings):
        return risk

    smart_wallet_enabled = _smart_wallet_enabled(settings, candidate)
    try:
        smart_trades = await _get_token_trades(settings, candidate.mint, tag_filter="3", limit=100) if smart_wallet_enabled else []
        sniper_trades = await _get_token_trades(settings, candidate.mint, tag_filter="7", limit=100) if settings.sniper_detection_enabled else []
        bundle_trades = await _get_token_trades(settings, candidate.mint, tag_filter="9", limit=100) if settings.bundle_detection_enabled else []
    except Exception as exc:
        risk.data_gaps.append(f"OKX wallet-risk check failed: {exc.__class__.__name__}.")
        return risk

    if smart_wallet_enabled:
        buys = _buy_count(smart_trades)
        sells = _sell_count(smart_trades)
        if buys and buys >= sells:
            risk.smart_wallet_signal = f"positive ({buys} buy, {sells} sell)"
        elif smart_trades:
            risk.smart_wallet_signal = f"mixed ({buys} buy, {sells} sell)"
        else:
            risk.smart_wallet_signal = "not_seen"

    if settings.sniper_detection_enabled:
        if len(sniper_trades) >= 10:
            risk.sniper_risk = f"high ({len(sniper_trades)} tagged trades)"
        elif sniper_trades:
            risk.sniper_risk = f"medium ({len(sniper_trades)} tagged trades)"
        else:
            risk.sniper_risk = "not_seen"

    if settings.bundle_detection_enabled:
        if len(bundle_trades) >= 5:
            risk.bundle_risk = f"high ({len(bundle_trades)} tagged trades)"
        elif bundle_trades:
            risk.bundle_risk = f"medium ({len(bundle_trades)} tagged trades)"
        else:
            risk.bundle_risk = "not_seen"

    risk.data_gaps = [
        item
        for item in risk.data_gaps
        if not item.startswith("Dev-sold, sniper, bundle")
        and not item.startswith("Current bot-risk is heuristic")
    ]
    if not any(item.startswith("Holder concentration") for item in risk.data_gaps):
        risk.data_gaps.append("Holder concentration still needs Helius/top-holder data.")
    return risk
