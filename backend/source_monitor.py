"""Source monitoring report for the dashboard: method, frequency, last check, jobs found,
availability and measured detection delay for every source, plus sources that are NOT monitored
and why.

Detection delay is only computed for jobs published AFTER monitoring of that source started
(``source_state.monitoring_since``); otherwise the first check of a board would report every old
posting as "detected weeks late". What it measures depends on the source:
  * ATS / aggregator / RSS: first detected  -  publication time reported by the source
  * Workday: not computed (Workday reports the posting date only, not the time)
  * email alerts: first detected  -  arrival of the alert email. When the platform itself
    published the job is not in the email, so the platform's own delay is unknown.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import text

from backend.config import get_settings
from backend.database import SourceState, session_scope, utcnow
from backend.job_sources import EMAIL_PARSER_VERIFIED, JobSource, load_sources

# kind -> (detection class, realistic publication-to-email delay, can meet a 5-10 min target)
CAPABILITY: dict[str, tuple[str, str, bool]] = {
    "greenhouse": ("near-real-time", "~2–4 min (2-min poll; publish time reported)", True),
    "lever": ("near-real-time", "~2–4 min (2-min poll; Lever reports creation time)", True),
    "ashby": ("near-real-time", "~2–4 min (2-min poll; publish time reported)", True),
    "workday": ("near-real-time", "~5–7 min (5-min poll; posting DATE only, delay not measurable)", True),
    "adzuna": ("delayed (aggregator)", "~30–90 min", False),
    "jooble": ("delayed (aggregator)", "~30–90 min; no publish time", False),
    "rss": ("feed dependent", "feed dependent", False),
    "email": ("delayed (platform schedule)", "1–2 min after the alert email arrives; platforms usually "
              "send alerts about once a day, so the 5–10 min target cannot be met", False),
}

NOT_INTEGRABLE = [
    {"source": "Google Jobs", "reason": "No public search API; third-party Google scrapers violate Google's terms. "
                                        "Google Jobs is built from company career pages, which are monitored directly."},
    {"source": "SmartRecruiters", "reason": "robots.txt disallows all bots except LinkedInBot."},
    {"source": "Remotive", "reason": "robots.txt disallows /api/*; its feed is delayed 24 h."},
    {"source": "Workable / Recruitee", "reason": "Checked on 2026-10-31: no Indian AI/ML company boards with open jobs."},
]

ALLOWED_GAP_MIN_MINUTES = 10
# A maintenance window longer than this is reported (and alerted) as a failure.
MAX_MAINTENANCE = timedelta(hours=12)


def _source_key_sql() -> str:
    """SQL expression mapping a jobs row to the configured source name it came from."""
    return ("CASE WHEN j.source LIKE 'email:%' THEN j.source "
            "WHEN j.source IN ('greenhouse','lever','ashby','workday') THEN j.source || ':' || split_part(j.source_job_id, ':', 1) "
            "WHEN j.source = 'rss' THEN 'rss:' || split_part(j.source_job_id, ':', 2) "
            "ELSE j.source END")


def _job_stats(s) -> dict[str, dict]:
    key = _source_key_sql()
    rows = s.execute(text(f"""
        SELECT {key} AS name,
               count(*) AS total,
               count(*) FILTER (WHERE j.first_detected_at > now() - interval '24 hours') AS d24,
               count(*) FILTER (WHERE j.first_detected_at > now() - interval '7 days') AS d7,
               count(*) FILTER (WHERE j.pipeline_status = 'notified') AS notified,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM j.first_detected_at - j.posted_at))
                 FILTER (WHERE j.posted_at IS NOT NULL AND j.posted_at >= st.monitoring_since
                         AND j.first_detected_at > now() - interval '7 days'
                         AND (j.raw->>'posted_precision') IS DISTINCT FROM 'day') AS pub_p50,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM j.first_detected_at - j.posted_at))
                 FILTER (WHERE j.posted_at IS NOT NULL AND j.posted_at >= st.monitoring_since
                         AND j.first_detected_at > now() - interval '7 days'
                         AND (j.raw->>'posted_precision') IS DISTINCT FROM 'day') AS pub_p90,
               count(*) FILTER (WHERE j.posted_at IS NOT NULL AND j.posted_at >= st.monitoring_since
                         AND j.first_detected_at > now() - interval '7 days'
                         AND (j.raw->>'posted_precision') IS DISTINCT FROM 'day') AS pub_n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM
                         j.first_detected_at - (j.raw->>'alert_received_at')::timestamptz))
                 FILTER (WHERE j.source LIKE 'email:%' AND (j.raw->>'alert_received_at') IS NOT NULL
                         AND (j.raw->>'alert_received_at')::timestamptz >= st.monitoring_since
                         AND j.first_detected_at > now() - interval '7 days') AS mail_p50,
               count(*) FILTER (WHERE j.source LIKE 'email:%' AND (j.raw->>'alert_received_at') IS NOT NULL
                         AND (j.raw->>'alert_received_at')::timestamptz >= st.monitoring_since
                         AND j.first_detected_at > now() - interval '7 days') AS mail_n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM n.sent_at - j.posted_at))
                 FILTER (WHERE n.sent_at IS NOT NULL AND j.posted_at IS NOT NULL AND j.posted_at >= st.monitoring_since
                         AND (j.raw->>'posted_precision') IS DISTINCT FROM 'day') AS notify_p50
        FROM jobs j
        LEFT JOIN source_state st ON st.name = {key}
        LEFT JOIN notifications n ON n.job_id = j.id AND n.status = 'sent'
        GROUP BY 1
    """)).mappings().all()
    return {r["name"]: dict(r) for r in rows}


def _check_stats(s) -> dict[str, dict]:
    rows = s.execute(text("""
        SELECT source_name AS name,
               count(*) FILTER (WHERE started_at > now() - interval '24 hours') AS checks_24h,
               count(*) FILTER (WHERE started_at > now() - interval '24 hours' AND success) AS ok_24h,
               count(*) FILTER (WHERE started_at > now() - interval '24 hours' AND not_modified) AS not_modified_24h,
               (array_agg(note ORDER BY started_at DESC) FILTER (WHERE note <> ''))[1] AS last_note,
               max(started_at) FILTER (WHERE note <> '') AS last_note_at
        FROM source_checks
        WHERE started_at > now() - interval '7 days'
        GROUP BY source_name
    """)).mappings().all()
    return {r["name"]: dict(r) for r in rows}


def _fmt_seconds(sec: Optional[float]) -> Optional[str]:
    if sec is None:
        return None
    sec = max(float(sec), 0.0)
    if sec < 90:
        return f"{sec:.0f} s"
    if sec < 2 * 3600:
        return f"{sec / 60:.0f} min"
    return f"{sec / 3600:.1f} h"


def _delay(src: JobSource, js: dict) -> tuple[Optional[str], str]:
    if src.kind == "workday":
        return None, "not measurable: Workday reports the posting date only"
    if src.kind == "email":
        if js.get("mail_n"):
            return _fmt_seconds(js["mail_p50"]), (f"median after the alert email arrived ({js['mail_n']} jobs, 7 d); "
                                                  "the platform's own delay before emailing is unknown")
        return None, "no alert emails since monitoring started"
    if src.kind == "jooble":
        return None, "not measurable: Jooble gives no publication time"
    if js.get("pub_n"):
        return _fmt_seconds(js["pub_p50"]), (f"median from publication to detection ({js['pub_n']} jobs, 7 d; "
                                             f"p90 {_fmt_seconds(js['pub_p90'])})")
    return None, "no jobs published since monitoring started yet"


def source_report() -> dict:
    sources = load_sources()
    now = utcnow()
    with session_scope() as s:
        states = {st.name: st for st in s.query(SourceState)}
        jobs = _job_stats(s)
        checks = _check_stats(s)
        s.expunge_all()
    out = []
    for src in sources:
        st = states.get(src.name)
        js, cs = jobs.get(src.name, {}), checks.get(src.name, {})
        allowed_gap = timedelta(minutes=max(5 * src.interval_minutes, ALLOWED_GAP_MIN_MINUTES))
        healthy = bool(st and st.last_success_at and now - st.last_success_at <= allowed_gap)
        unavailable = bool(st and st.last_error.startswith("unavailable:") and not healthy
                           and (st.last_success_at is None or now - st.last_success_at <= MAX_MAINTENANCE))
        cls, expected, realtime = CAPABILITY.get(src.kind, ("unknown", "unknown", False))
        delay, delay_basis = _delay(src, js)
        checks_24h = cs.get("checks_24h") or 0
        out.append({
            # original fields (kept for n8n's hourly health check)
            "source": src.name, "kind": src.kind, "interval_minutes": src.interval_minutes,
            "last_run_at": st.last_run_at.isoformat() if st and st.last_run_at else None,
            "last_success_at": st.last_success_at.isoformat() if st and st.last_success_at else None,
            "consecutive_failures": st.consecutive_failures if st else 0,
            "last_error": st.last_error if st else "",
            "healthy": healthy,
            # monitoring dashboard
            "method": src.method,
            "availability": ("temporarily unavailable" if unavailable else "available" if healthy
                             else "never checked" if not st else "failing / stale"),
            "availability_24h_pct": round(100 * (cs.get("ok_24h") or 0) / checks_24h, 1) if checks_24h else None,
            "checks_24h": checks_24h,
            "unchanged_24h": cs.get("not_modified_24h") or 0,
            "jobs_24h": js.get("d24", 0), "jobs_7d": js.get("d7", 0), "jobs_total": js.get("total", 0),
            "notified_total": js.get("notified", 0),
            "detection_delay": delay, "detection_delay_basis": delay_basis,
            "publish_to_email_median": _fmt_seconds(js.get("notify_p50")) if src.kind != "email" else None,
            "detection_class": cls, "expected_delay": expected, "meets_5_10_min_target": realtime,
            "monitoring_since": st.monitoring_since.isoformat() if st and st.monitoring_since else None,
            "parser_verified": (src.name.split(":", 1)[1] in EMAIL_PARSER_VERIFIED) if src.kind == "email" else None,
            "last_note": cs.get("last_note") or "",
        })
    return {"sources": out, "not_monitored": not_monitored()}


def not_monitored() -> list[dict]:
    cfg = yaml.safe_load(Path(get_settings().sources_config_path).read_text()) or {}
    rows = []
    for platform, c in (cfg.get("email_alerts") or {}).items():
        if not (c or {}).get("enabled"):
            rows.append({"source": f"email:{platform}", "reason": "disabled: awaiting a real alert-email sample "
                                                                  "(docs/EMAIL_ALERTS.md)"})
    wd = cfg.get("workday") or {}
    if wd.get("tenants") and not wd.get("enabled"):
        rows.append({"source": "workday", "reason": "disabled in sources.yaml (workday.enabled: false)"})
    for kind in ("adzuna", "jooble"):
        if cfg.get(kind) and not cfg[kind].get("enabled"):
            rows.append({"source": kind, "reason": f"disabled: needs a free API key ({kind.upper()}_* in .env)"})
    return rows + NOT_INTEGRABLE
