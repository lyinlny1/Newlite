from __future__ import annotations

import re

from agent.models import DexProfile, ResearchReport, RiskNarrativeReport, ScoreReport, TokenCandidate


def _x_socials(dex: DexProfile) -> list[str]:
    return [url for url in dex.socials if "x.com" in url.lower() or "twitter.com" in url.lower()]


def _label(score: int) -> str:
    if score >= 80:
        return "HIGH_POTENTIAL"
    if score >= 60:
        return "WATCHLIST"
    if score >= 40:
        return "LOW_CONFIDENCE"
    return "SKIP"


def _opportunity_label(score: int, dex: DexProfile | None = None, risk: RiskNarrativeReport | None = None, max_market_cap_usd: int | None = None) -> str:
    if dex and max_market_cap_usd and dex.market_cap and dex.market_cap > max_market_cap_usd:
        return "OVER_RANGE"
    if risk and risk.dev_sold_signal == "sold_seen":
        return "DEV_SOLD_RISK"
    if risk and risk.bundle_risk.startswith("high") and risk.sniper_risk.startswith("high"):
        return "HIGH_RISK"
    if dex and dex.found and (dex.liquidity_usd is None or dex.liquidity_usd <= 0):
        return "NO_LIQUIDITY"
    if score >= 60:
        return "MOMENTUM_WATCH"
    if score >= 45:
        return "WATCHLIST"
    if score >= 25:
        return "EARLY_RADAR"
    return "RISK"


def opportunity_label_for(
    score: int,
    dex: DexProfile | None = None,
    risk: RiskNarrativeReport | None = None,
    max_market_cap_usd: int | None = None,
) -> str:
    return _opportunity_label(score, dex=dex, risk=risk, max_market_cap_usd=max_market_cap_usd)


