from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenCandidate:
    mint: str
    name: str = ""
    symbol: str = ""
    source: str = "manual"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DexProfile:
    found: bool
    url: str = ""
    token_name: str = ""
    token_symbol: str = ""
    pair_address: str = ""
    dex_id: str = ""
    price_usd: str = ""
    market_cap: float | None = None
    fdv: float | None = None
    liquidity_usd: float | None = None
    volume_5m: float | None = None
    volume_1h: float | None = None
    txns_5m: int = 0
    txns_1h: int = 0
    pair_created_at: int | None = None
    websites: list[str] = field(default_factory=list)
    socials: list[str] = field(default_factory=list)
    image_url: str = ""
    header_url: str = ""
    description: str = ""
    boost_amount: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskNarrativeReport:
    narrative: str = "unknown"
    narrative_notes: list[str] = field(default_factory=list)
    bot_risk: str = "unknown"
    bot_notes: list[str] = field(default_factory=list)
    dev_wallet: str = ""
    dev_sold_signal: str = "unknown"
    dev_sold_notes: list[str] = field(default_factory=list)
    sniper_risk: str = "unknown"
    bundle_risk: str = "unknown"
    smart_wallet_signal: str = "unknown"
    holder_concentration_signal: str = "unknown"
    holder_concentration_notes: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class ResearchReport:
    search_query: str = ""
    results: list[SearchResult] = field(default_factory=list)
    website_checks: list[dict[str, Any]] = field(default_factory=list)
    estimated_interest: str = "unknown"
    notes: list[str] = field(default_factory=list)


@dataclass
class ScoreReport:
    score: int
    label: str
    reasons: list[str]
    risks: list[str]
    opportunity_score: int = 0
    opportunity_label: str = "UNKNOWN"
    opportunity_reasons: list[str] = field(default_factory=list)
    ai_summary: str = ""
    ai_provider: str = "none"
