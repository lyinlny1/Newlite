from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import time
from html import escape

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from agent.dexscreener import fetch_latest_solana_profiles, fetch_token_profile
from agent.models import TokenCandidate
from agent.okx_web3 import fetch_memepump_token_list
from agent.pumpportal import stream_migrations, stream_new_tokens, stream_new_tokens_live
from agent.research_agent import analyze_token, format_report_html
from agent.scoring import opportunity_breakdown
from config import Settings
from storage.db import (
    alerted_token_outcomes,
    append_decision,
    copycat_report,
    daily_activity_report,
    decision_as_dict,
    due_daily_checks,
    due_followups,
    latest_decision,
    mark_daily_check,
    mark_followup,
    mark_migration_alerted,
    missed_winners,
    monthly_api_usage,
    narrative_heatmap,
    overrange_tokens,
    performance_for,
    risk_bucket_report,
    should_alert_migration,
    should_check_migration_discovery,
    signal_performance,
    sideline_token,
    snapshot_velocity,
    top_peak_tokens,
    top_tokens,
    update_alert_score,
)
from storage.learning import learning_summary


LOGGER = logging.getLogger(__name__)
HTML = "HTML"
MC_RECHECK_DELAY_SECONDS = 120
MC_RECHECK_MAX_ATTEMPTS = 6
MC_PRECHECK_CONCURRENCY = 5
MC_PENDING_MAX = 1000
RISK_WATCH_MIN_OPPORTUNITY_SCORE = 40
OKX_DISCOVERY_SEEN: set[str] = set()


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text[:3900], parse_mode=HTML)


def _authorized(settings: Settings, update: Update) -> bool:
    return bool(settings.authorized_telegram_user_id and update.effective_user and update.effective_user.id == settings.authorized_telegram_user_id)


def _passes_alert_filter(result: dict, settings: Settings) -> bool:
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


def _passes_risk_watch_filter(result: dict, settings: Settings) -> bool:
    score = result["score"]
    dex = result["dex"]
    token = result["token"]
    if not token.mint.lower().endswith("pump"):
        return False
    if score.opportunity_score < RISK_WATCH_MIN_OPPORTUNITY_SCORE:
        return False
    if dex.market_cap is None or dex.market_cap < settings.min_market_cap_usd:
        return False
    if dex.market_cap > settings.max_market_cap_usd:
        return False
    if dex.dex_id and dex.dex_id.lower() not in {"pumpswap", "pumpfun"}:
        return False
    return score.opportunity_label in {"RISK", "HIGH_RISK", "DEV_SOLD_RISK", "NO_LIQUIDITY"}


