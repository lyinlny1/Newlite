from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import STORAGE_DIR


DB_PATH = STORAGE_DIR / "opportunity_agent.sqlite"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                mint TEXT PRIMARY KEY,
                symbol TEXT,
                name TEXT,
                source TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                first_score INTEGER,
                first_opportunity_score INTEGER,
                best_score INTEGER,
                best_opportunity_score INTEGER,
                first_price_usd REAL,
                first_market_cap REAL,
                first_liquidity_usd REAL,
                first_volume_5m REAL,
                x_url TEXT,
                image_url TEXT,
                dex_url TEXT,
                last_alert_score INTEGER DEFAULT 0,
                first_alert_at INTEGER DEFAULT 0,
                migration_alerted_at INTEGER DEFAULT 0,
                last_followup_at INTEGER DEFAULT 0,
                monitor_count INTEGER DEFAULT 0,
                sidelined_at INTEGER DEFAULT 0,
                last_daily_check_at INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                ts INTEGER NOT NULL,
                score INTEGER,
                opportunity_score INTEGER,
                price_usd REAL,
                market_cap REAL,
                liquidity_usd REAL,
                volume_5m REAL,
                txns_5m INTEGER,
                label TEXT,
                FOREIGN KEY(mint) REFERENCES tokens(mint)
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts ON snapshots(mint, ts);

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT NOT NULL,
                ts INTEGER NOT NULL,
                decision_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                summary TEXT,
                reason TEXT,
                risks_json TEXT,
                metrics_json TEXT,
                signals_json TEXT,
                rejected_json TEXT,
                FOREIGN KEY(mint) REFERENCES tokens(mint)
            );

            CREATE INDEX IF NOT EXISTS idx_decisions_mint_ts ON decisions(mint, ts);

            CREATE TABLE IF NOT EXISTS api_usage (
                api_name TEXT NOT NULL,
                period TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (api_name, period)
            );

            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tokens)")}
        if "first_opportunity_score" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN first_opportunity_score INTEGER")
        if "best_opportunity_score" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN best_opportunity_score INTEGER")
        if "migration_alerted_at" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN migration_alerted_at INTEGER DEFAULT 0")
        if "first_alert_at" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN first_alert_at INTEGER DEFAULT 0")
        if "monitor_count" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN monitor_count INTEGER DEFAULT 0")
        if "sidelined_at" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN sidelined_at INTEGER DEFAULT 0")
        if "last_daily_check_at" not in columns:
            conn.execute("ALTER TABLE tokens ADD COLUMN last_daily_check_at INTEGER DEFAULT 0")


def _month_period(ts: int | None = None) -> str:
    return time.strftime("%Y-%m", time.localtime(ts or int(time.time())))


def monthly_api_usage(api_name: str, period: str | None = None) -> int:
    init_db()
    period = period or _month_period()
    with connect() as conn:
        row = conn.execute(
            "SELECT count FROM api_usage WHERE api_name = ? AND period = ?",
            (api_name, period),
        ).fetchone()
    return int(row["count"] if row else 0)


def try_consume_monthly_api_usage(api_name: str, limit: int, amount: int = 1) -> bool:
    if limit <= 0:
        return True
    init_db()
    period = _month_period()
    now = int(time.time())
    with connect() as conn:
        row = conn.execute(
            "SELECT count FROM api_usage WHERE api_name = ? AND period = ?",
            (api_name, period),
        ).fetchone()
        current = int(row["count"] if row else 0)
        if current + amount > limit:
            return False
        conn.execute(
            """
            INSERT INTO api_usage (api_name, period, count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(api_name, period)
            DO UPDATE SET count = count + excluded.count, updated_at = excluded.updated_at
            """,
            (api_name, period, amount, now),
        )
    return True


