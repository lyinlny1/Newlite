from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from urllib.parse import urlencode

import websockets
from websockets.exceptions import ConnectionClosed

from config import Settings
from agent.models import TokenCandidate

PING_INTERVAL_SECONDS = 30
PING_TIMEOUT_SECONDS = 60
RECONNECT_DELAY_SECONDS = 5


def _candidate_from_event(event: dict) -> TokenCandidate | None:
    mint = event.get("mint") or event.get("tokenAddress") or event.get("address")
    if not mint:
        return None
    return TokenCandidate(
        mint=str(mint),
        name=str(event.get("name") or event.get("tokenName") or ""),
        symbol=str(event.get("symbol") or event.get("ticker") or ""),
        source="pumpportal",
        raw=event,
    )


async def stream_new_tokens(
    settings: Settings,
    limit: int,
    timeout_seconds: int,
    consume_full_timeout: bool = False,
) -> AsyncIterator[TokenCandidate]:
    params = urlencode({"api-key": settings.pumpportal_api_key}) if settings.pumpportal_api_key else ""
    url = settings.pumpportal_ws_url + (f"?{params}" if params else "")
    seen: set[str] = set()
    yielded = 0
    end_at = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining_window = end_at - asyncio.get_running_loop().time()
        if remaining_window <= 0:
            break
        try:
            async with websockets.connect(
                url,
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
                open_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                while True:
                    remaining = max(0.1, end_at - asyncio.get_running_loop().time())
                    if remaining <= 0.1:
                        return
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    candidate = _candidate_from_event(event)
                    if not candidate or candidate.mint in seen:
                        continue
                    seen.add(candidate.mint)
                    if yielded >= limit:
                        if consume_full_timeout:
                            continue
                        return
                    yielded += 1
                    yield candidate
        except (ConnectionClosed, OSError, TimeoutError):
            if asyncio.get_running_loop().time() + RECONNECT_DELAY_SECONDS >= end_at:
                break
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def stream_new_tokens_live(settings: Settings) -> AsyncIterator[TokenCandidate]:
    params = urlencode({"api-key": settings.pumpportal_api_key}) if settings.pumpportal_api_key else ""
    url = settings.pumpportal_ws_url + (f"?{params}" if params else "")
    seen: set[str] = set()
    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
                open_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                while True:
                    message = await ws.recv()
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    candidate = _candidate_from_event(event)
                    if not candidate or candidate.mint in seen:
                        continue
                    seen.add(candidate.mint)
                    yield candidate
        except (ConnectionClosed, OSError, TimeoutError):
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


async def stream_migrations(settings: Settings) -> AsyncIterator[TokenCandidate]:
    params = urlencode({"api-key": settings.pumpportal_api_key}) if settings.pumpportal_api_key else ""
    url = settings.pumpportal_ws_url + (f"?{params}" if params else "")
    seen: set[str] = set()
    while True:
        try:
            async with websockets.connect(
                url,
                ping_interval=PING_INTERVAL_SECONDS,
                ping_timeout=PING_TIMEOUT_SECONDS,
                open_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                while True:
                    message = await ws.recv()
                    try:
                        event = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    candidate = _candidate_from_event(event)
                    if not candidate or candidate.mint in seen:
                        continue
                    seen.add(candidate.mint)
                    candidate.source = "pumpportal_migration"
                    yield candidate
        except (ConnectionClosed, OSError, TimeoutError):
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)
