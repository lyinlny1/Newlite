from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_DIR.mkdir(exist_ok=True)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_name: str
    llm_provider_order: list[str]
    llm_timeout_seconds: int
    openrouter_api_key: str
    openrouter_base_url: str
    openrouter_model: str
    openrouter_http_referer: str
    ollama_local_base_url: str
    ollama_local_model: str
    ollama_cloud_api_key: str
    ollama_cloud_base_url: str
    ollama_cloud_model: str
    pumpportal_ws_url: str
    pumpportal_api_key: str
    helius_api_key: str
    okx_api_key: str
    okx_secret_key: str
    okx_passphrase: str
    okx_base_url: str
    okx_wallet_risk_enabled: bool
    okx_monthly_request_limit: int
    okx_migrated_discovery_enabled: bool
    okx_migrated_discovery_limit: int
    okx_migrated_discovery_max_age_minutes: int
    okx_memepump_discovery_stages: list[str]
    okx_memepump_discovery_min_volume_usd: int
    okx_memepump_discovery_min_tx_count: int
    okx_memepump_discovery_min_buy_tx_count: int
    wallet_risk_enabled: bool
    smart_wallet_enabled: bool
    smart_wallet_on_migration_enabled: bool
    sniper_detection_enabled: bool
    bundle_detection_enabled: bool
    enable_free_web_research: bool
    web_search_max_results: int
    telegram_bot_token: str
    authorized_telegram_user_id: int | None
    min_alert_score: int
    min_market_cap_usd: int
    max_market_cap_usd: int
    newlite_agent_enabled: bool
    newlite_agent_min_opportunity_score: int
    max_reasoning_calls_per_scan: int
    auto_learn_enabled: bool
    learn_min_samples: int
    learn_max_adjustment: int
    dev_sold_check_enabled: bool
    dev_sold_check_seconds: int
    hermes_official_enabled: bool
    hermes_cli_command: str
    hermes_cli_provider: str
    hermes_cli_model: str
    hermes_cli_timeout_seconds: int
    scan_timeout_seconds: int
    scan_limit: int
    auto_monitor_enabled: bool
    monitor_interval_minutes: int
    followup_interval_minutes: int
    sideline_after_hours: int
    daily_check_interval_hours: int
    daily_report_time: str
    migrated_discovery_enabled: bool
    migrated_discovery_limit: int


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    providers = [
        item.strip().lower()
        for item in os.getenv("LLM_PROVIDER_ORDER", "openrouter,ollama_cloud,ollama_local").split(",")
        if item.strip()
    ]
    user_id = os.getenv("AUTHORIZED_TELEGRAM_USER_ID", "").strip()
    return Settings(
        app_name=os.getenv("APP_NAME", "Newlite Research"),
        llm_provider_order=providers,
        llm_timeout_seconds=_int(os.getenv("LLM_TIMEOUT_SECONDS"), 45),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "").strip(),
        openrouter_http_referer=os.getenv("OPENROUTER_HTTP_REFERER", "").strip(),
        ollama_local_base_url=os.getenv("OLLAMA_LOCAL_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        ollama_local_model=os.getenv("OLLAMA_LOCAL_MODEL", "llama3.1:8b").strip(),
        ollama_cloud_api_key=os.getenv("OLLAMA_CLOUD_API_KEY", "").strip(),
        ollama_cloud_base_url=os.getenv("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/v1").rstrip("/"),
        ollama_cloud_model=os.getenv("OLLAMA_CLOUD_MODEL", "").strip(),
        pumpportal_ws_url=os.getenv("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data").strip(),
        pumpportal_api_key=os.getenv("PUMPPORTAL_API_KEY", "").strip(),
        helius_api_key=os.getenv("HELIUS_API_KEY", "").strip(),
        okx_api_key=os.getenv("OKX_API_KEY", "").strip(),
        okx_secret_key=os.getenv("OKX_SECRET_KEY", "").strip(),
        okx_passphrase=os.getenv("OKX_PASSPHRASE", "").strip(),
        okx_base_url=os.getenv("OKX_BASE_URL", "https://web3.okx.com").rstrip("/"),
        okx_wallet_risk_enabled=_bool(os.getenv("OKX_WALLET_RISK_ENABLED"), False),
        okx_monthly_request_limit=_int(os.getenv("OKX_MONTHLY_REQUEST_LIMIT"), 999_990),
        okx_migrated_discovery_enabled=_bool(os.getenv("OKX_MIGRATED_DISCOVERY_ENABLED"), True),
        okx_migrated_discovery_limit=_int(os.getenv("OKX_MIGRATED_DISCOVERY_LIMIT"), 20),
        okx_migrated_discovery_max_age_minutes=_int(os.getenv("OKX_MIGRATED_DISCOVERY_MAX_AGE_MINUTES"), 180),
        okx_memepump_discovery_stages=[
            item.strip().upper()
            for item in os.getenv("OKX_MEMEPUMP_DISCOVERY_STAGES", "NEW,MIGRATING,MIGRATED").split(",")
            if item.strip()
        ],
        okx_memepump_discovery_min_volume_usd=_int(os.getenv("OKX_MEMEPUMP_DISCOVERY_MIN_VOLUME_USD"), 1_000),
        okx_memepump_discovery_min_tx_count=_int(os.getenv("OKX_MEMEPUMP_DISCOVERY_MIN_TX_COUNT"), 10),
        okx_memepump_discovery_min_buy_tx_count=_int(os.getenv("OKX_MEMEPUMP_DISCOVERY_MIN_BUY_TX_COUNT"), 5),
        wallet_risk_enabled=_bool(os.getenv("WALLET_RISK_ENABLED"), False),
        smart_wallet_enabled=_bool(os.getenv("SMART_WALLET_ENABLED"), False),
        smart_wallet_on_migration_enabled=_bool(os.getenv("SMART_WALLET_ON_MIGRATION_ENABLED"), True),
        sniper_detection_enabled=_bool(os.getenv("SNIPER_DETECTION_ENABLED"), False),
        bundle_detection_enabled=_bool(os.getenv("BUNDLE_DETECTION_ENABLED"), False),
        enable_free_web_research=_bool(os.getenv("ENABLE_FREE_WEB_RESEARCH"), True),
        web_search_max_results=_int(os.getenv("WEB_SEARCH_MAX_RESULTS"), 6),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        authorized_telegram_user_id=int(user_id) if user_id.isdigit() else None,
        min_alert_score=_int(os.getenv("MIN_ALERT_SCORE"), 20),
        min_market_cap_usd=_int(os.getenv("MIN_MARKET_CAP_USD"), 25_000),
        max_market_cap_usd=_int(os.getenv("MAX_MARKET_CAP_USD"), 200_000),
        newlite_agent_enabled=_bool(os.getenv("NEWLITE_AGENT_ENABLED"), True),
        newlite_agent_min_opportunity_score=_int(os.getenv("NEWLITE_AGENT_MIN_OPPORTUNITY_SCORE"), 45),
        max_reasoning_calls_per_scan=_int(os.getenv("MAX_REASONING_CALLS_PER_SCAN"), 5),
        auto_learn_enabled=_bool(os.getenv("AUTO_LEARN_ENABLED"), True),
        learn_min_samples=_int(os.getenv("LEARN_MIN_SAMPLES"), 20),
        learn_max_adjustment=_int(os.getenv("LEARN_MAX_ADJUSTMENT"), 10),
        dev_sold_check_enabled=_bool(os.getenv("DEV_SOLD_CHECK_ENABLED"), False),
        dev_sold_check_seconds=_int(os.getenv("DEV_SOLD_CHECK_SECONDS"), 6),
        hermes_official_enabled=_bool(os.getenv("HERMES_OFFICIAL_ENABLED"), False),
        hermes_cli_command=os.getenv("HERMES_CLI_COMMAND", "hermes").strip(),
        hermes_cli_provider=os.getenv("HERMES_CLI_PROVIDER", "").strip(),
        hermes_cli_model=os.getenv("HERMES_CLI_MODEL", "").strip(),
        hermes_cli_timeout_seconds=_int(os.getenv("HERMES_CLI_TIMEOUT_SECONDS"), 120),
        scan_timeout_seconds=_int(os.getenv("SCAN_TIMEOUT_SECONDS"), 60),
        scan_limit=_int(os.getenv("SCAN_LIMIT"), 30),
        auto_monitor_enabled=_bool(os.getenv("AUTO_MONITOR_ENABLED"), False),
        monitor_interval_minutes=_int(os.getenv("MONITOR_INTERVAL_MINUTES"), 10),
        followup_interval_minutes=_int(os.getenv("FOLLOWUP_INTERVAL_MINUTES"), 30),
        sideline_after_hours=_int(os.getenv("SIDELINE_AFTER_HOURS"), 2),
        daily_check_interval_hours=_int(os.getenv("DAILY_CHECK_INTERVAL_HOURS"), 24),
        daily_report_time=os.getenv("DAILY_REPORT_TIME", "13:00").strip(),
        migrated_discovery_enabled=_bool(os.getenv("MIGRATED_DISCOVERY_ENABLED"), True),
        migrated_discovery_limit=_int(os.getenv("MIGRATED_DISCOVERY_LIMIT"), 20),
    )