def _float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _x_url(dex) -> str:
    return next((url for url in dex.socials if "x.com" in url.lower() or "twitter.com" in url.lower()), "")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def remember_analysis(result: dict) -> None:
    init_db()
    token = result["token"]
    dex = result["dex"]
    score = result["score"]
    now = int(time.time())
    symbol = token.symbol or dex.token_symbol
    name = token.name or dex.token_name
    x_url = _x_url(dex)
    price = _float(dex.price_usd)
    with connect() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE mint = ?", (token.mint,)).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO tokens (
                    mint, symbol, name, source, first_seen, last_seen,
                    first_score, first_opportunity_score, best_score, best_opportunity_score,
                    first_price_usd, first_market_cap, first_liquidity_usd, first_volume_5m,
                    x_url, image_url, dex_url, last_alert_score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token.mint,
                    symbol,
                    name,
                    token.source,
                    now,
                    now,
                    score.score,
                    score.opportunity_score,
                    score.score,
                    score.opportunity_score,
                    price,
                    dex.market_cap,
                    dex.liquidity_usd,
                    dex.volume_5m,
                    x_url,
                    dex.image_url,
                    dex.url,
                    0,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE tokens
                SET symbol = COALESCE(NULLIF(?, ''), symbol),
                    name = COALESCE(NULLIF(?, ''), name),
                    last_seen = ?,
                    best_score = MAX(COALESCE(best_score, 0), ?),
                    best_opportunity_score = MAX(COALESCE(best_opportunity_score, 0), ?),
                    x_url = COALESCE(NULLIF(?, ''), x_url),
                    image_url = COALESCE(NULLIF(?, ''), image_url),
                    dex_url = COALESCE(NULLIF(?, ''), dex_url)
                WHERE mint = ?
                """,
                (symbol, name, now, score.score, score.opportunity_score, x_url, dex.image_url, dex.url, token.mint),
            )
        conn.execute(
            """
            INSERT INTO snapshots (
                mint, ts, score, opportunity_score, price_usd, market_cap,
                liquidity_usd, volume_5m, txns_5m, label
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token.mint,
                now,
                score.score,
                score.opportunity_score,
                price,
                dex.market_cap,
                dex.liquidity_usd,
                dex.volume_5m,
                dex.txns_5m,
                score.label,
            ),
        )


def append_decision(
    result: dict,
    decision_type: str,
    actor: str,
    summary: str,
    reason: str,
    rejected: list[str] | None = None,
) -> None:
    init_db()
    token = result["token"]
    dex = result["dex"]
    score = result["score"]
    risk = result.get("risk")
    signals = {
        "has_x": bool(_x_url(dex)),
        "has_image": bool(dex.image_url or dex.header_url),
        "narrative": risk.narrative if risk else "unknown",
        "bot_risk": risk.bot_risk if risk else "unknown",
        "dev_sold": risk.dev_sold_signal if risk else "unknown",
        "sniper": risk.sniper_risk if risk else "unknown",
        "bundle": risk.bundle_risk if risk else "unknown",
        "smart_wallet": risk.smart_wallet_signal if risk else "unknown",
        "holder_concentration": risk.holder_concentration_signal if risk else "unknown",
        "dex_id": dex.dex_id,
    }
    metrics = {
        "opportunity_score": score.opportunity_score,
        "trust_score": score.score,
        "market_cap": dex.market_cap,
        "liquidity_usd": dex.liquidity_usd,
        "volume_5m": dex.volume_5m,
        "txns_5m": dex.txns_5m,
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO decisions (
                mint, ts, decision_type, actor, summary, reason,
                risks_json, metrics_json, signals_json, rejected_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token.mint,
                int(time.time()),
                decision_type,
                actor,
                summary[:500],
                reason[:1000],
                _json(score.risks[:8]),
                _json(metrics),
                _json(signals),
                _json((rejected or [])[:8]),
            ),
        )


def latest_decision(mint: str) -> sqlite3.Row | None:
    init_db()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM decisions WHERE mint = ? ORDER BY ts DESC LIMIT 1",
            (mint,),
        ).fetchone()


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def decision_as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "mint": row["mint"],
        "ts": row["ts"],
        "decision_type": row["decision_type"],
        "actor": row["actor"],
        "summary": row["summary"],
        "reason": row["reason"],
        "risks": _loads(row["risks_json"], []),
        "metrics": _loads(row["metrics_json"], {}),
        "signals": _loads(row["signals_json"], {}),
        "rejected": _loads(row["rejected_json"], []),
    }


