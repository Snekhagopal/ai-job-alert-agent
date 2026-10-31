"""FastAPI backend: monitoring trigger, resume upload, job/tracker API, health."""
from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, text

from backend import email_service, pipeline, resume_parser, source_monitor
from backend.config import get_settings, setup_logging
from backend.database import (
    APPLICATION_STATUSES,
    Job,
    LLMUsage,
    Notification,
    Resume,
    SourceCheck,
    count_by,
    get_active_resume,
    init_db,
    session_scope,
    utcnow,
)
from backend.job_sources import load_sources

setup_logging()
log = logging.getLogger("backend")
MAX_RESUME_BYTES = 5 * 1024 * 1024


def _scheduler_loop(stop: threading.Event) -> None:
    interval = get_settings().cycle_interval_seconds
    while not stop.is_set():
        try:
            pipeline.run_background_tick()
        except Exception:
            log.exception("scheduled tick failed")
        stop.wait(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.backend_api_key or len(s.backend_api_key) < 24:
        raise RuntimeError("BACKEND_API_KEY must be set to a random string of at least 24 characters")
    for attempt in range(10):  # the database container may still be starting
        try:
            init_db()
            break
        except Exception as exc:
            log.warning("database not ready (%s), retrying in 3s", exc)
            time.sleep(3)
    else:
        raise RuntimeError("could not connect to the database")
    load_sources()  # fail fast on a broken sources.yaml
    stop = threading.Event()
    if s.processing_worker_enabled:
        threading.Thread(target=pipeline.processing_worker_loop, args=(stop,), daemon=True, name="processing").start()
        log.info("processing worker enabled")
    if s.internal_scheduler_enabled:
        threading.Thread(target=_scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()
        log.info("internal scheduler enabled (every %ss)", s.cycle_interval_seconds)
    yield
    stop.set()
    pipeline.wake_processing()  # let the worker see the stop flag


app = FastAPI(title="AI Job Alert Agent", version="1.0.0", lifespan=lifespan)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_api_key, get_settings().backend_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


auth = [Depends(require_api_key)]


# ---------------------------------------------------------------- health


@app.get("/health")
def health():
    """Unauthenticated liveness/readiness probe (no sensitive data)."""
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            last = s.scalar(select(func.max(SourceCheck.finished_at)))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {type(exc).__name__}")
    stale = last is None or utcnow() - last > timedelta(minutes=20)
    return {"status": "ok", "db": "ok", "last_source_check": last.isoformat() if last else None,
            "monitoring_stale": stale}


@app.get("/api/health/sources", dependencies=auth)
def source_health():
    """Per-source monitoring status (method, frequency, last check, jobs found, availability,
    detection delay) plus the sources that are not monitored and why."""
    report = source_monitor.source_report()
    sources = report["sources"]
    alertable = [s for s in sources if not s["healthy"] and s["availability"] != "temporarily unavailable"]
    return {"healthy": all(s["healthy"] for s in sources), "unhealthy_count": sum(not s["healthy"] for s in sources),
            # unhealthy, excluding provider maintenance windows shorter than 12 h (used by n8n's hourly alert)
            "alertable_unhealthy_count": len(alertable),
            "sources": sources, "not_monitored": report["not_monitored"]}


# ---------------------------------------------------------------- monitoring


@app.post("/api/run-cycle", dependencies=auth)
def run_cycle(
    force: bool = False,
    only: Optional[str] = Query(default=None, description="comma-separated source names/kinds"),
    background: bool = Query(default=False, description="start due sources and return immediately; "
                                                       "scoring/email run in the background worker"),
):
    only_list = only.split(",") if only else None
    if background:
        summary = pipeline.run_background_tick(force=force, only=only_list)
        if not get_settings().processing_worker_enabled:
            threading.Thread(target=pipeline.process_pending, daemon=True, name="processing-once").start()
        return {"mode": "background", **summary}
    try:
        return pipeline.run_cycle(force=force, only=only_list)
    except pipeline.CycleAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class AdminAlert(BaseModel):
    subject: str
    body: str


ADMIN_ALERT_REPEAT_SECONDS = 30 * 60
_admin_alert_last: dict[str, float] = {}


@app.post("/api/admin-alert", dependencies=auth)
def admin_alert(alert: AdminAlert):
    # n8n ticks every minute; without this a persistent backend error would email you every minute.
    now = time.time()
    if now - _admin_alert_last.get(alert.subject, 0.0) < ADMIN_ALERT_REPEAT_SECONDS:
        return {"sent": False, "reason": "same alert sent less than 30 minutes ago"}
    email_service.send_admin_alert(alert.subject[:200], alert.body[:10000])
    _admin_alert_last[alert.subject] = now
    return {"sent": True}


@app.post("/api/test-email", dependencies=auth)
def test_email():
    """Sends a sample notification to NOTIFY_EMAIL_TO to verify Gmail API setup. Nothing is stored."""
    sample = Job(
        id=0, source="test", source_job_id="test", url="https://example.com/job/123",
        apply_url="https://example.com/job/123/apply", title="Junior Machine Learning Engineer (TEST EMAIL)",
        company="Example AI Labs", location="Chennai, India", experience_text="0-2 years", salary="Not disclosed",
        posted_at=None, first_detected_at=utcnow(), limited_details=False, fingerprint="test", match_score=82,
        match_details={"method": "llm", "matching_skills": ["Python", "PyTorch"], "missing_skills": ["Kubernetes"],
                       "reasons": ["This is a test message from your job agent"]},
    )
    try:
        message_id = email_service.send_job_notification(sample)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"email failed: {exc}")
    return {"sent": True, "gmail_message_id": message_id}


# ---------------------------------------------------------------- resume


@app.post("/api/resume", dependencies=auth)
async def upload_resume(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > MAX_RESUME_BYTES:
        raise HTTPException(status_code=413, detail="resume larger than 5 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="please upload a PDF file")
    try:
        text_ = resume_parser.extract_pdf_text(data)
    except resume_parser.ResumeParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    profile = resume_parser.build_profile(text_)
    with session_scope() as s:
        for r in s.scalars(select(Resume).where(Resume.is_active.is_(True))):
            r.is_active = False
        resume = Resume(filename=(file.filename or "resume.pdf")[:255], raw_text=text_, profile=profile, is_active=True)
        s.add(resume)
        s.flush()
        return {"id": resume.id, "filename": resume.filename, "profile": profile}


@app.get("/api/resume", dependencies=auth)
def get_resume():
    with session_scope() as s:
        r = get_active_resume(s)
        if not r:
            raise HTTPException(status_code=404, detail="no resume uploaded yet")
        return {"id": r.id, "filename": r.filename, "uploaded_at": r.uploaded_at.isoformat(), "profile": r.profile}


# ---------------------------------------------------------------- jobs / tracker


def _job_dict(j: Job, full: bool = False) -> dict:
    d = {
        "id": j.id, "source": j.source, "title": j.title, "company": j.company, "location": j.location,
        "is_remote": j.is_remote, "experience_text": j.experience_text, "salary": j.salary,
        "url": j.url, "apply_url": j.apply_url,
        "posted_at": j.posted_at.isoformat() if j.posted_at else None,
        "first_detected_at": j.first_detected_at.isoformat(),
        "pipeline_status": j.pipeline_status, "filter_reason": j.filter_reason,
        "match_score": j.match_score, "application_status": j.application_status, "notes": j.notes,
        "limited_details": j.limited_details,
    }
    if full:
        d.update(description=j.description, match_details=j.match_details, raw=j.raw)
    else:
        md = j.match_details or {}
        d.update(matching_skills=md.get("matching_skills", []), missing_skills=md.get("missing_skills", []))
    return d


@app.get("/api/jobs", dependencies=auth)
def list_jobs(
    pipeline_status: Optional[str] = None,
    application_status: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(default=200, le=2000),
    offset: int = 0,
):
    with session_scope() as s:
        stmt = select(Job)
        if pipeline_status:
            stmt = stmt.where(Job.pipeline_status.in_(pipeline_status.split(",")))
        if application_status:
            stmt = stmt.where(Job.application_status.in_(application_status.split(",")))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Job.title.ilike(like) | Job.company.ilike(like))
        rows = s.scalars(stmt.order_by(Job.first_detected_at.desc()).limit(limit).offset(offset)).all()
        return [_job_dict(j) for j in rows]


@app.get("/api/jobs/{job_id}", dependencies=auth)
def get_job(job_id: int):
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(status_code=404, detail="job not found")
        return _job_dict(j, full=True)


class JobUpdate(BaseModel):
    application_status: Optional[str] = None
    notes: Optional[str] = None


@app.patch("/api/jobs/{job_id}", dependencies=auth)
def update_job(job_id: int, update: JobUpdate):
    if update.application_status and update.application_status not in APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"application_status must be one of {APPLICATION_STATUSES}")
    with session_scope() as s:
        j = s.get(Job, job_id)
        if not j:
            raise HTTPException(status_code=404, detail="job not found")
        if update.application_status:
            j.application_status = update.application_status
            j.status_updated_at = utcnow()
        if update.notes is not None:
            j.notes = update.notes[:5000]
        return _job_dict(j)


@app.get("/api/stats", dependencies=auth)
def stats():
    with session_scope() as s:
        total = s.scalar(select(func.count(Job.id)))
        by_pipeline = dict(count_by(s, Job.pipeline_status))
        by_application = dict(count_by(s, Job.application_status))
        relevant_q = Job.pipeline_status.in_(["relevant", "notified", "low_match"])
        by_company = s.execute(select(Job.company, func.count()).where(relevant_q).group_by(Job.company)
                               .order_by(func.count().desc()).limit(20)).all()
        by_location = s.execute(select(Job.location, func.count()).where(relevant_q).group_by(Job.location)
                                .order_by(func.count().desc()).limit(20)).all()
        by_source = dict(count_by(s, Job.source))
        scores = list(s.scalars(select(Job.match_score).where(Job.match_score.is_not(None))))
        notif = dict(s.execute(select(Notification.status, func.count()).group_by(Notification.status)).all())
        llm_today = s.get(LLMUsage, utcnow().date())
        last_check = s.scalar(select(func.max(SourceCheck.finished_at)).where(SourceCheck.success.is_(True)))
    return {
        "total_discovered": total,
        "by_pipeline_status": by_pipeline,
        "by_application_status": by_application,
        "by_company": [{"company": c or "Unknown", "count": n} for c, n in by_company],
        "by_location": [{"location": l or "Unknown", "count": n} for l, n in by_location],
        "by_source": by_source,
        "match_scores": scores,
        "notifications": notif,
        "llm_calls_today": llm_today.calls if llm_today else 0,
        "llm_daily_budget": get_settings().max_llm_calls_per_day,
        "last_successful_check": last_check.isoformat() if last_check else None,
        "application_statuses": APPLICATION_STATUSES,
    }
