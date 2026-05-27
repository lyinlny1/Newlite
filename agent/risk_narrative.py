from __future__ import annotations

from agent.models import DexProfile, RiskNarrativeReport, TokenCandidate


def _blob(candidate: TokenCandidate, dex: DexProfile) -> str:
    parts = [
        candidate.name,
        candidate.symbol,
        dex.token_name,
        dex.token_symbol,
        dex.description,
        " ".join(dex.socials),
        " ".join(dex.websites),
    ]
    return " ".join(part for part in parts if part).lower()


def detect_narrative(candidate: TokenCandidate, dex: DexProfile) -> tuple[str, list[str]]:
    text = _blob(candidate, dex)
    checks = [
        ("AI / singularity", ["ai", "agent", "singularity", "gpt", "robot", "agi"]),
        ("wealth / moon", ["rich", "moon", "lambo", "million", "wealth", "pump"]),
        ("political / public figure", ["trump", "biden", "elon", "president", "maga"]),
        ("animal meme", ["dog", "cat", "frog", "pepe", "shib", "wif"]),
        ("internet meme", ["meme", "viral", "tiktok", "brainrot", "sigma", "chad"]),
        ("utility / infra claim", ["defi", "swap", "wallet", "chain", "protocol"]),
    ]
    matches = [label for label, words in checks if any(word in text for word in words)]
    if not matches:
        return "unknown", ["No obvious narrative keyword found."]
    return matches[0], [f"Detected narrative: {', '.join(matches[:3])}."]


def detect_bot_activity(dex: DexProfile) -> tuple[str, list[str]]:
    notes: list[str] = []
    if not dex.txns_5m:
        return "unknown", ["No short-term transaction data."]
    volume_per_tx = (dex.volume_5m or 0) / max(dex.txns_5m, 1)
    if dex.txns_5m >= 250 and volume_per_tx < 150:
        notes.append(f"Very high tx count with low average trade size (${volume_per_tx:,.0f}).")
        return "high", notes
    if dex.txns_5m >= 120 and volume_per_tx < 250:
        notes.append(f"High tx count with small average trade size (${volume_per_tx:,.0f}).")
        return "medium", notes
    if dex.txns_5m >= 60:
        notes.append("High activity, but not enough evidence to call bot traffic.")
        return "low", notes
    return "low", ["No strong bot-activity heuristic triggered."]


def detect_dev_wallet(candidate: TokenCandidate) -> tuple[str, list[str]]:
    raw = candidate.raw or {}
    keys = [
        "traderPublicKey",
        "creator",
        "creatorAddress",
        "deployer",
        "owner",
        "wallet",
        "publicKey",
    ]
    for key in keys:
        value = raw.get(key)
        if value:
            return str(value), [f"Dev wallet inferred from PumpPortal field `{key}`."]
    return "", ["Dev wallet not available from this token event."]


def analyze_risk_narrative(candidate: TokenCandidate, dex: DexProfile) -> RiskNarrativeReport:
    narrative, narrative_notes = detect_narrative(candidate, dex)
    bot_risk, bot_notes = detect_bot_activity(dex)
    dev_wallet, dev_sold_notes = detect_dev_wallet(candidate)
    data_gaps = [
        "Dev-sold, sniper, bundle, holder concentration, and smart-wallet detection need trade-stream or wallet-level data.",
        "Current bot-risk is heuristic from DEX tx/volume only, not wallet-level proof.",
    ]
    return RiskNarrativeReport(
        narrative=narrative,
        narrative_notes=narrative_notes,
        bot_risk=bot_risk,
        bot_notes=bot_notes,
        dev_wallet=dev_wallet,
        dev_sold_signal="not_checked" if dev_wallet else "not_available",
        dev_sold_notes=dev_sold_notes,
        sniper_risk="not_available",
        bundle_risk="not_available",
        smart_wallet_signal="not_available",
        data_gaps=data_gaps,
    )