def due_followups(max_age_hours: int = 24, min_interval_minutes: int = 10, limit: int = 20) -> list[sqlite3.Row]:
    init_db()
    now = int(time.time())
    min_age = now - max_age_hours * 3600
    due_before = now - min_interval_minutes * 60
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM tokens
                WHERE first_seen >= ?
                  AND COALESCE(last_alert_score, 0) > 0
                  AND COALESCE(sidelined_at, 0) = 0
                  AND COALESCE(NULLIF(last_followup_at, 0), NULLIF(first_alert_at, 0), first_seen) <= ?
                ORDER BY COALESCE(best_opportunity_score, 0) DESC, COALESCE(best_score, 0) DESC, first_seen DESC
                LIMIT ?
                """,
                (min_age, due_before, limit),
            )
        )


def mark_followup(mint: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "UPDATE tokens SET last_followup_at = ?, monitor_count = COALESCE(monitor_count, 0) + 1 WHERE mint = ?",
            (int(time.time()), mint),
        )


def sideline_token(mint: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "UPDATE tokens SET sidelined_at = ? WHERE mint = ? AND COALESCE(sidelined_at, 0) = 0",
            (int(time.time()), mint),
        )


def due_daily_checks(interval_hours: int = 24, limit: int = 100) -> list[sqlite3.Row]:
    init_db()
    now = int(time.time())
    due_before = now - interval_hours * 3600
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM tokens
                WHERE first_seen <= ?
                  AND COALESCE(last_daily_check_at, 0) <= ?
                ORDER BY COALESCE(last_alert_score, 0) DESC, last_seen ASC
                LIMIT ?
                """,
                (due_before, due_before, limit),
            )
        )


def mark_daily_check(mint: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("UPDATE tokens SET last_daily_check_at = ? WHERE mint = ?", (int(time.time()), mint))


def performance_for(mint: str) -> dict[str, float | None]:
    init_db()
    with connect() as conn:
        first = conn.execute("SELECT * FROM tokens WHERE mint = ?", (mint,)).fetchone()
        latest = conn.execute("SELECT * FROM snapshots WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,)).fetchone()
        peaks = conn.execute(
            """
            SELECT MAX(price_usd) AS peak_price_usd, MAX(market_cap) AS peak_market_cap
            FROM snapshots
            WHERE mint = ?
            """,
            (mint,),
        ).fetchone()
    if not first or not latest:
        return {
            "price_change_pct": None,
            "market_cap_change_pct": None,
            "peak_price_change_pct": None,
            "peak_market_cap_change_pct": None,
        }

    def pct(old: float | None, new: float | None) -> float | None:
        if old is None or new is None or old <= 0:
            return None
        return ((new - old) / old) * 100

    return {
        "price_change_pct": pct(first["first_price_usd"], latest["price_usd"]),
        "market_cap_change_pct": pct(first["first_market_cap"], latest["market_cap"]),
        "peak_price_change_pct": pct(first["first_price_usd"], peaks["peak_price_usd"] if peaks else None),
        "peak_market_cap_change_pct": pct(first["first_market_cap"], peaks["peak_market_cap"] if peaks else None),
    }


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


def _pct(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or old <= 0:
        return None
    return ((new - old) / old) * 100


def _outcome_label(current: float | None, peak: float | None) -> str:
    if current is None and peak is None:
        return "unknown"
    if peak is not None and peak >= 200:
        return "runner_3x"
    if peak is not None and peak >= 100:
        return "winner_2x"
    if peak is not None and peak >= 50 and (current is None or current <= 10):
        return "gave_back"
    if peak is not None and peak >= 30:
        return "moved_up"
    if current is not None and current <= -50:
        return "dumped"
    if current is not None and current <= -20:
        return "weak"
    return "flat"


def update_alert_score(mint: str, score: int) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            UPDATE tokens
            SET last_alert_score = MAX(COALESCE(last_alert_score, 0), ?),
                first_alert_at = CASE WHEN COALESCE(first_alert_at, 0) = 0 THEN ? ELSE first_alert_at END,
                sidelined_at = 0
            WHERE mint = ?
            """,
            (score, int(time.time()), mint),
        )


def daily_activity_report(hours: int = 24, limit: int = 12) -> dict[str, Any]:
    init_db()
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        scanned = conn.execute("SELECT COUNT(*) AS count FROM tokens WHERE first_seen >= ?", (since,)).fetchone()["count"]
        alerts = conn.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE decision_type = 'ALERT' AND ts >= ?",
            (since,),
        ).fetchone()["count"]
        followups = conn.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE decision_type = 'FOLLOW_UP' AND ts >= ?",
            (since,),
        ).fetchone()["count"]
        sidelined = conn.execute("SELECT COUNT(*) AS count FROM tokens WHERE sidelined_at >= ?", (since,)).fetchone()["count"]
        daily_checks = conn.execute(
            "SELECT COUNT(*) AS count FROM decisions WHERE decision_type = 'DAILY_CHECK' AND ts >= ?",
            (since,),
        ).fetchone()["count"]
        monitored = list(
            conn.execute(
                """
                SELECT * FROM tokens
                WHERE COALESCE(last_alert_score, 0) > 0
                ORDER BY COALESCE(monitor_count, 0) DESC, COALESCE(last_followup_at, 0) DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
    rows: list[dict[str, Any]] = []
    for row in monitored:
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        rows.append(
            {
                "mint": row["mint"],
                "symbol": row["symbol"] or row["name"] or row["mint"][:8],
                "first_market_cap": row["first_market_cap"],
                "change_pct": current,
                "peak_gain_pct": peak,
                "monitor_count": row["monitor_count"] or 0,
                "first_alert_at": row["first_alert_at"] or 0,
                "sidelined": bool(row["sidelined_at"] or 0),
                "last_alert_score": row["last_alert_score"] or 0,
                "state": token_state(row, perf),
            }
        )
    return {
        "hours": hours,
        "scanned": scanned,
        "alerts": alerts,
        "followups": followups,
        "daily_checks": daily_checks,
        "sidelined": sidelined,
        "monitored": rows,
    }


def snapshot_velocity(mint: str) -> dict[str, float | None]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                "SELECT * FROM snapshots WHERE mint = ? ORDER BY ts DESC LIMIT 2",
                (mint,),
            )
        )
    if len(rows) < 2:
        return {"volume_5m_change_pct": None, "txns_5m_change_pct": None, "liquidity_change_pct": None}
    latest, previous = rows[0], rows[1]
    return {
        "volume_5m_change_pct": _pct(previous["volume_5m"], latest["volume_5m"]),
        "txns_5m_change_pct": _pct(previous["txns_5m"], latest["txns_5m"]),
        "liquidity_change_pct": _pct(previous["liquidity_usd"], latest["liquidity_usd"]),
    }


def token_state(row: sqlite3.Row, perf: dict[str, float | None] | None = None) -> str:
    perf = perf or performance_for(row["mint"])
    current, peak = _best_current_and_peak(perf)
    if row["sidelined_at"] or 0:
        return "SIDELINED"
    if row["migration_alerted_at"] or 0:
        return "MIGRATED"
    if current is not None and current <= -50:
        return "DEAD"
    if peak is not None and peak >= 100:
        return "MOMENTUM"
    if row["last_alert_score"] or 0:
        return "WATCHING"
    return "NEW"


def top_peak_tokens(limit: int = 10) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                "SELECT * FROM tokens WHERE COALESCE(last_alert_score, 0) > 0 ORDER BY last_seen DESC LIMIT 300"
            )
        )
    result = []
    for row in rows:
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        if peak is None:
            continue
        result.append({
            "mint": row["mint"],
            "symbol": row["symbol"] or row["name"] or row["mint"][:8],
            "current": current,
            "peak": peak,
            "state": token_state(row, perf),
        })
    return sorted(result, key=lambda item: item["peak"], reverse=True)[:limit]


