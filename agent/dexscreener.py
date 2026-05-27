from __future__ import annotations

from typing import Any

import aiohttp

from agent.models import DexProfile


DEX_BASE = "https://api.dexscreener.com"
LATEST_PROFILES_URL = f"{DEX_BASE}/token-profiles/latest/v1"


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_txns(txns: dict[str, Any] | None, window: str) -> int:
    data = (txns or {}).get(window) or {}
    return int(data.get("buys") or 0) + int(data.get("sells") or 0)


def _extract_links(info: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    websites: list[str] = []
    socials: list[str] = []
    if not info:
        return websites, socials
    for item in info.get("websites") or []:
        url = item.get("url")
        if url:
            websites.append(url)
    for item in info.get("socials") or []:
        url = item.get("url")
        if url:
            socials.append(url)
    return websites, socials


def _best_pair(pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
    solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    candidates = solana_pairs or pairs
    if not candidates:
        return None
    return max(candidates, key=lambda p: _float((p.get("liquidity") or {}).get("usd")) or 0)


async def fetch_token_profile(mint: str, timeout_seconds: int = 20) -> DexProfile:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    url = f"{DEX_BASE}/tokens/v1/solana/{mint}"
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status == 404:
                return DexProfile(found=False)
            if resp.status >= 400:
                text = await resp.text()
                return DexProfile(found=False, description=f"DEX Screener HTTP {resp.status}: {text[:160]}")
            data = await resp.json()

    pairs = data if isinstance(data, list) else []
    pair = _best_pair(pairs)
    if not pair:
        return DexProfile(found=False, raw={"response": data})

    info = pair.get("info") or {}
    websites, socials = _extract_links(info)
    volume = pair.get("volume") or {}
    liquidity = pair.get("liquidity") or {}
    boosts = pair.get("boosts") or {}
    base_token = pair.get("baseToken") or {}
    return DexProfile(
        found=True,
        url=pair.get("url", ""),
        token_name=base_token.get("name") or "",
        token_symbol=base_token.get("symbol") or "",
        pair_address=pair.get("pairAddress", ""),
        dex_id=pair.get("dexId") or "",
        price_usd=pair.get("priceUsd") or "",
        market_cap=_float(pair.get("marketCap")),
        fdv=_float(pair.get("fdv")),
        liquidity_usd=_float(liquidity.get("usd")),
        volume_5m=_float(volume.get("m5")),
        volume_1h=_float(volume.get("h1")),
        txns_5m=_sum_txns(pair.get("txns"), "m5"),
        txns_1h=_sum_txns(pair.get("txns"), "h1"),
        pair_created_at=pair.get("pairCreatedAt"),
        websites=websites,
        socials=socials,
        image_url=info.get("imageUrl") or "",
        header_url=info.get("header") or "",
        description=info.get("description") or "",
        boost_amount=_float(boosts.get("active")),
        raw=pair,
    )


async def fetch_latest_solana_profiles(limit: int = 100, timeout_seconds: int = 20) -> list[dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(LATEST_PROFILES_URL) as resp:
            if resp.status >= 400:
                return []
            data = await resp.json(content_type=None)
    profiles = data if isinstance(data, list) else []
    solana_profiles = [
        item
        for item in profiles
        if isinstance(item, dict)
        and item.get("chainId") == "solana"
        and item.get("tokenAddress")
    ]
    return solana_profiles[:limit]
