from __future__ import annotations

import time
from typing import Any

from storage.db import connect, init_db, missed_winners, performance_for, signal_performance

_CACHE: dict[str, Any] = {"key": None, "ts": 0, "adjustments": []}
_SOURCE_CACHE: dict[str, Any] = {"key": None, "ts": 0, "sources": []}
_CACHE_SECONDS = 300


def _signal_map(result: dict) -> set[str]:
    dex = result["dex"]
    risk = result.get("risk")
    signals = {
        "has_x": str(any("x.com" in url.lower() or "twitter.com" in url.lower() for url in dex.socials)),
        "has_image": str(bool(dex.image_url or dex.header_url)),
    }
    if risk:
        signals.update(
            {
                "narrative": risk.narrative,
                "dev_sold": risk.dev_sold_signal,
                "bundle": risk.bundle_risk,
                "bundle_level": risk.bundle_risk.split(" ", 1)[0] if risk.bundle_risk else "",
                "sniper": risk.sniper_risk,
                "sniper_level": risk.sniper_risk.split(" ", 1)[0] if risk.sniper_risk else "",
                "smart_wallet": risk.smart_wallet_signal,
                "holder_concentration": risk.holder_concentration_signal,
                "holder_concentration_level": risk.holder_concentration_signal,
                "bot_risk": risk.bot_risk,
                "bot_risk_level": risk.bot_risk,
            }
        )
    return {
        f"{key}={value}"
        for key, value in signals.items()
        if value not in {"None", "unknown", ""}
    }


def learned_signal_adjustments(min_samples: int = 20, max_adjustment: int = 10, limit: int = 300) -> list[dict[str, Any]]:
    cache_key = (min_samples, max_adjustment, limit)
    now = time.time()
    if _CACHE["key"] == cache_key and now - float(_CACHE["ts"]) < _CACHE_SECONDS:
        return list(_CACHE["adjustments"])
    rows = signal_performance(limit=limit)
    adjustments: list[dict[str, Any]] = []
    for row in rows:
        count = int(row.get("count") or 0)
        if count < min_samples:
            continue
        win_rate = float(row.get("win_rate") or 0)
        dump_rate = float(row.get("dump_rate") or 0)
        gave_back_rate = float(row.get("gave_back_rate") or 0)
        delta = 0
        if win_rate >= 0.45 and dump_rate <= 0.35:
            delta = round((win_rate - dump_rate) * max_adjustment)
        elif dump_rate >= 0.45:
            delta = -round((dump_rate - win_rate) * max_adjustment)
        elif gave_back_rate >= 0.65 and win_rate < 0.35:
            delta = -round(gave_back_rate * max_adjustment * 0.5)
        delta = max(-max_adjustment, min(max_adjustment, delta))
        if delta == 0:
            continue
        adjustments.append(
            {
                "signal": row["signal"],
                "delta": delta,
                "count": count,
                "win_rate": win_rate,
                "dump_rate": dump_rate,
                "gave_back_rate": gave_back_rate,
                "avg_peak": float(row.get("avg_peak") or 0),
            }
        )
    sorted_adjustments = sorted(adjustments, key=lambda item: abs(item["delta"]), reverse=True)
    _CACHE.update({"key": cache_key, "ts": now, "adjustments": sorted_adjustments})
    return sorted_adjustments


def _learned_adjustment_notes(adjustments: list[dict[str, Any]], limit: int = 6) -> list[str]:
    lessons: list[str] = []
    for item in adjustments[:limit]:
        if item["delta"] > 0:
            lessons.append(
                f"Prefer {item['signal']} when other basics are valid; "
                f"historical peak-win {item['win_rate'] * 100:.0f}% over n={item['count']}."
            )
        else:
            lessons.append(
                f"De-prioritize {item['signal']}; "
                f"historical dump {item['dump_rate'] * 100:.0f}% over n={item['count']}."
            )
    return lessons