def _dedupe(items: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _tagged_trade_count(value: str) -> int:
    match = re.search(r"\((\d+)\s+tagged trades?\)", value)
    return int(match.group(1)) if match else 0


def _sniper_count_penalty(count: int) -> int:
    if count >= 50:
        return 10
    if count >= 25:
        return 7
    if count >= 10:
        return 4
    if count > 0:
        return 2
    return 0


def _bundle_count_penalty(count: int) -> int:
    if count >= 75:
        return 14
    if count >= 30:
        return 10
    if count >= 10:
        return 6
    if count > 0:
        return 3
    return 0


def _risk_with_count(kind: str, value: str, count: int) -> str:
    if count:
        level = value.split(" ", 1)[0] if value else "unknown"
        return f"{kind} risk {level}: {count} tagged trades."
    return f"{kind} risk: {value}."


def opportunity_breakdown(
    dex: DexProfile,
    research: ResearchReport,
    risk: RiskNarrativeReport | None = None,
    min_market_cap_usd: int = 25_000,
    max_market_cap_usd: int = 200_000,
) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    x_links = _x_socials(dex)

    if dex.market_cap:
        early_midpoint = min_market_cap_usd + ((max_market_cap_usd - min_market_cap_usd) * 0.2)
        if dex.market_cap < min_market_cap_usd:
            items.append((0, f"MC below alert range: ${dex.market_cap:,.0f}"))
        elif dex.market_cap <= early_midpoint:
            items.append((18, f"MC early-small: ${dex.market_cap:,.0f}"))
        elif dex.market_cap <= max_market_cap_usd:
            items.append((14, f"MC early: ${dex.market_cap:,.0f}"))
        else:
            items.append((0, f"MC over range: ${dex.market_cap:,.0f}"))

    if dex.liquidity_usd:
        if 10_000 <= dex.liquidity_usd <= 300_000:
            items.append((16, f"Liquidity healthy: ${dex.liquidity_usd:,.0f}"))
        elif dex.liquidity_usd > 300_000:
            items.append((9, f"Liquidity large: ${dex.liquidity_usd:,.0f}"))
        elif dex.liquidity_usd >= 3_000:
            items.append((6, f"Liquidity thin but present: ${dex.liquidity_usd:,.0f}"))

    if dex.volume_5m and dex.liquidity_usd:
        ratio = dex.volume_5m / max(dex.liquidity_usd, 1)
        if ratio >= 0.2:
            items.append((18, "5m volume/liquidity very strong"))
        elif ratio >= 0.05:
            items.append((12, "5m volume/liquidity active"))
        elif dex.volume_5m >= 1_000:
            items.append((6, "5m volume active"))
    elif dex.volume_5m and dex.volume_5m >= 1_000:
        items.append((6, "5m volume active"))

    if dex.txns_5m >= 60:
        items.append((14, f"5m tx high: {dex.txns_5m}"))
    elif dex.txns_5m >= 25:
        items.append((10, f"5m tx active: {dex.txns_5m}"))
    elif dex.txns_5m >= 10:
        items.append((5, f"5m tx moving: {dex.txns_5m}"))

    if x_links:
        items.append((16, "X/Twitter present"))
    if dex.image_url or dex.header_url:
        items.append((8, "Image/header present"))
    if research.estimated_interest == "high":
        items.append((10, "Free web interest high"))
    elif research.estimated_interest == "medium":
        items.append((6, "Free web interest medium"))

    if risk:
        if risk.narrative != "unknown":
            items.append((6, f"Narrative: {risk.narrative}"))
        if risk.dev_sold_signal == "sold_seen":
            items.append((-20, "Dev sold seen"))
        elif risk.dev_sold_signal == "check_failed":
            items.append((0, "Dev-sold check failed"))
        if risk.smart_wallet_signal.startswith("positive"):
            items.append((8, f"Smart wallet: {risk.smart_wallet_signal}"))
        elif risk.smart_wallet_signal.startswith("mixed"):
            items.append((3, f"Smart wallet: {risk.smart_wallet_signal}"))
        if risk.sniper_risk.startswith("high"):
            items.append((-12, f"Sniper risk: {risk.sniper_risk}"))
        elif risk.sniper_risk.startswith("medium"):
            items.append((-6, f"Sniper risk: {risk.sniper_risk}"))
        sniper_count = _tagged_trade_count(risk.sniper_risk)
        sniper_count_penalty = _sniper_count_penalty(sniper_count)
        if sniper_count_penalty:
            items.append((-sniper_count_penalty, f"Sniper count penalty: {sniper_count} tagged trades"))
        if risk.bundle_risk.startswith("high"):
            items.append((-14, f"Bundle risk: {risk.bundle_risk}"))
        elif risk.bundle_risk.startswith("medium"):
            items.append((-7, f"Bundle risk: {risk.bundle_risk}"))
        bundle_count = _tagged_trade_count(risk.bundle_risk)
        bundle_count_penalty = _bundle_count_penalty(bundle_count)
        if bundle_count_penalty:
            items.append((-bundle_count_penalty, f"Bundle count penalty: {bundle_count} tagged trades"))
        if risk.holder_concentration_signal == "high":
            items.append((-12, "Holder concentration high"))
        elif risk.holder_concentration_signal == "medium":
            items.append((-6, "Holder concentration medium"))
        if risk.bot_risk == "high":
            items.append((-15, "Bot-risk high"))
        elif risk.bot_risk == "medium":
            items.append((-7, "Bot-risk medium"))
    return items


def _opportunity_score(
    dex: DexProfile,
    research: ResearchReport,
    risk: RiskNarrativeReport | None = None,
    min_market_cap_usd: int = 25_000,
    max_market_cap_usd: int = 200_000,
) -> tuple[int, list[str], list[str]]:
    score = 0
    reasons: list[str] = []
    risks: list[str] = []
    x_links = _x_socials(dex)

    if dex.market_cap:
        early_midpoint = min_market_cap_usd + ((max_market_cap_usd - min_market_cap_usd) * 0.2)
        if dex.market_cap < min_market_cap_usd:
            risks.append(f"Market cap terlalu kecil untuk alert utama: ${dex.market_cap:,.0f}.")
        elif dex.market_cap <= early_midpoint:
            score += 18
            reasons.append(f"Market cap masih kecil: ${dex.market_cap:,.0f}.")
        elif dex.market_cap <= max_market_cap_usd:
            score += 14
            reasons.append(f"Market cap masih early: ${dex.market_cap:,.0f}.")
        else:
            risks.append(f"Market cap di atas batas watchlist: ${dex.market_cap:,.0f}.")

    if dex.liquidity_usd:
        if 10_000 <= dex.liquidity_usd <= 300_000:
            score += 16
            reasons.append(f"Likuiditas cukup untuk early move: ${dex.liquidity_usd:,.0f}.")
        elif dex.liquidity_usd > 300_000:
            score += 9
            reasons.append(f"Likuiditas besar: ${dex.liquidity_usd:,.0f}.")
        elif dex.liquidity_usd >= 3_000:
            score += 6
            reasons.append(f"Likuiditas ada tapi masih tipis: ${dex.liquidity_usd:,.0f}.")

    if dex.volume_5m and dex.liquidity_usd:
        ratio = dex.volume_5m / max(dex.liquidity_usd, 1)
        if ratio >= 0.2:
            score += 18
            reasons.append("Volume 5m sangat kuat dibanding likuiditas.")
        elif ratio >= 0.05:
            score += 12
            reasons.append("Volume 5m mulai aktif dibanding likuiditas.")
        elif dex.volume_5m >= 1_000:
            score += 6
            reasons.append("Volume 5m aktif.")
    elif dex.volume_5m and dex.volume_5m >= 1_000:
        score += 6
        reasons.append("Volume 5m aktif.")

    if dex.txns_5m >= 60:
        score += 14
        reasons.append(f"Transaksi 5m tinggi: {dex.txns_5m}.")
    elif dex.txns_5m >= 25:
        score += 10
        reasons.append(f"Transaksi 5m cukup aktif: {dex.txns_5m}.")
    elif dex.txns_5m >= 10:
        score += 5
        reasons.append(f"Transaksi 5m mulai bergerak: {dex.txns_5m}.")

    if x_links:
        score += 16
        reasons.append("Ada X/Twitter, sinyal sosial utama untuk meme coin.")
    if dex.image_url or dex.header_url:
        score += 8
        reasons.append("Ada image/header, identitas visual tersedia.")
    if research.estimated_interest == "high":
        score += 10
        reasons.append("Free web research menunjukkan interest tinggi.")
    elif research.estimated_interest == "medium":
        score += 6
        reasons.append("Free web research menunjukkan interest sedang.")

    if risk:
        if risk.narrative != "unknown":
            score += 6
            reasons.append(f"Narrative: {risk.narrative}.")
        if risk.dev_sold_signal == "sold_seen":
            score -= 20
            risks.extend(risk.dev_sold_notes[:2])
        elif risk.dev_sold_signal == "check_failed":
            risks.extend(risk.dev_sold_notes[:1])
        if risk.smart_wallet_signal.startswith("positive"):
            score += 8
            reasons.append(f"Smart wallet signal: {risk.smart_wallet_signal}.")
        elif risk.smart_wallet_signal.startswith("mixed"):
            score += 3
            reasons.append(f"Smart wallet signal: {risk.smart_wallet_signal}.")
        if risk.sniper_risk.startswith("high"):
            score -= 12
            risks.append(_risk_with_count("Sniper", risk.sniper_risk, _tagged_trade_count(risk.sniper_risk)))
        elif risk.sniper_risk.startswith("medium"):
            score -= 6
            risks.append(_risk_with_count("Sniper", risk.sniper_risk, _tagged_trade_count(risk.sniper_risk)))
        sniper_count = _tagged_trade_count(risk.sniper_risk)
        sniper_count_penalty = _sniper_count_penalty(sniper_count)
        if sniper_count_penalty:
            score -= sniper_count_penalty
            if not risk.sniper_risk.startswith(("medium", "high")):
                risks.append(f"Sniper tagged trades semakin banyak: {sniper_count}.")
        if risk.bundle_risk.startswith("high"):
            score -= 14
            risks.append(_risk_with_count("Bundle", risk.bundle_risk, _tagged_trade_count(risk.bundle_risk)))
        elif risk.bundle_risk.startswith("medium"):
            score -= 7
            risks.append(_risk_with_count("Bundle", risk.bundle_risk, _tagged_trade_count(risk.bundle_risk)))
        bundle_count = _tagged_trade_count(risk.bundle_risk)
        bundle_count_penalty = _bundle_count_penalty(bundle_count)
        if bundle_count_penalty:
            score -= bundle_count_penalty
            if not risk.bundle_risk.startswith(("medium", "high")):
                risks.append(f"Bundle tagged trades semakin banyak: {bundle_count}.")
        if risk.holder_concentration_signal == "high":
            score -= 12
            risks.extend(risk.holder_concentration_notes[:1])
        elif risk.holder_concentration_signal == "medium":
            score -= 6
            risks.extend(risk.holder_concentration_notes[:1])
        if risk.bot_risk == "high":
            score -= 15
            risks.extend(risk.bot_notes[:2])
        elif risk.bot_risk == "medium":
            score -= 7
            risks.extend(risk.bot_notes[:1])

    return max(0, min(100, score)), _dedupe(reasons, 8), _dedupe(risks, 6)


def rule_score(
    candidate: TokenCandidate,
    dex: DexProfile,
    research: ResearchReport,
    risk: RiskNarrativeReport | None = None,
    min_market_cap_usd: int = 25_000,
    max_market_cap_usd: int = 200_000,
) -> ScoreReport:
    score = 0
    reasons: list[str] = []
    risks: list[str] = []

    if dex.found:
        score += 10
        reasons.append("DEX Screener pair/profile data found.")
    else:
        risks.append("No DEX Screener pair found yet.")

    x_links = _x_socials(dex)
    if dex.socials:
        score += min(18, len(dex.socials) * 6)
        reasons.append(f"{len(dex.socials)} social link(s) found.")
    else:
        risks.append("No social links found.")

    if x_links:
        score += 17
        reasons.append("X/Twitter social link found.")
    elif dex.socials:
        risks.append("Social links found, but no X/Twitter link.")

    if dex.image_url or dex.header_url:
        score += 7
        reasons.append("Token image/header is present on DEX Screener.")

    if dex.websites:
        score += 4
        reasons.append("Website link present as an extra trust signal.")

    if dex.liquidity_usd and dex.liquidity_usd >= 5_000:
        score += 10
        reasons.append(f"Liquidity is visible: ${dex.liquidity_usd:,.0f}.")
    elif dex.found and (dex.liquidity_usd is None or dex.liquidity_usd <= 0):
        risks.append("Liquidity is zero or unavailable.")
    elif dex.found:
        risks.append("Liquidity is missing or thin.")

    if dex.volume_5m and dex.volume_5m >= 1_000:
        score += 10
        reasons.append(f"5m volume is active: ${dex.volume_5m:,.0f}.")
    if dex.txns_5m >= 20:
        score += 10
        reasons.append(f"5m transaction count is active: {dex.txns_5m}.")
    elif dex.found:
        risks.append("Short-term transaction count is weak.")

    reachable_sites = sum(1 for item in research.website_checks if item.get("ok"))
    mint_mentions = sum(1 for item in research.website_checks if item.get("mentions_mint"))
    if reachable_sites:
        score += 8
        reasons.append(f"{reachable_sites} linked website(s) reachable.")
    if mint_mentions:
        score += 7
        reasons.append("A linked website mentions the token mint.")

    if research.estimated_interest == "high":
        score += 20
        reasons.append("Free web search suggests high identity visibility.")
    elif research.estimated_interest == "medium":
        score += 12
        reasons.append("Free web search suggests medium identity visibility.")
    elif research.estimated_interest == "low":
        score += 5
        reasons.append("Free web search found limited identity visibility.")
    else:
        if not x_links:
            risks.append("Free web search did not find strong identity visibility.")

    opp_score, opp_reasons, opp_risks = _opportunity_score(
        dex,
        research,
        risk,
        min_market_cap_usd=min_market_cap_usd,
        max_market_cap_usd=max_market_cap_usd,
    )
    risks.extend(opp_risks)
    if risk:
        if risk.narrative != "unknown":
            reasons.append(f"Narrative detected: {risk.narrative}.")
        if risk.dev_sold_signal == "sold_seen":
            score -= 18
            risks.append("Dev wallet sell detected.")
        elif risk.dev_sold_signal == "not_seen":
            reasons.append("No dev-wallet sell seen in live check.")
        if risk.smart_wallet_signal.startswith("positive"):
            reasons.append(f"Smart wallet signal: {risk.smart_wallet_signal}.")
        elif risk.smart_wallet_signal.startswith("mixed"):
            reasons.append(f"Smart wallet signal is mixed: {risk.smart_wallet_signal}.")
        if risk.sniper_risk.startswith(("medium", "high")):
            sniper_count = _tagged_trade_count(risk.sniper_risk)
            score -= _sniper_count_penalty(sniper_count)
            risks.append(f"Sniper risk: {risk.sniper_risk}.")
        if risk.bundle_risk.startswith(("medium", "high")):
            bundle_count = _tagged_trade_count(risk.bundle_risk)
            score -= _bundle_count_penalty(bundle_count)
            risks.append(f"Bundle risk: {risk.bundle_risk}.")
        if risk.holder_concentration_signal in {"medium", "high"}:
            risks.extend(risk.holder_concentration_notes[:1])
        if risk.bot_risk in {"medium", "high"}:
            risks.append(f"Bot activity heuristic: {risk.bot_risk}.")
    if dex.dex_id and dex.dex_id.lower() not in {"pumpswap", "pumpfun"}:
        risks.append(f"Not a Pump.fun/PumpSwap pair: {dex.dex_id}.")
    score = max(0, min(100, score))
    return ScoreReport(
        score=score,
        label=_label(score),
        reasons=_dedupe(reasons, 8),
        risks=_dedupe(risks, 8),
        opportunity_score=opp_score,
        opportunity_label=_opportunity_label(opp_score, dex=dex, risk=risk, max_market_cap_usd=max_market_cap_usd),
        opportunity_reasons=opp_reasons,
    )
