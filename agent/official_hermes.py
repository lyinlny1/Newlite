from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from agent.models import DexProfile, ResearchReport, RiskNarrativeReport, ScoreReport, TokenCandidate
from config import Settings


def official_hermes_available(settings: Settings) -> bool:
    command = settings.hermes_cli_command
    return bool(settings.hermes_official_enabled and (shutil.which(command) or Path(command).exists()))


def _context(candidate: TokenCandidate, dex: DexProfile, research: ResearchReport, score: ScoreReport, risk: RiskNarrativeReport | None = None) -> dict:
    return {
        "mission": "Assess a Solana pump.fun memecoin for high-upside watchlist potential. Never give buy advice.",
        "token": {
            "mint": candidate.mint,
            "symbol": candidate.symbol or dex.token_symbol,
            "name": candidate.name or dex.token_name,
        },
        "scores": {
            "opportunity_score": score.opportunity_score,
            "opportunity_label": score.opportunity_label,
            "trust_score": score.score,
            "opportunity_reasons": score.opportunity_reasons,
            "risks": score.risks,
        },
        "market": {
            "dex_url": dex.url,
            "market_cap": dex.market_cap,
            "liquidity_usd": dex.liquidity_usd,
            "volume_5m": dex.volume_5m,
            "volume_1h": dex.volume_1h,
            "txns_5m": dex.txns_5m,
            "txns_1h": dex.txns_1h,
        },
        "identity": {
            "websites": dex.websites,
            "socials": dex.socials,
            "image_url": dex.image_url,
            "header_url": dex.header_url,
            "description": dex.description,
            "free_web_interest": research.estimated_interest,
            "free_web_notes": research.notes,
        },
        "risk_narrative": {
            "narrative": risk.narrative,
            "narrative_notes": risk.narrative_notes,
            "bot_risk": risk.bot_risk,
            "bot_notes": risk.bot_notes,
            "dev_wallet": risk.dev_wallet,
            "dev_sold_signal": risk.dev_sold_signal,
            "dev_sold_notes": risk.dev_sold_notes,
            "sniper_risk": risk.sniper_risk,
            "bundle_risk": risk.bundle_risk,
            "smart_wallet_signal": risk.smart_wallet_signal,
            "holder_concentration_signal": risk.holder_concentration_signal,
            "holder_concentration_notes": risk.holder_concentration_notes,
            "data_gaps": risk.data_gaps,
        } if risk else {},
        "free_search_results": [
            {"title": item.title, "url": item.url, "snippet": item.snippet}
            for item in research.results[:5]
        ],
    }


async def run_official_hermes(
    settings: Settings,
    candidate: TokenCandidate,
    dex: DexProfile,
    research: ResearchReport,
    score: ScoreReport,
    risk: RiskNarrativeReport | None = None,
) -> ScoreReport:
    if not settings.hermes_official_enabled:
        return score
    if not (shutil.which(settings.hermes_cli_command) or Path(settings.hermes_cli_command).exists()):
        return score

    prompt = (
        "You are official NousResearch Hermes Agent being called by Newlite Research.\n"
        "Analyze this token research JSON and return plain Indonesian text only, exactly four short lines:\n"
        "Tesis: one sentence.\n"
        "Peluang: strongest evidence for possible upside.\n"
        "Risiko: biggest reasons to avoid or wait.\n"
        "Aksi: one of skip, observe, watch, recheck. Never say buy.\n\n"
        + json.dumps(_context(candidate, dex, research, score, risk), ensure_ascii=False)
    )

    cmd = [settings.hermes_cli_command, "chat", "-q", prompt, "-Q", "--source", "newlite-research"]
    if settings.hermes_cli_provider:
        cmd.extend(["--provider", settings.hermes_cli_provider])
    if settings.hermes_cli_model:
        cmd.extend(["--model", settings.hermes_cli_model])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=settings.hermes_cli_timeout_seconds)
    except asyncio.TimeoutError:
        return score
    except OSError as exc:
        return score

    out = stdout.decode("utf-8", errors="ignore").strip()
    err = stderr.decode("utf-8", errors="ignore").strip()
    if proc.returncode != 0:
        return score
    if not out:
        return score

    score.ai_summary = out
    score.ai_provider = "official-hermes"
    return score
