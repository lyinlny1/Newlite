from __future__ import annotations

from html import escape
import re
import time

from agent.dexscreener import fetch_token_profile
from agent.dev_sold import check_dev_sold
from agent.free_research import research_token
from agent.helius_holder import enrich_helius_holder_concentration
from agent.newlite_agent import run_newlite_opportunity, should_run_newlite_agent
from agent.okx_web3 import enrich_okx_wallet_risk
from agent.official_hermes import official_hermes_available, run_official_hermes
from agent.models import TokenCandidate
from agent.risk_narrative import analyze_risk_narrative
from agent.scoring import opportunity_label_for, rule_score
from config import Settings
from storage.db import remember_analysis
from storage.learning import adjustment_for_result


def _plain_text(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.strip()


async def analyze_token(candidate: TokenCandidate, settings: Settings, force_reasoning: bool = False, enable_reasoning: bool = True) -> dict:
    dex = await fetch_token_profile(candidate.mint)
    research = await research_token(candidate, dex.websites, settings.web_search_max_results) if settings.enable_free_web_research else None
    if research is None:
        from agent.models import ResearchReport

        research = ResearchReport(notes=["Free web research disabled."])
    risk = analyze_risk_narrative(candidate, dex)
    risk = await enrich_okx_wallet_risk(settings, candidate, risk)
    risk = await enrich_helius_holder_concentration(settings, candidate, risk)
    risk = await check_dev_sold(settings, candidate, risk)
    score = rule_score(
        candidate,
        dex,
        research,
        risk,
        min_market_cap_usd=settings.min_market_cap_usd,
        max_market_cap_usd=settings.max_market_cap_usd,
    )
    result = {
        "token": candidate,
        "dex": dex,
        "research": research,
        "risk": risk,
        "score": score,
    }
    learned_delta = 0
    if settings.auto_learn_enabled:
        delta, notes = adjustment_for_result(
            result,
            min_samples=settings.learn_min_samples,
            max_adjustment=settings.learn_max_adjustment,
        )
        learned_delta = delta
        if delta:
            score.opportunity_score = max(0, min(100, score.opportunity_score + delta))
            score.opportunity_label = opportunity_label_for(
                score.opportunity_score,
                dex=dex,
                risk=risk,
                max_market_cap_usd=settings.max_market_cap_usd,
            )
            score.opportunity_reasons = [*score.opportunity_reasons, *notes][:8]
            result["score"] = score
    learned_reasoning_boost = learned_delta > 0 and score.opportunity_score >= max(0, settings.newlite_agent_min_opportunity_score - 10)
    should_reason = enable_reasoning and (
        should_run_newlite_agent(settings, result, force=force_reasoning)
        or (settings.newlite_agent_enabled and learned_reasoning_boost)
    )
    if official_hermes_available(settings) and should_reason:
        score = await run_official_hermes(settings, candidate, dex, research, score, risk)
        result["score"] = score
        if score.ai_summary:
            remember_analysis(result)
            return result
    if should_reason:
        score = await run_newlite_opportunity(settings, candidate, dex, research, score, risk)
        result["score"] = score
    remember_analysis(result)
    return result


def format_report(result: dict) -> str:
    token = result["token"]
    dex = result["dex"]
    research = result["research"]
    risk = result.get("risk")
    score = result["score"]
    symbol = token.symbol or dex.token_symbol
    name = token.name or dex.token_name
    identity = f"${symbol}" if symbol else name or token.mint[:8]
    lines = [
        f"{identity} | {score.opportunity_label} | Opp {score.opportunity_score}/100 | Trust {score.score}/100",
        f"Mint: {token.mint}",
    ]
    if dex.url:
        lines.append(f"DEX: {dex.url}")
    if name and (not symbol or name.lower() != symbol.lower()):
        lines.append(f"Name: {name}")
    if dex.image_url:
        lines.append(f"Image: {dex.image_url}")
    market_line = []
    if dex.market_cap is not None:
        market_line.append(f"MC: ${dex.market_cap:,.0f}")
    if dex.liquidity_usd and dex.liquidity_usd > 0:
        market_line.append(f"Liq: ${dex.liquidity_usd:,.0f}")
    if dex.volume_5m is not None:
        market_line.append(f"Vol 5m: ${dex.volume_5m:,.0f}")
    if dex.txns_5m:
        market_line.append(f"Tx 5m: {dex.txns_5m}")
    if market_line:
        lines.append(" | ".join(market_line))
    if dex.dex_id:
        lines.append(f"DEX ID: {dex.dex_id}")
    if risk:
        lines.append(f"Narrative: {risk.narrative} | Bot risk: {risk.bot_risk}")
        lines.append(f"Dev sold: {risk.dev_sold_signal}" + (f" | Dev: {risk.dev_wallet}" if risk.dev_wallet else ""))
        lines.append(f"Sniper: {risk.sniper_risk} | Bundle: {risk.bundle_risk} | Smart wallet: {risk.smart_wallet_signal}")
    if dex.websites:
        lines.append("Website: " + ", ".join(dex.websites[:2]))
    if dex.socials:
        lines.append("Social: " + ", ".join(dex.socials[:3]))
    lines.append(f"Free web interest: {research.estimated_interest}")
    if score.opportunity_reasons:
        lines.append("Opportunity: " + "; ".join(score.opportunity_reasons[:4]))
    if score.reasons:
        lines.append("Signals: " + "; ".join(score.reasons[:4]))
    if score.risks:
        lines.append("Risks: " + "; ".join(score.risks[:4]))
    if score.ai_summary:
        lines.append(f"Reasoning ({score.ai_provider}):\n{_plain_text(score.ai_summary)}")
    return "\n".join(lines)[:3900]


def _short_list(items: list[str], limit: int = 4) -> str:
    return "\n".join(f"• {escape(item)}" for item in items[:limit])


def _token_age(pair_created_at: int | None) -> str:
    if not pair_created_at:
        return ""
    ts = pair_created_at / 1000 if pair_created_at > 10_000_000_000 else pair_created_at
    seconds = max(0, int(time.time() - ts))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def _alert_reason(result: dict) -> str:
    score = result["score"]
    bits = list(score.opportunity_reasons[:2])
    if score.risks:
        bits.append("risk: " + score.risks[0])
    return "; ".join(bits)


def format_report_html(result: dict, title: str | None = None) -> str:
    token = result["token"]
    dex = result["dex"]
    research = result["research"]
    risk = result.get("risk")
    score = result["score"]
    symbol = token.symbol or dex.token_symbol
    name = token.name or dex.token_name
    identity = f"${symbol}" if symbol else name or token.mint[:8]
    label_emoji = {
        "MOMENTUM_WATCH": "🚀",
        "WATCHLIST": "🟢",
        "EARLY_RADAR": "🟡",
        "OVER_RANGE": "🟣",
        "HIGH_RISK": "🔴",
        "DEV_SOLD_RISK": "🔴",
        "NO_LIQUIDITY": "⚫",
        "RISK": "🔴",
    }.get(score.opportunity_label, "🔎")

    header = title or f"{label_emoji} <b>{escape(identity)}</b> | <b>{escape(score.opportunity_label)}</b>"
    lines = [
        header,
        f"📊 <b>Opp:</b> {score.opportunity_score}/100 | <b>Trust:</b> {score.score}/100",
        f"🧾 <b>Mint:</b> <code>{escape(token.mint)}</code>",
    ]
    if dex.url:
        lines.append(f"🔗 <b>DEX:</b> {escape(dex.url)}")
    if name and (not symbol or name.lower() != symbol.lower()):
        lines.append(f"🏷️ <b>Name:</b> {escape(name)}")
    age = _token_age(dex.pair_created_at)
    if age:
        lines.append(f"⏳ <b>Age:</b> {escape(age)}")

    market = []
    if dex.market_cap is not None:
        market.append(f"MC ${dex.market_cap:,.0f}")
    if dex.liquidity_usd and dex.liquidity_usd > 0:
        market.append(f"Liq ${dex.liquidity_usd:,.0f}")
    if dex.volume_5m is not None:
        market.append(f"Vol 5m ${dex.volume_5m:,.0f}")
    if dex.txns_5m:
        market.append(f"Tx 5m {dex.txns_5m}")
    if market:
        lines.append(f"💧 <b>Market:</b> {escape(' | '.join(market))}")
    if dex.dex_id:
        lines.append(f"🏦 <b>DEX:</b> {escape(dex.dex_id)}")
    if risk:
        lines.append(f"🧬 <b>Narrative:</b> {escape(risk.narrative)}")
        dev_wallet = f" | <b>Dev:</b> <code>{escape(risk.dev_wallet[:8])}...</code>" if risk.dev_wallet else ""
        lines.append(f"👨‍💻 <b>Dev sold:</b> {escape(risk.dev_sold_signal)}{dev_wallet}")
        lines.append(
            f"🤖 <b>Bot:</b> {escape(risk.bot_risk)} | "
            f"🎯 <b>Sniper:</b> {escape(risk.sniper_risk)} | "
            f"📦 <b>Bundle:</b> {escape(risk.bundle_risk)} | "
            f"🧠 <b>Smart wallet:</b> {escape(risk.smart_wallet_signal)}"
        )
        lines.append(f"👥 <b>Holders:</b> {escape(risk.holder_concentration_signal)}")
        if risk.holder_concentration_notes:
            lines.append("   " + escape(risk.holder_concentration_notes[0]))
    if dex.socials:
        lines.append("🐦 <b>Social:</b> " + escape(", ".join(dex.socials[:3])))
    if dex.websites:
        lines.append("🌐 <b>Website:</b> " + escape(", ".join(dex.websites[:2])))
    if dex.image_url:
        lines.append(f"🖼️ <b>Image:</b> {escape(dex.image_url)}")
    lines.append(f"🔎 <b>Free web:</b> {escape(research.estimated_interest)}")
    why = _alert_reason(result)
    if why:
        lines.append(f"🧭 <b>Why:</b> {escape(why)}")

    if score.opportunity_reasons:
        lines.append("\n✅ <b>Opportunity</b>\n" + _short_list(score.opportunity_reasons))
    if score.risks:
        visible_risks = [
            risk for risk in score.risks
            if "Official Hermes" not in risk and "Authentication" not in risk and "401" not in risk
        ]
        if visible_risks:
            lines.append("\n⚠️ <b>Risks</b>\n" + _short_list(visible_risks))
    if risk and risk.data_gaps:
        lines.append("\n📌 <b>Data gaps</b>\n" + _short_list(risk.data_gaps, limit=2))
    if score.ai_summary:
        lines.append(f"\n🧠 <b>Reasoning ({escape(score.ai_provider)})</b>\n{escape(_plain_text(score.ai_summary))}")
    return "\n".join(lines)[:3900]
