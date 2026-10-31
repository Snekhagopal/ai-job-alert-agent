"""Central configuration loaded from environment variables (.env in local dev)."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Core ---
    database_url: str = "postgresql+psycopg://jobagent:jobagent@db:5432/jobagent"
    backend_api_key: str = ""  # required in production; checked at startup
    sources_config_path: str = "config/sources.yaml"
    log_level: str = "INFO"

    # --- Preferences ---
    max_experience_years: float = 2.0
    min_match_score: int = 60
    job_max_age_days: int = 21  # postings older than this (when the source gives a date) are treated as stale

    # --- LLM ---
    llm_provider: Literal["gemini", "openai", "none"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    max_llm_calls_per_day: int = 150
    llm_timeout_seconds: float = 60.0

    # --- Gmail (OAuth2 installed-app credentials of YOUR Google account) ---
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    gmail_refresh_token: str = ""
    notify_email_to: str = ""  # where match alerts are sent
    admin_alerts_enabled: bool = True  # email yourself when a source keeps failing
    dry_run_email: bool = False  # log emails instead of sending

    # --- Keyed job APIs (optional) ---
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""

    # --- Scheduling ---
    # n8n is the primary scheduler (a 1-minute tick). Enable this only if you run without n8n.
    internal_scheduler_enabled: bool = False
    cycle_interval_seconds: int = 60
    # Max seconds one processing pass (AI scoring + email) runs before yielding.
    cycle_time_budget_seconds: int = 240
    # Background worker that scores + emails jobs as soon as discovery stores them.
    processing_worker_enabled: bool = True
    # Optional dead-man's switch (e.g. https://hc-ping.com/<uuid>): pinged after every
    # completed cycle, so you get alerted by an EXTERNAL service if the VM/n8n/backend dies.
    healthchecks_ping_url: str = ""

    # --- HTTP politeness ---
    http_user_agent: str = "ai-job-alert-agent/1.0 (personal, non-commercial job alert bot)"
    http_min_delay_seconds: float = 1.0
    http_timeout_seconds: float = 20.0

    @property
    def gmail_configured(self) -> bool:
        return bool(self.gmail_client_id and self.gmail_client_secret and self.gmail_refresh_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def setup_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
