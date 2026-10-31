import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Tests run against a real PostgreSQL database (TEST_DATABASE_URL); they never send email or call an LLM.
os.environ.update(
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://localhost/jobagent_test"),
    BACKEND_API_KEY="test-key-0123456789abcdefghijkl",
    LLM_PROVIDER="none",
    DRY_RUN_EMAIL="true",
    NOTIFY_EMAIL_TO="me@example.com",
    GMAIL_CLIENT_ID="", GMAIL_CLIENT_SECRET="", GMAIL_REFRESH_TOKEN="",
    SOURCES_CONFIG_PATH=str(ROOT / "tests/fixtures/sources_test.yaml"),
    HEALTHCHECKS_PING_URL="",
    ADMIN_ALERTS_ENABLED="false",
    PROCESSING_WORKER_ENABLED="false",
)

from backend import config, database  # noqa: E402


@pytest.fixture
def db():
    config.get_settings.cache_clear()
    database.reset_engine()
    engine = database.get_engine()
    database.Base.metadata.drop_all(engine)
    database.Base.metadata.create_all(engine)
    from backend import http_client, job_sources
    job_sources.reset_etag_cache()
    yield
    http_client.reset_shared_client()
    database.reset_engine()


@pytest.fixture
def settings(monkeypatch):
    """Override settings for one test: settings(min_match_score=70)."""
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k.upper(), str(v))
        config.get_settings.cache_clear()
        return config.get_settings()
    yield _set
    config.get_settings.cache_clear()