def missed_winners(limit: int = 10, min_peak_pct: float = 100) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT *
                FROM tokens t
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM decisions d
                    WHERE d.mint = t.mint
                      AND (
                        d.decision_type IN (
                            'ALERT', 'RISK_WATCH', 'MIGRATION', 'MIGRATED_DISCOVERY', 'MIGRATED_RISK_WATCH',
                            'OVER_RANGE_DISCOVERY'
                        )
                        OR d.decision_type LIKE 'OKX_%_DISCOVERY'
                        OR d.decision_type LIKE 'OKX_%_RISK_WATCH'
                        OR d.decision_type LIKE 'OKX_%_FALLBACK'
                        OR d.decision_type LIKE 'OKX_%_OVER_RANGE'
                      )
                )
                ORDER BY t.last_seen DESC
                LIMIT 500
                """
            )
        )
    result = []
    for row in rows:
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        if peak is not None and peak >= min_peak_pct:
            result.append({
                "mint": row["mint"],
                "symbol": row["symbol"] or row["name"] or row["mint"][:8],
                "peak": peak,
                "current": current,
                "best_opportunity_score": row["best_opportunity_score"] or 0,
            })
    return sorted(result, key=lambda item: item["peak"], reverse=True)[:limit]


def overrange_tokens(max_market_cap_usd: int, limit: int = 10) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT t.*, s.market_cap AS latest_market_cap
                FROM tokens t
                LEFT JOIN snapshots s ON s.id = (
                    SELECT id FROM snapshots WHERE mint = t.mint ORDER BY ts DESC LIMIT 1
                )
                WHERE COALESCE(s.market_cap, t.first_market_cap, 0) > ?
                ORDER BY COALESCE(s.market_cap, t.first_market_cap, 0) DESC
                LIMIT ?
                """,
                (max_market_cap_usd, limit),
            )
        )
    return [
        {
            "mint": row["mint"],
            "symbol": row["symbol"] or row["name"] or row["mint"][:8],
            "market_cap": row["latest_market_cap"] or row["first_market_cap"],
            "best_opportunity_score": row["best_opportunity_score"] or 0,
        }
        for row in rows
    ]