def _best_current_and_peak(perf: dict[str, float | None]) -> tuple[float | None, float | None]:
    current_values = [
        value
        for value in (perf.get("market_cap_change_pct"), perf.get("price_change_pct"))
        if value is not None
    ]
    peak_values = [
        value
        for value in (perf.get("peak_market_cap_change_pct"), perf.get("peak_price_change_pct"))
        if value is not None
    ]
    current = max(current_values) if current_values else None
    peak = max(peak_values) if peak_values else None
    return current, peak


def source_performance(limit: int = 300, min_count: int = 3) -> list[dict[str, Any]]:
    cache_key = (limit, min_count)
    now = time.time()
    if _SOURCE_CACHE["key"] == cache_key and now - float(_SOURCE_CACHE["ts"]) < _CACHE_SECONDS:
        return list(_SOURCE_CACHE["sources"])
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT mint, source
                FROM tokens
                WHERE COALESCE(source, '') != ''
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        if peak is None:
            continue
        source = row["source"] or "unknown"
        bucket = buckets.setdefault(source, {"source": source, "count": 0, "wins": 0, "dumps": 0, "avg_peak": 0.0})
        bucket["count"] += 1
        bucket["wins"] += 1 if peak >= 30 else 0
        bucket["dumps"] += 1 if current is not None and current <= -30 else 0
        bucket["avg_peak"] += peak
    result = []
    for bucket in buckets.values():
        if bucket["count"] < min_count:
            continue
        bucket["avg_peak"] = bucket["avg_peak"] / bucket["count"]
        bucket["win_rate"] = bucket["wins"] / bucket["count"]
        bucket["dump_rate"] = bucket["dumps"] / bucket["count"]
        result.append(bucket)
    sorted_sources = sorted(result, key=lambda item: (item["win_rate"], item["avg_peak"], item["count"]), reverse=True)
    _SOURCE_CACHE.update({"key": cache_key, "ts": now, "sources": sorted_sources})
    return sorted_sources


def adjustment_for_result(result: dict, min_samples: int = 20, max_adjustment: int = 10) -> tuple[int, list[str]]:
    active = _signal_map(result)
    adjustments = learned_signal_adjustments(min_samples=min_samples, max_adjustment=max_adjustment)
    total = 0
    notes: list[str] = []
    for item in adjustments:
        if item["signal"] not in active:
            continue
        total += int(item["delta"])
        direction = "bonus" if item["delta"] > 0 else "penalty"
        notes.append(
            f"Learned {direction} {item['delta']:+d}: {item['signal']} "
            f"(n={item['count']}, win {item['win_rate'] * 100:.0f}%, dump {item['dump_rate'] * 100:.0f}%)."
        )
    source = result["token"].source
    for item in source_performance(min_count=max(3, min_samples // 2)):
        if item["source"] != source:
            continue
        delta = 0
        if item["win_rate"] >= 0.45 and item["dump_rate"] <= 0.35:
            delta = max(1, round((item["win_rate"] - item["dump_rate"]) * max_adjustment * 0.5))
        elif item["dump_rate"] >= 0.45:
            delta = -max(1, round((item["dump_rate"] - item["win_rate"]) * max_adjustment * 0.5))
        if delta:
            total += delta
            direction = "bonus" if delta > 0 else "penalty"
            notes.append(
                f"Learned source {direction} {delta:+d}: {source} "
                f"(n={item['count']}, win {item['win_rate'] * 100:.0f}%, dump {item['dump_rate'] * 100:.0f}%)."
            )
        break
    total = max(-max_adjustment, min(max_adjustment, total))
    return total, notes[:4]


def learning_summary(min_samples: int = 20, max_adjustment: int = 10) -> dict[str, Any]:
    adjustments = learned_signal_adjustments(min_samples=min_samples, max_adjustment=max_adjustment)
    missed = missed_winners(limit=5, min_peak_pct=100)
    sources = source_performance(min_count=3)
    return {
        "min_samples": min_samples,
        "max_adjustment": max_adjustment,
        "adjustments": adjustments,
        "lessons": _learned_adjustment_notes(adjustments),
        "missed_winners": missed,
        "source_performance": sources,
    }
