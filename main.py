from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict

from agent.models import TokenCandidate
from agent.pumpportal import stream_new_tokens
from agent.research_agent import analyze_token, format_report
from agent.telegram_bot import run_bot
from config import load_settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _passes_alert_filter(result: dict, settings) -> bool:
    score = result["score"]
    dex = result["dex"]
    token = result["token"]
    if not token.mint.lower().endswith("pump"):
        return False
    if score.opportunity_label in {"RISK", "HIGH_RISK", "DEV_SOLD_RISK", "NO_LIQUIDITY", "OVER_RANGE"}:
        return False
    if score.opportunity_score < settings.min_alert_score:
        return False
    if dex.market_cap is None or dex.market_cap < settings.min_market_cap_usd:
        return False
    if dex.market_cap > settings.max_market_cap_usd:
        return False
    if dex.dex_id and dex.dex_id.lower() not in {"pumpswap", "pumpfun"}:
        return False
    return True


def _json_default(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


async def token_command(args: argparse.Namespace) -> None:
    settings = load_settings()
    result = await analyze_token(TokenCandidate(mint=args.mint, symbol=args.symbol or "", name=args.name or "", source="cli"), settings)
    if args.json:
        print(json.dumps(result, default=_json_default, indent=2, ensure_ascii=False))
    else:
        print(format_report(result))


async def scan_command(args: argparse.Namespace) -> None:
    settings = load_settings()
    async for candidate in stream_new_tokens(settings, limit=args.limit or settings.scan_limit, timeout_seconds=args.timeout or settings.scan_timeout_seconds):
        if not candidate.mint.lower().endswith("pump"):
            continue
        result = await analyze_token(candidate, settings)
        if args.all or _passes_alert_filter(result, settings):
            print(format_report(result))
            print("-" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Research-only Solana meme token agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    token = sub.add_parser("token", help="Analyze a known token mint.")
    token.add_argument("mint")
    token.add_argument("--symbol", default="")
    token.add_argument("--name", default="")
    token.add_argument("--json", action="store_true")

    scan = sub.add_parser("scan", help="Scan new PumpPortal token launches.")
    scan.add_argument("--limit", type=int)
    scan.add_argument("--timeout", type=int)
    scan.add_argument("--all", action="store_true", help="Print all scanned tokens, not only alerts.")

    sub.add_parser("bot", help="Run Telegram bot.")
    args = parser.parse_args()

    if args.command == "token":
        asyncio.run(token_command(args))
    elif args.command == "scan":
        asyncio.run(scan_command(args))
    elif args.command == "bot":
        run_bot(load_settings())


if __name__ == "__main__":
    main()