def narrative_heatmap(hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
    return _signal_bucket_report("narrative", hours=hours, limit=limit)


def risk_bucket_report(hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    risk_filters = {
        "bundle": ("medium", "high"),
        "sniper": ("medium", "high"),
        "holder_concentration": ("medium", "high"),
        "bot_risk": ("medium", "high"),
        "dev_sold": ("sold_seen", "check_failed"),
    }
    for key, prefixes in risk_filters.items():
        for item in _signal_bucket_report(key, hours=hours, limit=200, value_prefixes=prefixes):
            buckets[item["signal"]] = item
    return sorted(buckets.values(), key=lambda item: (item["count"], item["win_rate"], item["avg_peak"]), reverse=True)[:limit]


def _signal_bucket_report(
    signal_key: str,
    hours: int = 24,
    limit: int = 10,
    value_prefixes: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    init_db()
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT d.*
                FROM decisions d
                WHERE d.ts >= ?
                ORDER BY d.ts DESC
                LIMIT 1000
                """,
                (since,),
            )
        )
    buckets: dict[str, dict[str, Any]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for row in rows:
        signals = _loads(row["signals_json"], {})
        value = signals.get(signal_key)
        if value in (None, "unknown", "not_available", ""):
            continue
        value_text = str(value)
        if value_prefixes and not value_text.startswith(value_prefixes):
            continue
        pair = (row["mint"], value_text)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        if peak is None:
            continue
        signal = f"{signal_key}={value_text}"
        bucket = buckets.setdefault(signal, {"signal": signal, "count": 0, "wins": 0, "dumps": 0, "avg_peak": 0.0})
        bucket["count"] += 1
        bucket["wins"] += 1 if peak >= 30 else 0
        bucket["dumps"] += 1 if current is not None and current <= -30 else 0
        bucket["avg_peak"] += peak
    result = []
    for bucket in buckets.values():
        bucket["avg_peak"] = bucket["avg_peak"] / bucket["count"]
        bucket["win_rate"] = bucket["wins"] / bucket["count"]
        bucket["dump_rate"] = bucket["dumps"] / bucket["count"]
        result.append(bucket)
    return sorted(result, key=lambda item: (item["count"], item["win_rate"], item["avg_peak"]), reverse=True)[:limit]


def copycat_report(hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
    init_db()
    since = int(time.time()) - hours * 3600
    with connect() as conn:
        rows = list(conn.execute("SELECT symbol, name FROM tokens WHERE first_seen >= ?", (since,)))
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for row in rows:
        raw = row["symbol"] or row["name"] or ""
        key = re.sub(r"[^a-z0-9]+", "", raw.lower())
        if len(key) < 3:
            continue
        counts[key] = counts.get(key, 0) + 1
        display[key] = raw
    result = [{"name": display[key], "count": count} for key, count in counts.items() if count >= 2]
    return sorted(result, key=lambda item: item["count"], reverse=True)[:limit]


def should_alert_migration(mint: str) -> bool:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT last_alert_score, migration_alerted_at FROM tokens WHERE mint = ?", (mint,)).fetchone()
    if row is None:
        return False
    return bool((row["last_alert_score"] or 0) > 0 and not (row["migration_alerted_at"] or 0))


def should_check_migration_discovery(mint: str) -> bool:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT migration_alerted_at FROM tokens WHERE mint = ?", (mint,)).fetchone()
    return row is None or not (row["migration_alerted_at"] or 0)


def mark_migration_alerted(mint: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("UPDATE tokens SET migration_alerted_at = ? WHERE mint = ?", (int(time.time()), mint))


def top_tokens(limit: int = 10) -> list[sqlite3.Row]:
    init_db()
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM tokens
                ORDER BY COALESCE(best_opportunity_score, 0) DESC, COALESCE(best_score, 0) DESC, first_seen DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def alerted_token_outcomes(limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT * FROM tokens
                WHERE COALESCE(last_alert_score, 0) > 0
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        perf = performance_for(row["mint"])
        current, peak = _best_current_and_peak(perf)
        label = _outcome_label(current, peak)
        outcomes.append({
            "mint": row["mint"],
            "symbol": row["symbol"] or row["name"] or row["mint"][:8],
            "best_opportunity_score": row["best_opportunity_score"] or 0,
            "best_score": row["best_score"] or 0,
            "change_pct": current,
            "peak_gain_pct": peak,
            "label": label,
        })
    return outcomes


def signal_performance(limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        rows = list(
            conn.execute(
                """
                SELECT d.*, t.first_market_cap, t.mint
                FROM decisions d
                JOIN tokens t ON t.mint = d.mint
                WHERE (
                    d.decision_type IN (
                        'ALERT', 'FOLLOW_UP', 'MIGRATION', 'MANUAL_RESEARCH', 'DEEP_RESEARCH',
                        'RISK_WATCH', 'FILTERED_ALERT', 'MIGRATED_DISCOVERY', 'MIGRATED_RISK_WATCH',
                        'OKX_MIGRATED_DISCOVERY', 'OKX_MIGRATED_RISK_WATCH', 'OKX_MIGRATED_FALLBACK'
                    )
                    OR d.decision_type LIKE 'OKX_%_DISCOVERY'
                    OR d.decision_type LIKE 'OKX_%_RISK_WATCH'
                    OR d.decision_type LIKE 'OKX_%_FALLBACK'
                    OR d.decision_type LIKE 'OKX_%_FILTERED'
                    OR d.decision_type LIKE 'OKX_%_OVER_RANGE'
                )
                ORDER BY d.ts DESC
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
        signals = _loads(row["signals_json"], {})
        bundle = str(signals.get("bundle") or "")
        sniper = str(signals.get("sniper") or "")
        bot_risk = str(signals.get("bot_risk") or "")
        holder_concentration = str(signals.get("holder_concentration") or "")
        signal_values = [
            f"narrative={signals.get('narrative')}",
            f"dev_sold={signals.get('dev_sold')}",
            f"bundle={signals.get('bundle')}",
            f"bundle_level={bundle.split(' ', 1)[0] if bundle else ''}",
            f"sniper={signals.get('sniper')}",
            f"sniper_level={sniper.split(' ', 1)[0] if sniper else ''}",
            f"smart_wallet={signals.get('smart_wallet')}",
            f"holder_concentration={signals.get('holder_concentration')}",
            f"holder_concentration_level={holder_concentration}",
            f"bot_risk={signals.get('bot_risk')}",
            f"bot_risk_level={bot_risk}",
            f"has_x={signals.get('has_x')}",
            f"has_image={signals.get('has_image')}",
        ]
        for key in signal_values:
            if key.endswith("=") or key.endswith("=None") or key.endswith("=unknown"):
                continue
            bucket = buckets.setdefault(
                key,
                {"signal": key, "count": 0, "wins": 0, "dumps": 0, "gave_back": 0, "avg_peak": 0.0, "avg_current": 0.0},
            )
            bucket["count"] += 1
            bucket["wins"] += 1 if peak >= 30 else 0
            bucket["dumps"] += 1 if current is not None and current <= -30 else 0
            bucket["gave_back"] += 1 if peak >= 50 and (current is None or current <= 10) else 0
            bucket["avg_peak"] += peak
            bucket["avg_current"] += current or 0
    result = []
    for bucket in buckets.values():
        if bucket["count"]:
            bucket["avg_peak"] = bucket["avg_peak"] / bucket["count"]
            bucket["avg_current"] = bucket["avg_current"] / bucket["count"]
            bucket["win_rate"] = bucket["wins"] / bucket["count"]
            bucket["dump_rate"] = bucket["dumps"] / bucket["count"]
            bucket["gave_back_rate"] = bucket["gave_back"] / bucket["count"]
            result.append(bucket)
    return sorted(result, key=lambda item: (item["count"], item["win_rate"], item["avg_peak"]), reverse=True)
