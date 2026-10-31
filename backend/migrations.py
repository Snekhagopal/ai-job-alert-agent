"""Small, ordered, idempotent schema migrations.

``Base.metadata.create_all`` creates missing tables but never alters existing ones, so columns
added after a database was first created are added here. Rules:
  * append new migrations to MIGRATIONS; never edit or reorder applied ones
  * every statement must be safe to re-run (ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS)
  * a fresh database gets the columns from create_all; the migration is then a no-op
Applied ids are recorded in ``schema_migrations``. A Postgres advisory lock serialises
concurrent startups (e.g. two backend replicas).
"""
from __future__ import annotations

import logging
from typing import Callable, Union

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

_LOCK_ID = 7_301_995  # arbitrary, constant advisory-lock key for this app

MIGRATIONS: list[tuple[str, list[Union[str, Callable]]]] = [
    ("2026_10_31_01_source_check_not_modified", [
        "ALTER TABLE source_checks ADD COLUMN IF NOT EXISTS not_modified BOOLEAN NOT NULL DEFAULT FALSE",
    ]),
    ("2026_10_31_02_source_check_note", [
        "ALTER TABLE source_checks ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT ''",
    ]),
    ("2026_10_31_03_cross_portal_dedupe_v2", [
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS fingerprint_v2 VARCHAR(64)",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_company VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_title VARCHAR(512) NOT NULL DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dedupe_city VARCHAR(64) NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS ix_jobs_fingerprint_v2 ON jobs (fingerprint_v2)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_dedupe_company ON jobs (dedupe_company)",
        "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS fingerprint_v2 VARCHAR(64)",
        lambda conn: _backfill_dedupe_v2(conn),
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_fingerprint_v2 ON notifications (fingerprint_v2) "
        "WHERE fingerprint_v2 IS NOT NULL",
    ]),
    ("2026_10_31_04_source_monitoring", [
        "ALTER TABLE source_state ADD COLUMN IF NOT EXISTS monitoring_since TIMESTAMPTZ",
        "UPDATE source_state st SET monitoring_since = COALESCE("
        " (SELECT min(c.started_at) FROM source_checks c WHERE c.source_name = st.name AND c.success),"
        " st.last_success_at) WHERE monitoring_since IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_source_checks_source_started ON source_checks (source_name, started_at)",
    ]),
]


def _backfill_dedupe_v2(conn) -> None:
    """Compute v2 keys for existing jobs. Each already-sent notification gets its job's v2
    fingerprint (only the earliest one per v2 value, so the unique index can be built), which
    blocks any future cross-portal copy of an already-emailed job from being emailed again."""
    from backend.database import dedupe_keys, job_fingerprint_v2

    rows = conn.execute(text("SELECT id, company, title, location FROM jobs WHERE fingerprint_v2 IS NULL")).all()
    for jid, company, title, location in rows:
        ck, tk, city = dedupe_keys(company, title, location)
        conn.execute(text("UPDATE jobs SET fingerprint_v2=:fp, dedupe_company=:c, dedupe_title=:t, dedupe_city=:city "
                          "WHERE id=:id"),
                     {"fp": job_fingerprint_v2(company, title, location), "c": ck, "t": tk, "city": city, "id": jid})
    conn.execute(text(
        "UPDATE notifications n SET fingerprint_v2 = j.fingerprint_v2 FROM jobs j "
        "WHERE n.job_id = j.id AND n.fingerprint_v2 IS NULL AND j.fingerprint_v2 IS NOT NULL "
        "AND n.id = (SELECT min(n2.id) FROM notifications n2 JOIN jobs j2 ON j2.id = n2.job_id "
        "            WHERE j2.fingerprint_v2 = j.fingerprint_v2)"
    ))
    log.info("dedupe v2 backfill: %d jobs", len(rows))


def apply_migrations(engine: Engine) -> list[str]:
    applied_now: list[str] = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_ID})
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " id VARCHAR(128) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        ))
        done = set(conn.scalars(text("SELECT id FROM schema_migrations")))
        for mig_id, statements in MIGRATIONS:
            if mig_id in done:
                continue
            for stmt in statements:
                if callable(stmt):  # data backfill written in Python
                    stmt(conn)
                else:
                    conn.execute(text(stmt))
            conn.execute(text("INSERT INTO schema_migrations (id) VALUES (:id)"), {"id": mig_id})
            applied_now.append(mig_id)
            log.info("applied migration %s", mig_id)
    return applied_now