def _runtime_scan_limit(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> int:
    return int(context.application.bot_data.get("scan_limit", settings.scan_limit))


def _runtime_reasoning_limit(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> int:
    return int(context.application.bot_data.get("max_reasoning_calls_per_scan", settings.max_reasoning_calls_per_scan))


def _runtime_scan_limit_app(application, settings: Settings) -> int:
    return int(application.bot_data.get("scan_limit", settings.scan_limit))


def _runtime_reasoning_limit_app(application, settings: Settings) -> int:
    return int(application.bot_data.get("max_reasoning_calls_per_scan", settings.max_reasoning_calls_per_scan))


def _runtime_followup_interval(context: ContextTypes.DEFAULT_TYPE, settings: Settings) -> int:
    return int(context.application.bot_data.get("followup_interval", settings.followup_interval_minutes))


def _runtime_followup_interval_app(application, settings: Settings) -> int:
    return int(application.bot_data.get("followup_interval", settings.followup_interval_minutes))


def _decision_reason(result: dict) -> str:
    score = result["score"]
    reason_bits = list(score.opportunity_reasons[:4])
    if score.risks:
        reason_bits.append("Risks: " + "; ".join(score.risks[:3]))
    return "; ".join(reason_bits) or "Rule-based research completed."


def _reasoning_note(items: list[str]) -> str:
    if not items:
        return ""
    return "\nReasoning to:\n" + "\n".join(f"• {item}" for item in items[:8])


def _reasoning_item(result: dict) -> str:
    token = result["token"]
    score = result["score"]
    name = _identity(result)
    return f"<code>{escape(token.mint)}</code> {escape(name)} | Opp {score.opportunity_score}/100 | Trust {score.score}/100"


def _identity(result: dict) -> str:
    token = result["token"]
    dex = result["dex"]
    return token.symbol or dex.token_symbol or token.name or dex.token_name or token.mint[:8]


def _log_decision(result: dict, decision_type: str, actor: str) -> None:
    score = result["score"]
    append_decision(
        result,
        decision_type=decision_type,
        actor=actor,
        summary=f"{_identity(result)} {decision_type}: Opp {score.opportunity_score}/100, Trust {score.score}/100.",
        reason=_decision_reason(result),
        rejected=score.risks[:4],
    )


def _format_risk_watch_report(result: dict) -> str:
    return format_report_html(result, title="🟡 <b>WATCH / RISK</b>")


def _best_change(perf: dict) -> float | None:
    values = [
        value
        for value in (perf.get("market_cap_change_pct"), perf.get("price_change_pct"))
        if value is not None
    ]
    return max(values) if values else None


def _dead_migrated_token(result: dict, settings: Settings) -> bool:
    dex = result["dex"]
    if dex.liquidity_usd is None or dex.liquidity_usd <= 0:
        return True
    if dex.market_cap is None or dex.market_cap <= 0:
        return True
    if (dex.volume_5m or 0) < 100 and dex.txns_5m < 3:
        return True
    if dex.market_cap < settings.min_market_cap_usd and dex.txns_5m < 5:
        return True
    return False


def _format_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _parse_report_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour = max(0, min(23, int(hour_text)))
        minute = max(0, min(59, int(minute_text)))
        return hour, minute
    except (AttributeError, ValueError):
        return 13, 0


def _daily_report_due(settings: Settings, now: int, last_report_at: int) -> bool:
    hour, minute = _parse_report_time(settings.daily_report_time)
    local_now = time.localtime(now)
    report_at = int(
        time.mktime(
            (
                local_now.tm_year,
                local_now.tm_mon,
                local_now.tm_mday,
                hour,
                minute,
                0,
                local_now.tm_wday,
                local_now.tm_yday,
                local_now.tm_isdst,
            )
        )
    )
    return now >= report_at and last_report_at < report_at


def _format_daily_report(report: dict) -> str:
    lines = [
        f"📒 <b>Daily Research Log</b> | {report['hours']}h",
        f"🔍 Scanned: <b>{report['scanned']}</b>",
        f"✅ Lolos alert: <b>{report['alerts']}</b>",
        f"🔁 Follow-up alerts: <b>{report['followups']}</b>",
        f"🧾 Daily audits: <b>{report['daily_checks']}</b>",
        f"🅿️ Dikesampingkan: <b>{report['sidelined']}</b>",
    ]
    monitored = report.get("monitored") or []
    if monitored:
        lines.append("")
        lines.append("<b>Monitored tokens</b>")
        for item in monitored[:10]:
            change = item["change_pct"]
            change_text = "unknown" if change is None else f"{change:+.1f}%"
            peak = item.get("peak_gain_pct")
            peak_text = "unknown" if peak is None else f"{peak:+.1f}%"
            state = item.get("state") or ("SIDELINED" if item["sidelined"] else "WATCHING")
            lines.append(
                f"• <b>{escape(str(item['symbol']))}</b> | {state} | MC awal ${item['first_market_cap'] or 0:,.0f} | "
                f"now {change_text} | peak {peak_text} | monitor {item['monitor_count']}x | alert {_format_ts(item['first_alert_at'])}"
            )
    return "\n".join(lines)


def _format_compact_followup(result: dict, perf: dict) -> str:
    token = result["token"]
    dex = result["dex"]
    name = token.symbol or dex.token_symbol or token.name or dex.token_name or token.mint[:8]
    price_change = perf.get("price_change_pct")
    mcap_change = perf.get("market_cap_change_pct")
    peak_price_change = perf.get("peak_price_change_pct")
    peak_mcap_change = perf.get("peak_market_cap_change_pct")
    lines = [
        f"🔁 <b>{escape(name)}</b>",
        f"🧾 <b>Mint:</b> <code>{escape(token.mint)}</code>",
    ]
    if price_change is not None:
        lines.append(f"📈 <b>Price since first seen:</b> {price_change:+.1f}%")
    if mcap_change is not None:
        lines.append(f"💰 <b>MC since first seen:</b> {mcap_change:+.1f}%")
    peak_values = [value for value in (peak_price_change, peak_mcap_change) if value is not None]
    if peak_values:
        lines.append(f"🏔️ <b>Peak gain since first seen:</b> {max(peak_values):+.1f}%")
    return "\n".join(lines)


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    if not _authorized(settings, update):
        await update.effective_message.reply_text("Unauthorized.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _reply(update, "🧠 <b>Newlite Research</b> online.\nResearch-only, no auto-buy.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    await _reply(
        update,
        "📘 <b>Commands</b>\n"
        "<code>/status</code>\n"
        "<code>/token &lt;mint&gt; [symbol] [name]</code>\n"
        "<code>/deep &lt;mint&gt; [symbol] [name]</code>\n"
        "<code>/why &lt;mint&gt;</code>\n"
        "<code>/learn</code>\n"
        "<code>/signals</code>\n"
        "<code>/why_score &lt;mint&gt;</code>\n"
        "<code>/top_peak</code>\n"
        "<code>/narratives</code>\n"
        "<code>/risk_report</code>\n"
        "<code>/missed</code>\n"
        "<code>/overrange</code>\n"
        "<code>/copycats</code>\n"
        "<code>/scan [limit] [timeout_seconds]</code>\n"
        "<code>/scan_limit &lt;jumlah&gt;</code> optional runtime override\n"
        "<code>/reasoning_limit &lt;jumlah&gt;</code>\n"
        "<code>/monitor_start [interval_minutes]</code>\n"
        "<code>/monitor_interval &lt;minutes&gt;</code>\n"
        "<code>/followup_interval &lt;minutes&gt;</code>\n"
        "<code>/daily_report</code>\n"
        "<code>/monitor_stop</code>\n"
        "<code>/monitor_status</code>\n"
        "<code>/opportunities</code>"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    scan_limit = _runtime_scan_limit(context, settings)
    reasoning_limit = _runtime_reasoning_limit(context, settings)
    followup_interval = _runtime_followup_interval(context, settings)
    await _reply(
        update,
        "⚙️ <b>Status</b>\n"
        f"🧠 <b>LLM:</b> {', '.join(settings.llm_provider_order)}\n"
        f"🏠 <b>Ollama:</b> {settings.ollama_local_model or 'not configured'}\n"
        f"☁️ <b>Ollama Cloud:</b> {settings.ollama_cloud_model or 'not configured'}\n"
        f"🔀 <b>OpenRouter:</b> {settings.openrouter_model or 'not configured'}\n"
        f"🧩 <b>Newlite agent:</b> {settings.newlite_agent_enabled} / min {settings.newlite_agent_min_opportunity_score}\n"
        f"🪽 <b>Official Hermes:</b> {settings.hermes_official_enabled}\n"
        f"🔎 <b>Free web:</b> {settings.enable_free_web_research}\n"
        f"🧬 <b>OKX wallet risk:</b> {settings.okx_wallet_risk_enabled and bool(settings.okx_api_key and settings.okx_secret_key and settings.okx_passphrase)}\n"
        f"📟 <b>OKX usage:</b> {monthly_api_usage('okx'):,}/{settings.okx_monthly_request_limit:,} this month\n"
        f"👥 <b>Helius holders:</b> {settings.wallet_risk_enabled and bool(settings.helius_api_key)}\n"
        f"🎯 <b>Min alert:</b> Opp {settings.min_alert_score}, MC ${settings.min_market_cap_usd:,.0f}\n"
        f"🧱 <b>Max MC:</b> ${settings.max_market_cap_usd:,.0f}\n"
        f"🔢 <b>Analysis cap/window:</b> {scan_limit}\n"
        f"🧠 <b>Max reasoning/scan:</b> {reasoning_limit}\n"
        f"⏱️ <b>Scan interval:</b> {context.application.bot_data.get('monitor_interval', settings.monitor_interval_minutes)} minutes\n"
        f"🔁 <b>Follow-up interval:</b> {followup_interval} minutes\n"
        f"🅿️ <b>Sideline if no move:</b> {settings.sideline_after_hours}h\n"
        f"📒 <b>Daily audit:</b> every {settings.daily_check_interval_hours}h"
        f" at {escape(settings.daily_report_time)}"
    )


async def token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: <code>/token &lt;mint&gt; [symbol] [name]</code>")
        return
    mint = context.args[0]
    symbol = context.args[1] if len(context.args) > 1 else ""
    name = " ".join(context.args[2:]) if len(context.args) > 2 else ""
    await _reply(update, f"🔎 Researching <code>{mint}</code>...")
    result = await analyze_token(
        TokenCandidate(mint=mint, symbol=symbol, name=name, source="telegram"),
        context.application.bot_data["settings"],
        force_reasoning=True,
    )
    _log_decision(result, "MANUAL_RESEARCH", "RESEARCHER")
    await _reply(update, format_report_html(result))


async def deep_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: <code>/deep &lt;mint&gt; [symbol] [name]</code>")
        return
    mint = context.args[0]
    symbol = context.args[1] if len(context.args) > 1 else ""
    name = " ".join(context.args[2:]) if len(context.args) > 2 else ""
    await _reply(update, f"🧠 Deep research <code>{escape(mint)}</code>...")
    result = await analyze_token(
        TokenCandidate(mint=mint, symbol=symbol, name=name, source="telegram_deep"),
        context.application.bot_data["settings"],
        force_reasoning=True,
    )
    _log_decision(result, "DEEP_RESEARCH", "RESEARCHER")
    await _reply(update, format_report_html(result, title=f"🧠 <b>DEEP RESEARCH</b> | <b>{escape(_identity(result))}</b>"))


async def why_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: <code>/why &lt;mint&gt;</code>")
        return
    mint = context.args[0]
    decision = decision_as_dict(latest_decision(mint))
    if not decision:
        await _reply(update, f"Belum ada decision log untuk <code>{escape(mint)}</code>.")
        return
    metrics = decision["metrics"]
    signals = decision["signals"]
    risks = decision["risks"]
    rejected = decision["rejected"]
    lines = [
        f"🧾 <b>Why</b> | <code>{escape(mint)}</code>",
        f"<b>Decision:</b> {escape(decision['decision_type'])} by {escape(decision['actor'])}",
        f"<b>Summary:</b> {escape(decision['summary'] or '-')}",
        f"<b>Reason:</b> {escape(decision['reason'] or '-')}",
        f"<b>Metrics:</b> Opp {metrics.get('opportunity_score', 0)}/100 | Trust {metrics.get('trust_score', 0)}/100 | MC ${metrics.get('market_cap') or 0:,.0f}",
        f"<b>Signals:</b> narrative={escape(str(signals.get('narrative')))}, dev={escape(str(signals.get('dev_sold')))}, bundle={escape(str(signals.get('bundle')))}, smart={escape(str(signals.get('smart_wallet')))}, holders={escape(str(signals.get('holder_concentration')))}",
    ]
    if risks:
        lines.append("<b>Risks:</b>\n" + "\n".join(f"• {escape(str(item))}" for item in risks[:4]))
    if rejected:
        lines.append("<b>Rejected/Watchouts:</b>\n" + "\n".join(f"• {escape(str(item))}" for item in rejected[:4]))
    await _reply(update, "\n".join(lines))


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    runtime_reasoning_limit = _runtime_reasoning_limit(context, settings)
    limit = int(context.args[0]) if context.args and context.args[0].isdigit() else _runtime_scan_limit(context, settings)
    timeout = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else settings.scan_timeout_seconds
    await _reply(update, f"🔍 <b>Scanning PumpPortal</b>\nLimit={limit}, timeout={timeout}s.")
    sent = 0
    reasoning_used = 0
    reasoning_items: list[str] = []
    try:
        async for candidate in stream_new_tokens(settings, limit=limit, timeout_seconds=timeout):
            allow_reasoning = reasoning_used < runtime_reasoning_limit
            result = await analyze_token(candidate, settings, enable_reasoning=allow_reasoning)
            if result["score"].ai_summary:
                reasoning_used += 1
                reasoning_items.append(_reasoning_item(result))
            if _passes_alert_filter(result, settings):
                sent += 1
                await _reply(update, format_report_html(result))
                _log_decision(result, "ALERT", "SCREENER")
                update_alert_score(candidate.mint, result["score"].opportunity_score)
            elif _passes_risk_watch_filter(result, settings):
                sent += 1
                await _reply(update, _format_risk_watch_report(result))
                _log_decision(result, "RISK_WATCH", "SCREENER")
                update_alert_score(candidate.mint, result["score"].opportunity_score)
            else:
                _log_decision(result, "FILTERED_ALERT", "SCREENER")
            await asyncio.sleep(0.25)
    except Exception as exc:
        LOGGER.exception("Scan failed")
        await _reply(update, f"⚠️ Scan failed: <code>{exc}</code>")
        return
    await _reply(update, f"✅ <b>Scan complete</b>\nAlerts sent: {sent}\nReasoning used: {reasoning_used}/{runtime_reasoning_limit}{_reasoning_note(reasoning_items)}")


async def _scan_once(
    settings: Settings,
    send_message,
    get_scan_limit,
    get_reasoning_limit,
    prefix: str = "Auto scan",
    timeout_seconds: int | None = None,
) -> int:
    sent = 0
    scanned = 0
    reasoning_used = 0
    reasoning_items: list[str] = []
    narrative_alerts: dict[str, int] = {}
    narrative_suppressed: dict[str, int] = {}
    scan_limit = get_scan_limit()
    reasoning_limit = get_reasoning_limit()
    timeout = timeout_seconds or settings.scan_timeout_seconds
    received = 0
    non_pump_skipped = 0
    LOGGER.info("%s started: limit=%s timeout=%ss", prefix, scan_limit, timeout)
    async for candidate in stream_new_tokens(
        settings,
        limit=scan_limit,
        timeout_seconds=timeout,
        consume_full_timeout=bool(timeout_seconds),
    ):
        received += 1
        if not candidate.mint.lower().endswith("pump"):
            non_pump_skipped += 1
            continue
        scanned += 1
        LOGGER.info(
            "%s candidate #%s mint=%s symbol=%s name=%s source=%s",
            prefix,
            scanned,
            candidate.mint,
            candidate.symbol or "-",
            candidate.name or "-",
            candidate.source,
        )
        reasoning_limit = get_reasoning_limit()
        allow_reasoning = reasoning_used < reasoning_limit
        result = await analyze_token(candidate, settings, enable_reasoning=allow_reasoning)
        if result["score"].ai_summary:
            reasoning_used += 1
            reasoning_items.append(_reasoning_item(result))
        if _passes_alert_filter(result, settings):
            narrative = getattr(result.get("risk"), "narrative", "unknown") or "unknown"
            if narrative_alerts.get(narrative, 0) >= 3:
                narrative_suppressed[narrative] = narrative_suppressed.get(narrative, 0) + 1
                _log_decision(result, "NARRATIVE_COOLDOWN", "SCREENER")
            else:
                narrative_alerts[narrative] = narrative_alerts.get(narrative, 0) + 1
                sent += 1
                await send_message(format_report_html(result))
                _log_decision(result, "ALERT", "SCREENER")
                update_alert_score(candidate.mint, result["score"].opportunity_score)
        elif _passes_risk_watch_filter(result, settings):
            sent += 1
            await send_message(_format_risk_watch_report(result))
            _log_decision(result, "RISK_WATCH", "SCREENER")
            update_alert_score(candidate.mint, result["score"].opportunity_score)
        else:
            _log_decision(result, "FILTERED_ALERT", "SCREENER")
        await asyncio.sleep(0.25)
    cooldown = ""
    if narrative_suppressed:
        cooldown = "\nCooldown: " + ", ".join(f"{escape(k)} {v}x" for k, v in narrative_suppressed.items())
    LOGGER.info(
        "%s complete: received=%s scanned=%s non_pump_skipped=%s alerts=%s reasoning_used=%s/%s",
        prefix,
        received,
        scanned,
        non_pump_skipped,
        sent,
        reasoning_used,
        get_reasoning_limit(),
    )
    await send_message(f"✅ <b>{prefix} complete</b>\nScanned: {scanned}\nAlerts sent: {sent}\nReasoning used: {reasoning_used}/{get_reasoning_limit()}{cooldown}{_reasoning_note(reasoning_items)}")
    return sent


async def _followup_once(settings: Settings, send_message, followup_interval_minutes: int) -> int:
    sent = 0
    due = due_followups(min_interval_minutes=followup_interval_minutes, limit=10)
    for row in due:
        candidate = TokenCandidate(mint=row["mint"], symbol=row["symbol"] or "", name=row["name"] or "", source="followup")
        result = await analyze_token(candidate, settings, enable_reasoning=False)
        perf = performance_for(candidate.mint)
        velocity = snapshot_velocity(candidate.mint)
        mark_followup(candidate.mint)
        score = result["score"]
        dex = result["dex"]
        price_change = perf.get("price_change_pct")
        mcap_change = perf.get("market_cap_change_pct")
        first_alert_at = row["first_alert_at"] or row["first_seen"]
        best_change = _best_change(perf)
        no_move_after_window = (
            bool(first_alert_at)
            and int(time.time()) - int(first_alert_at) >= settings.sideline_after_hours * 3600
            and best_change is not None
            and best_change <= 0
        )
        if no_move_after_window:
            sideline_token(candidate.mint)
            _log_decision(result, "SIDELINED", "FOLLOWUP")
            continue
        previous_alert_score = row["last_alert_score"] or 0
        score_improved = score.opportunity_score >= previous_alert_score + 15
        volume_accel = velocity.get("volume_5m_change_pct")
        tx_accel = velocity.get("txns_5m_change_pct")
        meaningful_move = (
            (
                dex.market_cap is not None
                and dex.market_cap >= settings.min_market_cap_usd
                and (
                    (price_change is not None and abs(price_change) >= 30)
                    or (mcap_change is not None and abs(mcap_change) >= 30)
                    or (volume_accel is not None and volume_accel >= 150)
                    or (tx_accel is not None and tx_accel >= 100)
                    or score_improved
                )
            )
        )
        if not meaningful_move:
            continue
        sent += 1
        await send_message(_format_compact_followup(result, perf))
        _log_decision(result, "FOLLOW_UP", "FOLLOWUP")
        update_alert_score(candidate.mint, score.opportunity_score)
    return sent


async def _daily_check_once(settings: Settings) -> int:
    checked = 0
    due = due_daily_checks(interval_hours=settings.daily_check_interval_hours, limit=100)
    for row in due:
        candidate = TokenCandidate(mint=row["mint"], symbol=row["symbol"] or "", name=row["name"] or "", source="daily_check")
        result = await analyze_token(candidate, settings, enable_reasoning=False)
        mark_daily_check(candidate.mint)
        _log_decision(result, "DAILY_CHECK", "AUDIT")
        checked += 1
        await asyncio.sleep(0.2)
    return checked


async def _migrated_discovery_once(settings: Settings, send_message, get_reasoning_limit) -> int:
    if not settings.migrated_discovery_enabled:
        return 0
    profiles = await fetch_latest_solana_profiles(limit=max(20, settings.migrated_discovery_limit * 4))
    sent = 0
    reasoning_used = 0
    for profile in profiles:
        if sent >= settings.migrated_discovery_limit:
            break
        mint = str(profile.get("tokenAddress") or "")
        if not mint or not mint.lower().endswith("pump") or not should_check_migration_discovery(mint):
            continue
        candidate = TokenCandidate(mint=mint, source="migrated_discovery")
        result = await analyze_token(candidate, settings, enable_reasoning=False)
        dex = result["dex"]
        if not (dex.found and dex.dex_id and dex.dex_id.lower() == "pumpswap"):
            _log_decision(result, "MIGRATED_RECHECK_PENDING", "DISCOVERY")
            continue
        if _dead_migrated_token(result, settings):
            _log_decision(result, "MIGRATED_DEAD_SKIP", "DISCOVERY")
            mark_migration_alerted(mint)
            continue
        if _passes_alert_filter(result, settings):
            if reasoning_used < get_reasoning_limit():
                reasoned = await analyze_token(candidate, settings, force_reasoning=True)
                if reasoned["score"].ai_summary:
                    result = reasoned
                    reasoning_used += 1
            _log_decision(result, "MIGRATED_DISCOVERY", "DISCOVERY")
            await send_message(format_report_html(result, title="🚀 <b>MIGRATED DISCOVERY</b>"))
            sent += 1
        elif _passes_risk_watch_filter(result, settings):
            _log_decision(result, "MIGRATED_RISK_WATCH", "DISCOVERY")
            await send_message(format_report_html(result, title="🟡 <b>MIGRATED WATCH / RISK</b>"))
            update_alert_score(candidate.mint, result["score"].opportunity_score)
            sent += 1
        else:
            _log_decision(result, "MIGRATED_FILTERED", "DISCOVERY")
        mark_migration_alerted(mint)
        await asyncio.sleep(0.25)
    return sent


def _okx_market_cap(item: dict) -> float | None:
    try:
        return float(((item.get("market") or {}).get("marketCapUsd")))
    except (TypeError, ValueError):
        return None


def _okx_stage_title(stage: str, overrange: bool = False, risk_watch: bool = False, fallback: bool = False) -> str:
    stage_label = stage.replace("_", " ")
    if overrange:
        return f"🟣 <b>OKX {stage_label} OVER RANGE</b>"
    if risk_watch:
        return f"🟡 <b>OKX {stage_label} WATCH / RISK</b>"
    if fallback:
        return f"🚀 <b>OKX {stage_label} DISCOVERY</b>"
    return f"🚀 <b>OKX {stage_label} DISCOVERY</b>"


def _format_okx_memepump_fallback(item: dict, market_cap: float | None, stage: str) -> str:
    market = item.get("market") or {}
    tags = item.get("tags") or {}
    social = item.get("social") or {}
    mint = str(item.get("tokenContractAddress") or "")
    symbol = str(item.get("symbol") or "")
    name = str(item.get("name") or "")
    volume_1h = market.get("volumeUsd1h")
    tx_1h = market.get("txCount1h")
    holders = tags.get("totalHolders")
    snipers = tags.get("snipersPercent")
    bundlers = tags.get("bundlersPercent")
    top10 = tags.get("top10HoldingsPercent")
    socials = [url for url in (social.get("x"), social.get("telegram"), social.get("website")) if url]
    lines = [
        _okx_stage_title(stage, fallback=True),
        f"<b>{escape(symbol or name or mint[:8])}</b> {escape(name) if name and name != symbol else ''}".strip(),
        f"🧾 <b>Mint:</b> <code>{escape(mint)}</code>",
        f"📊 <b>OKX MC:</b> ${market_cap or 0:,.0f}",
        f"🌊 <b>OKX 1h:</b> vol ${float(volume_1h or 0):,.0f} | tx {escape(str(tx_1h or 0))}",
        f"🧬 <b>Risk tags:</b> holders {escape(str(holders or '-'))} | sniper {escape(str(snipers or '-'))}% | bundle {escape(str(bundlers or '-'))}% | top10 {escape(str(top10 or '-'))}%",
        "<b>Aksi:</b> observe",
    ]
    if socials:
        lines.append("<b>Links:</b> " + " | ".join(escape(str(url)) for url in socials[:3]))
    return "\n".join(lines)


async def _okx_migrated_discovery_once(settings: Settings, send_message, get_reasoning_limit) -> int:
    if not settings.okx_migrated_discovery_enabled:
        return 0
    sent = 0
    checked = 0
    reasoning_used = 0
    total_items = 0
    stages = [stage for stage in settings.okx_memepump_discovery_stages if stage in {"NEW", "MIGRATING", "MIGRATED"}]
    for stage in stages:
        try:
            items = await fetch_memepump_token_list(
                settings,
                stage=stage,
                limit=settings.okx_migrated_discovery_limit,
                min_market_cap_usd=settings.min_market_cap_usd,
                max_token_age_minutes=settings.okx_migrated_discovery_max_age_minutes,
                min_volume_usd=settings.okx_memepump_discovery_min_volume_usd,
                min_tx_count=settings.okx_memepump_discovery_min_tx_count,
                min_buy_tx_count=settings.okx_memepump_discovery_min_buy_tx_count,
            )
        except Exception:
            LOGGER.exception("OKX memepump discovery failed for stage=%s", stage)
            continue
        total_items += len(items)
        for item in items:
            if sent >= settings.okx_migrated_discovery_limit:
                break
            mint = str(item.get("tokenContractAddress") or "")
            seen_key = f"{stage}:{mint}"
            if not mint or not mint.lower().endswith("pump") or seen_key in OKX_DISCOVERY_SEEN:
                continue
            if stage == "MIGRATED" and not should_check_migration_discovery(mint):
                continue
            market_cap = _okx_market_cap(item)
            if market_cap is not None and market_cap < settings.min_market_cap_usd:
                OKX_DISCOVERY_SEEN.add(seen_key)
                continue
            candidate = TokenCandidate(
                mint=mint,
                symbol=str(item.get("symbol") or ""),
                name=str(item.get("name") or ""),
                source=f"okx_{stage.lower()}_discovery",
                raw=item,
            )
            OKX_DISCOVERY_SEEN.add(seen_key)
            checked += 1
            result = await analyze_token(candidate, settings, enable_reasoning=False)
            dex = result["dex"]
            if not dex.found and market_cap is not None:
                await send_message(_format_okx_memepump_fallback(item, market_cap, stage))
                _log_decision(result, f"OKX_{stage}_FALLBACK", "DISCOVERY")
                if stage == "MIGRATED":
                    mark_migration_alerted(mint)
                sent += 1
                await asyncio.sleep(0.25)
                continue
            if stage == "MIGRATED" and _dead_migrated_token(result, settings):
                _log_decision(result, "OKX_MIGRATED_DEAD_SKIP", "DISCOVERY")
                mark_migration_alerted(mint)
                continue
            overrange = result["score"].opportunity_label == "OVER_RANGE"
            if _passes_alert_filter(result, settings) or overrange:
                if reasoning_used < get_reasoning_limit():
                    reasoned = await analyze_token(candidate, settings, force_reasoning=True)
                    if reasoned["score"].ai_summary:
                        result = reasoned
                        reasoning_used += 1
                if overrange:
                    _log_decision(result, f"OKX_{stage}_OVER_RANGE", "DISCOVERY")
                    await send_message(format_report_html(result, title=_okx_stage_title(stage, overrange=True)))
                else:
                    _log_decision(result, f"OKX_{stage}_DISCOVERY", "DISCOVERY")
                    await send_message(format_report_html(result, title=_okx_stage_title(stage)))
                update_alert_score(candidate.mint, result["score"].opportunity_score)
                if stage == "MIGRATED":
                    mark_migration_alerted(mint)
                sent += 1
            elif _passes_risk_watch_filter(result, settings):
                _log_decision(result, f"OKX_{stage}_RISK_WATCH", "DISCOVERY")
                await send_message(format_report_html(result, title=_okx_stage_title(stage, risk_watch=True)))
                update_alert_score(candidate.mint, result["score"].opportunity_score)
                if stage == "MIGRATED":
                    mark_migration_alerted(mint)
                sent += 1
            else:
                _log_decision(result, f"OKX_{stage}_FILTERED", "DISCOVERY")
                if stage == "MIGRATED":
                    mark_migration_alerted(mint)
            await asyncio.sleep(0.25)
    LOGGER.info("OKX memepump discovery complete: stages=%s items=%s checked=%s sent=%s", ",".join(stages), total_items, checked, sent)
    return sent


def _new_live_stats() -> dict[str, object]:
    return {
        "received": 0,
        "mc_pending_added": 0,
        "mc_pending_dropped": 0,
        "mc_checks": 0,
        "mc_retry": 0,
        "mc_promoted": 0,
        "mc_final_skipped": 0,
        "queued": 0,
        "queue_dropped": 0,
        "non_pump_skipped": 0,
        "market_precheck_skipped": 0,
        "market_precheck_errors": 0,
        "analyzed": 0,
        "alerts": 0,
        "reasoning_used": 0,
        "reasoning_items": [],
        "narrative_alerts": {},
        "narrative_suppressed": {},
    }


async def _live_token_producer(
    settings: Settings,
    pending: dict[str, dict[str, object]],
    stats: dict[str, object],
) -> None:
    async for candidate in stream_new_tokens_live(settings):
        stats["received"] = int(stats["received"]) + 1
        if not candidate.mint.lower().endswith("pump"):
            stats["non_pump_skipped"] = int(stats["non_pump_skipped"]) + 1
            continue
        if candidate.mint in pending:
            continue
        if len(pending) >= MC_PENDING_MAX:
            stats["mc_pending_dropped"] = int(stats["mc_pending_dropped"]) + 1
            LOGGER.warning("Live scan MC pending full; dropped mint=%s", candidate.mint)
            continue
        pending[candidate.mint] = {
            "candidate": candidate,
            "attempts": 0,
            "next_check_at": time.time(),
        }
        stats["mc_pending_added"] = int(stats["mc_pending_added"]) + 1


async def _live_market_rechecker(
    settings: Settings,
    pending: dict[str, dict[str, object]],
    queue: asyncio.Queue[TokenCandidate],
    stats: dict[str, object],
) -> None:
    semaphore = asyncio.Semaphore(MC_PRECHECK_CONCURRENCY)
    while True:
        now = time.time()
        due = [
            mint
            for mint, item in pending.items()
            if float(item.get("next_check_at") or 0) <= now
        ][:MC_PRECHECK_CONCURRENCY]
        if not due:
            await asyncio.sleep(1)
            continue
        await asyncio.gather(*[_check_market_gate(settings, pending, queue, stats, semaphore, mint) for mint in due])


async def _check_market_gate(
    settings: Settings,
    pending: dict[str, dict[str, object]],
    queue: asyncio.Queue[TokenCandidate],
    stats: dict[str, object],
    semaphore: asyncio.Semaphore,
    mint: str,
) -> None:
    item = pending.get(mint)
    if not item:
        return
    candidate = item["candidate"]
    if not isinstance(candidate, TokenCandidate):
        pending.pop(mint, None)
        return
    item["attempts"] = int(item.get("attempts") or 0) + 1
    stats["mc_checks"] = int(stats["mc_checks"]) + 1
    try:
        async with semaphore:
            dex = await fetch_token_profile(candidate.mint, timeout_seconds=6)
    except Exception:
        stats["market_precheck_errors"] = int(stats["market_precheck_errors"]) + 1
        LOGGER.exception("Live scan market precheck failed for mint=%s", candidate.mint)
        if int(item["attempts"]) >= MC_RECHECK_MAX_ATTEMPTS:
            pending.pop(mint, None)
            stats["mc_final_skipped"] = int(stats["mc_final_skipped"]) + 1
        else:
            item["next_check_at"] = time.time() + MC_RECHECK_DELAY_SECONDS
        return
    market_cap = dex.market_cap
    if dex.found and market_cap is not None and settings.min_market_cap_usd <= market_cap <= settings.max_market_cap_usd:
        pending.pop(mint, None)
        stats["mc_promoted"] = int(stats["mc_promoted"]) + 1
        try:
            queue.put_nowait(candidate)
            stats["queued"] = int(stats["queued"]) + 1
            LOGGER.info("Live scan MC gate promoted mint=%s market_cap=%s", candidate.mint, market_cap)
        except asyncio.QueueFull:
            stats["queue_dropped"] = int(stats["queue_dropped"]) + 1
            LOGGER.warning("Live scan queue full; dropped mint=%s", candidate.mint)
        return
    if market_cap is not None and market_cap > settings.max_market_cap_usd:
        pending.pop(mint, None)
        candidate.source = "live_overrange"
        stats["mc_promoted"] = int(stats["mc_promoted"]) + 1
        try:
            queue.put_nowait(candidate)
            stats["queued"] = int(stats["queued"]) + 1
            LOGGER.info("Live scan MC gate overrange promoted mint=%s market_cap=%s", candidate.mint, market_cap)
        except asyncio.QueueFull:
            stats["queue_dropped"] = int(stats["queue_dropped"]) + 1
            LOGGER.warning("Live scan queue full; dropped overrange mint=%s", candidate.mint)
        return
    if market_cap is not None and market_cap < settings.min_market_cap_usd:
        pending.pop(mint, None)
        stats["market_precheck_skipped"] = int(stats["market_precheck_skipped"]) + 1
        stats["mc_final_skipped"] = int(stats["mc_final_skipped"]) + 1
        LOGGER.info(
            "Live scan MC gate skipped mint=%s found=%s market_cap=%s",
            candidate.mint,
            dex.found,
            market_cap,
        )
        return
    if int(item["attempts"]) >= MC_RECHECK_MAX_ATTEMPTS:
        pending.pop(mint, None)
        stats["market_precheck_skipped"] = int(stats["market_precheck_skipped"]) + 1
        stats["mc_final_skipped"] = int(stats["mc_final_skipped"]) + 1
        LOGGER.info(
            "Live scan MC gate final skipped mint=%s found=%s market_cap=%s attempts=%s",
            candidate.mint,
            dex.found,
            market_cap,
            item["attempts"],
        )
        return
    stats["mc_retry"] = int(stats["mc_retry"]) + 1
    item["next_check_at"] = time.time() + MC_RECHECK_DELAY_SECONDS
    LOGGER.info(
        "Live scan MC gate retry mint=%s found=%s market_cap=%s attempts=%s",
        candidate.mint,
        dex.found,
        market_cap,
        item["attempts"],
    )


async def _live_token_analyzer(
    settings: Settings,
    send_message,
    get_reasoning_limit,
    queue: asyncio.Queue[TokenCandidate],
    stats: dict[str, object],
) -> None:
    while True:
        candidate = await queue.get()
        try:
            stats["analyzed"] = int(stats["analyzed"]) + 1
            LOGGER.info(
                "Live scan analyzing #%s mint=%s symbol=%s name=%s source=%s queue_pending=%s",
                stats["analyzed"],
                candidate.mint,
                candidate.symbol or "-",
                candidate.name or "-",
                candidate.source,
                queue.qsize(),
            )
            reasoning_limit = get_reasoning_limit()
            reasoning_used = int(stats["reasoning_used"])
            allow_reasoning = reasoning_used < reasoning_limit
            result = await analyze_token(candidate, settings, enable_reasoning=allow_reasoning)
            if result["score"].ai_summary:
                stats["reasoning_used"] = int(stats["reasoning_used"]) + 1
                reasoning_items = stats["reasoning_items"]
                if isinstance(reasoning_items, list):
                    reasoning_items.append(_reasoning_item(result))
            overrange_discovery = candidate.source == "live_overrange" and result["score"].opportunity_label == "OVER_RANGE"
            risk_watch = _passes_risk_watch_filter(result, settings)
            if _passes_alert_filter(result, settings) or overrange_discovery or risk_watch:
                narrative = getattr(result.get("risk"), "narrative", "unknown") or "unknown"
                narrative_alerts = stats["narrative_alerts"]
                narrative_suppressed = stats["narrative_suppressed"]
                if not isinstance(narrative_alerts, dict) or not isinstance(narrative_suppressed, dict):
                    continue
                if narrative_alerts.get(narrative, 0) >= 3:
                    narrative_suppressed[narrative] = narrative_suppressed.get(narrative, 0) + 1
                    _log_decision(result, "NARRATIVE_COOLDOWN", "SCREENER")
                else:
                    narrative_alerts[narrative] = narrative_alerts.get(narrative, 0) + 1
                    stats["alerts"] = int(stats["alerts"]) + 1
                    if overrange_discovery:
                        await send_message(format_report_html(result, title="🟣 <b>OVER RANGE DISCOVERY</b>"))
                        _log_decision(result, "OVER_RANGE_DISCOVERY", "SCREENER")
                    elif risk_watch:
                        await send_message(_format_risk_watch_report(result))
                        _log_decision(result, "RISK_WATCH", "SCREENER")
                    else:
                        await send_message(format_report_html(result))
                        _log_decision(result, "ALERT", "SCREENER")
                    update_alert_score(candidate.mint, result["score"].opportunity_score)
            else:
                _log_decision(result, "FILTERED_ALERT", "SCREENER")
            await asyncio.sleep(0.25)
        except Exception:
            LOGGER.exception("Live scan analyze failed for mint=%s", candidate.mint)
        finally:
            queue.task_done()


def _format_live_summary(prefix: str, stats: dict[str, object], queue_size: int, reasoning_limit: int) -> str:
    narrative_suppressed = stats["narrative_suppressed"]
    cooldown = ""
    if isinstance(narrative_suppressed, dict) and narrative_suppressed:
        cooldown = "\nCooldown: " + ", ".join(f"{escape(str(k))} {v}x" for k, v in narrative_suppressed.items())
    reasoning_items = stats["reasoning_items"] if isinstance(stats["reasoning_items"], list) else []
    return (
        f"✅ <b>{prefix}</b>\n"
        f"Received: {stats['received']}\n"
        f"MC pending added: {stats['mc_pending_added']}\n"
        f"MC pending now: {stats.get('mc_pending_now', 0)}\n"
        f"MC checks: {stats['mc_checks']}\n"
        f"MC retry: {stats['mc_retry']}\n"
        f"MC promoted: {stats['mc_promoted']}\n"
        f"MC final skipped: {stats['mc_final_skipped']}\n"
        f"Queued: {stats['queued']}\n"
        f"Analyzed: {stats['analyzed']}\n"
        f"Skipped non-pump: {stats['non_pump_skipped']}\n"
        f"Skipped MC gate: {stats['market_precheck_skipped']}\n"
        f"MC precheck errors: {stats['market_precheck_errors']}\n"
        f"MC pending dropped: {stats['mc_pending_dropped']}\n"
        f"Dropped queue full: {stats['queue_dropped']}\n"
        f"Queue pending: {queue_size}\n"
        f"Alerts sent: {stats['alerts']}\n"
        f"Reasoning used: {stats['reasoning_used']}/{reasoning_limit}"
        f"{cooldown}{_reasoning_note(reasoning_items)}"
    )


async def _monitor_loop(
    settings: Settings,
    send_message,
    get_interval,
    get_scan_limit,
    get_reasoning_limit,
    get_followup_interval,
    get_last_daily_report_at,
    set_last_daily_report_at,
) -> None:
    await send_message(
        f"Auto monitor started. Live scan is running continuously. Summary updates are sent every {get_interval()} minutes. "
        f"Follow-up checks run every {get_followup_interval()} minutes. "
        f"Tokens with no positive move after {settings.sideline_after_hours}h are parked."
    )
    stats = _new_live_stats()
    queue: asyncio.Queue[TokenCandidate] = asyncio.Queue(maxsize=max(50, get_scan_limit() * 2))
    pending: dict[str, dict[str, object]] = {}
    producer_task = asyncio.create_task(_live_token_producer(settings, pending, stats))
    market_task = asyncio.create_task(_live_market_rechecker(settings, pending, queue, stats))
    analyzer_task = asyncio.create_task(_live_token_analyzer(settings, send_message, get_reasoning_limit, queue, stats))
    try:
        while True:
            try:
                interval_minutes = max(1, get_interval())
                await asyncio.sleep(interval_minutes * 60)
                for task_name, task in (("producer", producer_task), ("market", market_task), ("analyzer", analyzer_task)):
                    if task.done():
                        exc = task.exception()
                        raise RuntimeError(f"Live scan {task_name} stopped: {exc!r}")
                stats["mc_pending_now"] = len(pending)
                LOGGER.info(
                    "Live scan %sm summary: received=%s mc_pending_added=%s mc_pending_now=%s mc_checks=%s mc_retry=%s mc_promoted=%s mc_final_skipped=%s queued=%s analyzed=%s non_pump_skipped=%s market_precheck_skipped=%s market_precheck_errors=%s mc_pending_dropped=%s queue_dropped=%s queue_pending=%s alerts=%s reasoning_used=%s/%s",
                    interval_minutes,
                    stats["received"],
                    stats["mc_pending_added"],
                    stats["mc_pending_now"],
                    stats["mc_checks"],
                    stats["mc_retry"],
                    stats["mc_promoted"],
                    stats["mc_final_skipped"],
                    stats["queued"],
                    stats["analyzed"],
                    stats["non_pump_skipped"],
                    stats["market_precheck_skipped"],
                    stats["market_precheck_errors"],
                    stats["mc_pending_dropped"],
                    stats["queue_dropped"],
                    queue.qsize(),
                    stats["alerts"],
                    stats["reasoning_used"],
                    get_reasoning_limit(),
                )
                await send_message(_format_live_summary(f"Live scan {interval_minutes}m summary", stats, queue.qsize(), get_reasoning_limit()))
                stats.clear()
                stats.update(_new_live_stats())

                followups = await _followup_once(settings, send_message, get_followup_interval())
                if followups:
                    await send_message(f"Follow-up complete. Alerts sent: {followups}.")
                discovered = await _migrated_discovery_once(settings, send_message, get_reasoning_limit)
                if discovered:
                    await send_message(f"Migrated discovery complete. Alerts sent: {discovered}.")
                okx_discovered = await _okx_migrated_discovery_once(settings, send_message, get_reasoning_limit)
                if okx_discovered:
                    await send_message(f"OKX memepump discovery complete. Alerts sent: {okx_discovered}.")
                await _daily_check_once(settings)
                now = int(time.time())
                if _daily_report_due(settings, now, get_last_daily_report_at()):
                    await send_message(_format_daily_report(daily_activity_report(settings.daily_check_interval_hours)))
                    set_last_daily_report_at(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("Auto monitor loop failed")
                await send_message(f"⚠️ Auto monitor failed: <code>{escape(str(exc))}</code>")
                await asyncio.sleep(30)
    finally:
        producer_task.cancel()
        market_task.cancel()
        analyzer_task.cancel()
        with suppress(asyncio.CancelledError):
            await producer_task
        with suppress(asyncio.CancelledError):
            await market_task
        with suppress(asyncio.CancelledError):
            await analyzer_task


async def _migration_loop(settings: Settings, send_message) -> None:
    await send_message("Migration monitor started. Watching bonding-curve completions for alerted tokens.")
    while True:
        try:
            async for candidate in stream_migrations(settings):
                if not should_alert_migration(candidate.mint):
                    continue
                result = await analyze_token(candidate, settings, force_reasoning=True)
                mark_migration_alerted(candidate.mint)
                _log_decision(result, "MIGRATION", "FOLLOWUP")
                await send_message(format_report_html(result, title="🚀 <b>BONDING/MIGRATION UPDATE</b>"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Migration monitor failed")
            await send_message(f"⚠️ Migration monitor failed: <code>{escape(str(exc))}</code>. Reconnecting in 30s.")
            await asyncio.sleep(30)


async def monitor_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    existing = context.application.bot_data.get("monitor_task")
    if existing and not existing.done():
        await update.effective_message.reply_text("Auto monitor is already running.")
        return
    interval = int(context.args[0]) if context.args and context.args[0].isdigit() else settings.monitor_interval_minutes
    chat_id = update.effective_chat.id

    async def send_message(text: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=text[:3900], parse_mode=HTML)

    def get_interval() -> int:
        return int(context.application.bot_data.get("monitor_interval", interval))

    def get_scan_limit() -> int:
        return _runtime_scan_limit(context, settings)

    def get_reasoning_limit() -> int:
        return _runtime_reasoning_limit(context, settings)

    def get_followup_interval() -> int:
        return _runtime_followup_interval(context, settings)

    def get_last_daily_report_at() -> int:
        return int(context.application.bot_data.get("last_daily_report_at", int(time.time())))

    def set_last_daily_report_at(value: int) -> None:
        context.application.bot_data["last_daily_report_at"] = value

    context.application.bot_data.setdefault("last_daily_report_at", 0)
    task = asyncio.create_task(
        _monitor_loop(
            settings,
            send_message,
            get_interval,
            get_scan_limit,
            get_reasoning_limit,
            get_followup_interval,
            get_last_daily_report_at,
            set_last_daily_report_at,
        )
    )
    migration_task = asyncio.create_task(_migration_loop(settings, send_message))
    context.application.bot_data["monitor_task"] = task
    context.application.bot_data["migration_task"] = migration_task
    context.application.bot_data["monitor_interval"] = interval
    await _reply(update, f"▶️ <b>Auto monitor started</b>\nScan: {interval} minutes\nFollow-up: {get_followup_interval()} minutes.")


async def monitor_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await _reply(update, "Usage: <code>/monitor_interval &lt;minutes&gt;</code>")
        return
    interval = max(1, int(context.args[0]))
    context.application.bot_data["monitor_interval"] = interval
    await _reply(
        update,
        f"⏱️ Monitor interval set to <b>{interval} minutes</b>. "
        "This applies after the current scan/sleep cycle. Use /monitor_stop then /monitor_start to apply immediately."
    )


async def followup_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await _reply(update, "Usage: <code>/followup_interval &lt;minutes&gt;</code>")
        return
    interval = max(5, int(context.args[0]))
    context.application.bot_data["followup_interval"] = interval
    await _reply(update, f"🔁 Follow-up interval set to <b>{interval} minutes</b>.")


async def daily_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    await _reply(update, _format_daily_report(daily_activity_report(settings.daily_check_interval_hours)))


async def scan_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await _reply(update, "Usage: <code>/scan_limit &lt;jumlah&gt;</code>")
        return
    limit = max(1, min(200, int(context.args[0])))
    context.application.bot_data["scan_limit"] = limit
    await _reply(update, f"🔢 Analysis cap/window set to <b>{limit}</b>. Monitor window berikutnya akan memakai nilai ini.")


async def reasoning_limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await _reply(update, "Usage: <code>/reasoning_limit &lt;jumlah&gt;</code>")
        return
    limit = max(0, min(50, int(context.args[0])))
    context.application.bot_data["max_reasoning_calls_per_scan"] = limit
    await _reply(update, f"🧠 Max reasoning per scan set to <b>{limit}</b>.")


async def monitor_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    task = context.application.bot_data.get("monitor_task")
    migration_task = context.application.bot_data.get("migration_task")
    if not task or task.done():
        await _reply(update, "Auto monitor is not running.")
        return
    task.cancel()
    if migration_task and not migration_task.done():
        migration_task.cancel()
    await _reply(update, "⏹️ Auto monitor stopped.")


async def monitor_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    task = context.application.bot_data.get("monitor_task")
    migration_task = context.application.bot_data.get("migration_task")
    running = bool(task and not task.done())
    migration_running = bool(migration_task and not migration_task.done())
    interval = context.application.bot_data.get("monitor_interval", settings.monitor_interval_minutes)
    scan_limit = _runtime_scan_limit(context, settings)
    reasoning_limit = _runtime_reasoning_limit(context, settings)
    followup_interval_minutes = _runtime_followup_interval(context, settings)
    await _reply(
        update,
        "📡 <b>Monitor Status</b>\n"
        f"Auto monitor: {running}\n"
        f"Migration monitor: {migration_running}\n"
        f"Interval: {interval} minutes\n"
        f"Follow-up interval: {followup_interval_minutes} minutes\n"
        f"Analysis cap/window: {scan_limit}\n"
        f"Scan timeout: {settings.scan_timeout_seconds}s\n"
        f"Reasoning limit: {reasoning_limit}\n"
        f"Sideline after: {settings.sideline_after_hours}h no positive move\n"
        f"Daily audit/report: {settings.daily_check_interval_hours}h at {settings.daily_report_time}\n"
        f"Min alert: Opp {settings.min_alert_score}, MC ${settings.min_market_cap_usd:,.0f}"
        f"\nMax MC: ${settings.max_market_cap_usd:,.0f}"
    )


async def opportunities(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = top_tokens(limit=10)
    if not rows:
        await _reply(update, "No token memory yet.")
        return
    lines = ["🏆 <b>Top remembered opportunities</b>"]
    for row in rows:
        name = row["symbol"] or row["name"] or row["mint"][:8]
        lines.append(
            f"• <b>{name}</b>: opp {row['best_opportunity_score'] or 0}/100 | trust {row['best_score'] or 0}/100 | "
            f"mcap ${row['first_market_cap'] or 0:,.0f}\n<code>{row['mint']}</code>"
        )
    await _reply(update, "\n".join(lines))


async def learn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    outcomes = alerted_token_outcomes(limit=30)
    if not outcomes:
        await _reply(update, "Belum ada alert dengan outcome yang bisa dipelajari.")
        return
    counts: dict[str, int] = {}
    for item in outcomes:
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    lines = [
        "📚 <b>Learning Summary</b>",
        f"Auto learn: {settings.auto_learn_enabled} | min samples {settings.learn_min_samples} | max adjustment ±{settings.learn_max_adjustment}",
        "Outcome dari token yang pernah alert:",
        " ".join(f"{escape(k)}={v}" for k, v in sorted(counts.items())),
        "",
        "<b>Recent outcomes</b>",
    ]
    for item in outcomes[:8]:
        change = item["change_pct"]
        change_text = "unknown" if change is None else f"{change:+.1f}%"
        peak = item.get("peak_gain_pct")
        peak_text = "unknown" if peak is None else f"{peak:+.1f}%"
        lines.append(
            f"• <b>{escape(str(item['symbol']))}</b> {escape(item['label'])} | peak {peak_text} | now {change_text} | opp {item['best_opportunity_score']}/100"
        )
    learned = learning_summary(settings.learn_min_samples, settings.learn_max_adjustment)
    adjustments = learned["adjustments"]
    lines.append("")
    lines.append("<b>Active learned adjustments</b>")
    if adjustments:
        for item in adjustments[:8]:
            lines.append(
                f"• {escape(item['signal'])}: {item['delta']:+d} | n={item['count']} | win {item['win_rate'] * 100:.0f}% | dump {item['dump_rate'] * 100:.0f}%"
            )
    else:
        lines.append("Belum aktif. Sample belum cukup atau sinyal belum punya lift jelas.")
    lessons = learned["lessons"]
    lines.append("")
    lines.append("<b>Learned lessons</b>")
    if lessons:
        for lesson in lessons[:5]:
            lines.append(f"• {escape(lesson)}")
    else:
        lines.append("Belum ada lesson aktif.")
    sources = learned["source_performance"]
    lines.append("")
    lines.append("<b>Source performance</b>")
    if sources:
        for item in sources[:5]:
            lines.append(
                f"• {escape(item['source'])}: n={item['count']} | win {item['win_rate'] * 100:.0f}% | dump {item['dump_rate'] * 100:.0f}% | avg peak {item['avg_peak']:+.1f}%"
            )
    else:
        lines.append("Belum cukup data source.")
    missed = learned["missed_winners"]
    lines.append("")
    lines.append("<b>Missed winner memory</b>")
    if missed:
        for item in missed[:5]:
            current = "unknown" if item["current"] is None else f"{item['current']:+.1f}%"
            lines.append(f"• {escape(str(item['symbol']))}: peak {item['peak']:+.1f}% | now {current} | opp {item['best_opportunity_score']}/100")
    else:
        lines.append("Belum ada missed winner peak >= 100%.")
    await _reply(update, "\n".join(lines))


async def signals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = signal_performance(limit=200)
    if not rows:
        await _reply(update, "Belum cukup decision/outcome untuk menghitung performa sinyal.")
        return
    lines = ["📈 <b>Signal Performance</b>"]
    for item in rows[:10]:
        lines.append(
            f"• <b>{escape(item['signal'])}</b> | n={item['count']} | peak-win {item['win_rate'] * 100:.0f}% | giveback {item['gave_back_rate'] * 100:.0f}% | dump {item['dump_rate'] * 100:.0f}% | avg peak {item['avg_peak']:+.1f}%"
        )
    await _reply(update, "\n".join(lines))


async def why_score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    if not context.args:
        await _reply(update, "Usage: <code>/why_score &lt;mint&gt;</code>")
        return
    settings: Settings = context.application.bot_data["settings"]
    mint = context.args[0]
    result = await analyze_token(TokenCandidate(mint=mint, source="why_score"), settings, enable_reasoning=False)
    score = result["score"]
    breakdown = opportunity_breakdown(
        result["dex"],
        result["research"],
        result.get("risk"),
        min_market_cap_usd=settings.min_market_cap_usd,
        max_market_cap_usd=settings.max_market_cap_usd,
    )
    lines = [
        f"🧮 <b>Why Score</b> | <code>{escape(mint)}</code>",
        f"<b>Opp:</b> {score.opportunity_score}/100 | <b>Label:</b> {escape(score.opportunity_label)}",
    ]
    for points, reason in breakdown[:14]:
        sign = "+" if points > 0 else ""
        lines.append(f"• <code>{sign}{points}</code> {escape(reason)}")
    await _reply(update, "\n".join(lines))


async def top_peak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = top_peak_tokens(limit=10)
    if not rows:
        await _reply(update, "Belum ada peak data.")
        return
    lines = ["🏔️ <b>Top Peak</b>"]
    for item in rows:
        current = "unknown" if item["current"] is None else f"{item['current']:+.1f}%"
        lines.append(f"• <b>{escape(str(item['symbol']))}</b> | peak {item['peak']:+.1f}% | now {current} | {escape(item['state'])}")
    await _reply(update, "\n".join(lines))


async def narratives_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = narrative_heatmap(hours=24, limit=10)
    if not rows:
        await _reply(update, "Belum cukup data narrative 24h.")
        return
    lines = ["🔥 <b>Narrative Heatmap 24h</b>"]
    for item in rows:
        lines.append(f"• <b>{escape(item['signal'])}</b> | n={item['count']} | win {item['win_rate'] * 100:.0f}% | avg peak {item['avg_peak']:+.1f}%")
    await _reply(update, "\n".join(lines))


async def risk_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = risk_bucket_report(hours=24, limit=10)
    if not rows:
        await _reply(update, "Belum cukup risk data 24h.")
        return
    lines = ["⚠️ <b>Risk Report 24h</b>"]
    for item in rows:
        lines.append(f"• <b>{escape(item['signal'])}</b> | n={item['count']} | win {item['win_rate'] * 100:.0f}% | dump {item['dump_rate'] * 100:.0f}% | avg peak {item['avg_peak']:+.1f}%")
    await _reply(update, "\n".join(lines))


async def missed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = missed_winners(limit=10, min_peak_pct=100)
    if not rows:
        await _reply(update, "Belum ada missed winner peak >= 100%.")
        return
    lines = ["🕳️ <b>Missed Winners</b>"]
    for item in rows:
        current = "unknown" if item["current"] is None else f"{item['current']:+.1f}%"
        lines.append(f"• <b>{escape(str(item['symbol']))}</b> | peak {item['peak']:+.1f}% | now {current} | best opp {item['best_opportunity_score']}/100\n<code>{escape(item['mint'])}</code>")
    await _reply(update, "\n".join(lines))


async def overrange_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings: Settings = context.application.bot_data["settings"]
    rows = overrange_tokens(settings.max_market_cap_usd, limit=10)
    if not rows:
        await _reply(update, "Belum ada overrange token.")
        return
    lines = [f"🟣 <b>Overrange Tracker</b> | max ${settings.max_market_cap_usd:,.0f}"]
    for item in rows:
        lines.append(f"• <b>{escape(str(item['symbol']))}</b> | MC ${item['market_cap'] or 0:,.0f} | best opp {item['best_opportunity_score']}/100")
    await _reply(update, "\n".join(lines))


async def copycats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    rows = copycat_report(hours=24, limit=10)
    if not rows:
        await _reply(update, "Belum ada copycat cluster 24h.")
        return
    lines = ["🧬 <b>Copycat Clusters 24h</b>"]
    for item in rows:
        lines.append(f"• <b>{escape(str(item['name']))}</b> appeared {item['count']}x")
    await _reply(update, "\n".join(lines))


async def _post_init(application) -> None:
    settings: Settings = application.bot_data["settings"]
    if not settings.auto_monitor_enabled:
        return

    chat_id = settings.authorized_telegram_user_id

    async def send_message(text: str) -> None:
        await application.bot.send_message(chat_id=chat_id, text=text[:3900], parse_mode=HTML)

    application.bot_data["monitor_interval"] = settings.monitor_interval_minutes
    application.bot_data["scan_limit"] = settings.scan_limit
    application.bot_data["max_reasoning_calls_per_scan"] = settings.max_reasoning_calls_per_scan
    application.bot_data["followup_interval"] = settings.followup_interval_minutes

    def get_interval() -> int:
        return int(application.bot_data.get("monitor_interval", settings.monitor_interval_minutes))

    def get_scan_limit() -> int:
        return _runtime_scan_limit_app(application, settings)

    def get_reasoning_limit() -> int:
        return _runtime_reasoning_limit_app(application, settings)

    def get_followup_interval() -> int:
        return _runtime_followup_interval_app(application, settings)

    def get_last_daily_report_at() -> int:
        return int(application.bot_data.get("last_daily_report_at", int(time.time())))

    def set_last_daily_report_at(value: int) -> None:
        application.bot_data["last_daily_report_at"] = value

    application.bot_data.setdefault("last_daily_report_at", 0)
    task = asyncio.create_task(
        _monitor_loop(
            settings,
            send_message,
            get_interval,
            get_scan_limit,
            get_reasoning_limit,
            get_followup_interval,
            get_last_daily_report_at,
            set_last_daily_report_at,
        )
    )
    migration_task = asyncio.create_task(_migration_loop(settings, send_message))
    application.bot_data["monitor_task"] = task
    application.bot_data["migration_task"] = migration_task
    application.bot_data["monitor_interval"] = settings.monitor_interval_minutes


def run_bot(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
    if settings.authorized_telegram_user_id is None:
        raise RuntimeError("AUTHORIZED_TELEGRAM_USER_ID is required.")
    app = ApplicationBuilder().token(settings.telegram_bot_token).post_init(_post_init).build()
    app.bot_data["settings"] = settings
    app.bot_data["scan_limit"] = settings.scan_limit
    app.bot_data["max_reasoning_calls_per_scan"] = settings.max_reasoning_calls_per_scan
    app.bot_data["followup_interval"] = settings.followup_interval_minutes
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("token", token_cmd))
    app.add_handler(CommandHandler("deep", deep_cmd))
    app.add_handler(CommandHandler("why", why_cmd))
    app.add_handler(CommandHandler("learn", learn_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))
    app.add_handler(CommandHandler("why_score", why_score_cmd))
    app.add_handler(CommandHandler("top_peak", top_peak_cmd))
    app.add_handler(CommandHandler("narratives", narratives_cmd))
    app.add_handler(CommandHandler("risk_report", risk_report_cmd))
    app.add_handler(CommandHandler("missed", missed_cmd))
    app.add_handler(CommandHandler("overrange", overrange_cmd))
    app.add_handler(CommandHandler("copycats", copycats_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("scan_limit", scan_limit_cmd))
    app.add_handler(CommandHandler("reasoning_limit", reasoning_limit_cmd))
    app.add_handler(CommandHandler("monitor_start", monitor_start))
    app.add_handler(CommandHandler("monitor_interval", monitor_interval))
    app.add_handler(CommandHandler("followup_interval", followup_interval))
    app.add_handler(CommandHandler("daily_report", daily_report_cmd))
    app.add_handler(CommandHandler("monitor_stop", monitor_stop))
    app.add_handler(CommandHandler("monitor_status", monitor_status))
    app.add_handler(CommandHandler("opportunities", opportunities))
    app.run_polling()
