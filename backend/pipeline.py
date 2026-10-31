"""One monitoring cycle: fetch due sources -> dedupe -> rule filter -> AI match -> notify.

Triggered every minute by n8n (POST /api/run-cycle?background=true) or by the optional internal
scheduler. Each source has its own minimum interval (config/sources.yaml: 2 min for ATS boards,
1 min for Gmail, 30 min for rate-limited aggregators), so no source is polled more often than
allowed even though the tick itself runs every minute.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Optional

import httpx

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from backend import ai_matcher, email_service
from backend.config import get_settings
from backend.database import (
    INDIAN_CITY_KEYS,
    PIPELINE_DUPLICATE,
    PIPELINE_FILTERED_OUT,
    PIPELINE_LOW_MATCH,
    PIPELINE_NOTIFIED,
    PIPELINE_RELEVANT,
    Job,
    Notification,
    SourceCheck,
    SourceState,
    dedupe_keys,
    get_active_resume,
    job_fingerprint,
    job_fingerprint_v2,
    session_scope,
    utcnow,
)
from backend.http_client import PoliteClient, shared_client
from backend.job_filter import apply_filters
from backend.job_sources import JobSource, RawJob, SourceTemporarilyUnavailable, canonical_url, load_sources

log = logging.getLogger(__name__)

_cycle_lock = threading.Lock()
MAX_NOTIFY_ATTEMPTS = 5
CROSS_SOURCE_DEDUPE_DAYS = 30
ADMIN_ALERT_AFTER_FAILURES = 3
ADMIN_ALERT_COOLDOWN = timedelta(hours=6)
REJECTED_DESCRIPTION_CHARS = 2000


class CycleAlreadyRunning(RuntimeError):
    pass


# ---------------------------------------------------------------- ingest


def ingest(raw: RawJob) -> Optional[int]:
    """Store a job if it is new. Returns the new job id, or None when already known."""
    url = canonical_url(raw.url)
    now = utcnow()
    with session_scope() as s:
        existing = s.scalars(
            select(Job).where(
                or_(
                    (Job.source == raw.source) & (Job.source_job_id == raw.source_job_id),
                    Job.url == url,
                )
            )
        ).first()
        if existing:
            existing.last_seen_at = now
            return None

        fp = job_fingerprint(raw.company, raw.title, raw.location)
        company_key, title_key, city_key = dedupe_keys(raw.company, raw.title, raw.location)
        job = Job(
            source=raw.source,
            source_job_id=raw.source_job_id[:255],
            url=url[:2048],
            apply_url=(raw.apply_url or raw.url)[:2048],
            title=raw.title[:512],
            company=raw.company[:255],
            location=raw.location[:512],
            is_remote=raw.is_remote,
            experience_text=raw.experience_text[:255],
            salary=raw.salary[:255],
            description=raw.description,
            limited_details=raw.limited_details,
            posted_at=raw.posted_at,
            first_detected_at=now,
            last_seen_at=now,
            fingerprint=fp,
            fingerprint_v2=job_fingerprint_v2(raw.company, raw.title, raw.location),
            dedupe_company=company_key,
            dedupe_title=title_key,
            dedupe_city=city_key,
            raw=raw.raw,
        )

        original, how = _find_original(s, job, now)
        if original:
            job.pipeline_status = PIPELINE_DUPLICATE
            job.duplicate_of_id = original.id
            job.filter_reason = f"duplicate of job #{original.id} ({original.source}; {how})"
            job.description = job.description[:REJECTED_DESCRIPTION_CHARS]
            # Keep the richer record: an ATS posting beats a title-only alert email.
            if original.limited_details and not raw.limited_details and original.pipeline_status != PIPELINE_NOTIFIED:
                original.description = raw.description
                original.limited_details = False
        else:
            result = apply_filters(raw)
            job.is_remote = result.is_remote
            job.exp_min, job.exp_max = result.exp_min, result.exp_max
            if result.experience_text and not job.experience_text:
                job.experience_text = result.experience_text[:255]
            if result.passed:
                job.pipeline_status = PIPELINE_RELEVANT
                job.filter_reason = "; ".join(filter(None, [result.reason, result.location_note]))
            else:
                job.pipeline_status = PIPELINE_FILTERED_OUT
                job.filter_reason = result.reason
                job.description = job.description[:REJECTED_DESCRIPTION_CHARS]
        s.add(job)
        try:
            s.flush()
        except IntegrityError:  # concurrent insert of the same posting
            s.rollback()
            return None
        return job.id if job.pipeline_status == PIPELINE_RELEVANT else -job.id


def _find_original(s, job: Job, now) -> tuple[Optional[Job], str]:
    """The earlier record of the same role, from any source, within 30 days."""
    recent = (Job.duplicate_of_id.is_(None), Job.first_detected_at >= now - timedelta(days=CROSS_SOURCE_DEDUPE_DAYS))
    if job.fingerprint_v2:
        original = s.scalars(select(Job).where(Job.fingerprint_v2 == job.fingerprint_v2, *recent)
                             .order_by(Job.first_detected_at)).first()
        if original:
            return original, "same role after normalising company/title/city"
    # v1 merges some different postings (e.g. two "Remote" jobs in different countries), so it
    # decides only where v2 cannot: rows stored before v2 existed, or jobs without a company.
    original = s.scalars(select(Job).where(
        Job.fingerprint == job.fingerprint, *recent,
        or_(Job.fingerprint_v2.is_(None), job.fingerprint_v2 is None),
    ).order_by(Job.first_detected_at)).first()
    if original:
        return original, "same company/title/city"
    # A location given only as "India" (common in alert emails) matches the same company + title
    # in any Indian city, and vice versa. Remote and foreign locations never act as wildcards.
    if job.dedupe_company and job.dedupe_title and (job.dedupe_city == "" or job.dedupe_city in INDIAN_CITY_KEYS):
        other_cities = INDIAN_CITY_KEYS | {""} if job.dedupe_city == "" else {""}
        original = s.scalars(select(Job).where(
            Job.dedupe_company == job.dedupe_company, Job.dedupe_title == job.dedupe_title,
            Job.dedupe_city.in_(other_cities), *recent,
        ).order_by(Job.first_detected_at)).first()
        if original:
            return original, "same company and title; location given only as India"
    return None, ""


# ---------------------------------------------------------------- scoring + notify


def score_job(job_id: int) -> bool:
    """Runs AI matching. Returns True if the job should be notified."""
    s_ = get_settings()
    with session_scope() as s:
        job = s.get(Job, job_id)
        resume = get_active_resume(s)
        payload = {
            "title": job.title, "company": job.company, "location": job.location,
            "description": job.description, "experience_text": job.experience_text,
            "limited_details": job.limited_details,
        }
        profile, resume_text = (resume.profile, resume.raw_text) if resume else (None, "")
    result = ai_matcher.match_job(payload, profile, resume_text)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.match_score = result.score
        job.match_details = result.to_dict()
        if result.score is not None and result.score < s_.min_match_score:
            job.pipeline_status = PIPELINE_LOW_MATCH
            job.filter_reason += f"; match {result.score}% < {s_.min_match_score}%"
            return False
        if not result.relevant_role or not result.entry_level_ok:
            job.pipeline_status = PIPELINE_LOW_MATCH
            job.filter_reason += "; AI judged role not relevant / not entry-level"
            return False
    return True


def notify(job_id: int) -> bool:
    """Claim-then-send: the UNIQUE notification row is written BEFORE sending, so a
    crash can at worst lose one email (retried later) but never send it twice."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        notif = s.scalars(select(Notification).where(Notification.job_id == job_id)).first()
        if notif is None:
            notif = Notification(job_id=job_id, fingerprint=job.fingerprint, fingerprint_v2=job.fingerprint_v2,
                                 status="sending", attempts=0)
            s.add(notif)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                log.info("job %s already notified via a duplicate fingerprint", job_id)
                dup = s.get(Job, job_id)
                dup.pipeline_status = PIPELINE_DUPLICATE
                dup.filter_reason += "; same role already notified from another source"
                return False
        elif notif.status in ("sent", "sending") or notif.attempts >= MAX_NOTIFY_ATTEMPTS:
            return False
        notif.attempts += 1
        notif.status = "sending"
    try:
        with session_scope() as s:
            job = s.get(Job, job_id)
            message_id = email_service.send_job_notification(job)
    except Exception as exc:
        log.exception("notification for job %s failed", job_id)
        with session_scope() as s:
            notif = s.scalars(select(Notification).where(Notification.job_id == job_id)).one()
            notif.status, notif.error = "failed", str(exc)[:2000]
        return False
    with session_scope() as s:
        notif = s.scalars(select(Notification).where(Notification.job_id == job_id)).one()
        notif.status, notif.gmail_message_id, notif.sent_at, notif.error = "sent", message_id, utcnow(), ""
        s.get(Job, job_id).pipeline_status = PIPELINE_NOTIFIED
    return True


def retry_failed_notifications() -> int:
    # Only "failed" rows are retried. A row stuck in "sending" means the process died
    # mid-send; the email may have gone out, so it is surfaced on the dashboard instead
    # of being re-sent automatically.
    with session_scope() as s:
        ids = list(s.scalars(select(Notification.job_id).where(
            Notification.status == "failed",
            Notification.attempts < MAX_NOTIFY_ATTEMPTS,
        )))
    return sum(notify(i) for i in ids)


# ---------------------------------------------------------------- sources


def _due(source: JobSource, state: Optional[SourceState], force: bool) -> bool:
    if force or state is None or state.last_run_at is None:
        return True
    # 30s slack so a 5-minute source is not skipped because the cycle fired a little early
    return utcnow() - state.last_run_at >= timedelta(minutes=source.interval_minutes) - timedelta(seconds=30)


def _record_check(source: JobSource, started, ok: bool, found: int, new: int, error: str,
                  not_modified: bool = False, count_failure: bool = True, note: str = "") -> None:
    now = utcnow()
    with session_scope() as s:
        s.add(SourceCheck(source_name=source.name, started_at=started, finished_at=now, success=ok,
                          jobs_found=found, new_jobs=new, not_modified=not_modified, error=error[:4000],
                          note=note[:1000]))
        st = s.get(SourceState, source.name) or SourceState(name=source.name, kind=source.kind, consecutive_failures=0)
        st.kind, st.last_run_at = source.kind, started
        if ok:
            st.last_success_at, st.consecutive_failures, st.last_error = now, 0, ""
            st.monitoring_since = st.monitoring_since or started
        else:
            if count_failure:
                st.consecutive_failures = (st.consecutive_failures or 0) + 1
            st.last_error = error[:4000]
        s.merge(st)


def _maybe_alert_admin() -> None:
    now = utcnow()
    with session_scope() as s:
        broken = list(s.scalars(select(SourceState).where(
            SourceState.consecutive_failures >= ADMIN_ALERT_AFTER_FAILURES,
            or_(SourceState.last_alerted_at.is_(None), SourceState.last_alerted_at < now - ADMIN_ALERT_COOLDOWN),
        )))
        if not broken:
            return
        body = "\n\n".join(f"{b.name}: {b.consecutive_failures} consecutive failures\n  last error: {b.last_error[:500]}"
                           for b in broken)
        try:
            email_service.send_admin_alert(f"{len(broken)} job source(s) failing", body)
        except Exception:
            log.exception("admin alert failed")
            return
        for b in broken:
            b.last_alerted_at = now


# ---------------------------------------------------------------- discovery
#
# Discovery (fetch + dedupe + rule filter) and processing (AI scoring + email) are separate:
#   * every due source runs in its own worker thread; a source that is still running from an
#     earlier tick is skipped, never queued twice, so one slow board cannot delay the others
#   * scoring/notifying runs in its own worker (start_processing_worker), woken as soon as a
#     source stores a relevant job, so a slow LLM call never delays the next fetch
# run_cycle() keeps the original synchronous behaviour (fetch, then score+notify) for the
# dashboard button, scripts and tests.

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_processing_lock = threading.Lock()
_work_available = threading.Event()
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
DISCOVERY_WORKERS = 8


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS, thread_name_prefix="source")
        return _executor


def _run_source(src: JobSource, http: PoliteClient) -> dict:
    """Fetch one source and ingest its jobs. Never raises; the outcome is recorded."""
    started = utcnow()
    try:
        jobs = src.fetch(http)
    except SourceTemporarilyUnavailable as exc:
        # Provider maintenance window: shown on the dashboard, but does not count towards
        # the consecutive-failure admin alert.
        err = f"unavailable: {exc}"
        log.info("source %s %s", src.name, err)
        _record_check(src, started, False, 0, 0, err, count_failure=False)
        return {"source": src.name, "ok": False, "error": err[:500], "found": 0, "new": 0, "relevant_ids": []}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        log.warning("source %s failed: %s", src.name, err)
        _record_check(src, started, False, 0, 0, err)
        return {"source": src.name, "ok": False, "error": err[:500], "found": 0, "new": 0, "relevant_ids": []}
    new, relevant, ingest_failed = 0, [], False
    for raw in jobs:
        try:
            jid = ingest(raw)
        except Exception:
            log.exception("ingest failed for %s %s", raw.source, raw.url)
            ingest_failed = True
            continue
        if jid is not None:
            new += 1
            if jid > 0:
                relevant.append(jid)
    if ingest_failed:
        src._pending_etags.clear()  # re-download next time instead of trusting a 304
    src.commit()
    note = ""
    if getattr(src, "unparsed_emails", 0):
        note = f"{src.unparsed_emails} email(s) had no recognisable job links - parser may need a sample"
    _record_check(src, started, True, len(jobs), new, "", not_modified=src.not_modified, note=note)
    if relevant:
        _work_available.set()
    return {"source": src.name, "ok": True, "error": "", "found": len(jobs), "new": new, "relevant_ids": relevant}


def _run_source_tracked(src: JobSource, http: PoliteClient) -> dict:
    try:
        return _run_source(src, http)
    finally:
        with _inflight_lock:
            _inflight.discard(src.name)


def run_discovery(force: bool = False, only: Optional[list[str]] = None, wait: bool = True,
                  http: Optional[PoliteClient] = None) -> dict:
    """Start every due source that is not already running.

    wait=True blocks until those sources finish and returns their combined results.
    wait=False returns immediately (background mode used by the 1-minute scheduler)."""
    sources = load_sources()
    if only:
        sources = [x for x in sources if x.name in only or x.kind in only]
    with session_scope() as s:
        states = {st.name: st for st in s.scalars(select(SourceState))}
        s.expunge_all()
    due = [x for x in sources if _due(x, states.get(x.name), force)]
    with _inflight_lock:
        busy = [x.name for x in due if x.name in _inflight]
        start = [x for x in due if x.name not in _inflight]
        _inflight.update(x.name for x in start)
    http = http or shared_client()
    futures = [_get_executor().submit(_run_source_tracked, src, http) for src in start]
    summary = {
        "sources_total": len(sources), "sources_due": len(due), "sources_started": len(start),
        "sources_still_running": busy,
    }
    if not wait:
        return summary
    summary.update(sources_ok=0, failed_sources=[], jobs_seen=0, new_jobs=0, relevant_ids=[])
    for fut in as_completed(futures):
        r = fut.result()
        if not r["ok"]:
            summary["failed_sources"].append({"source": r["source"], "error": r["error"]})
            continue
        summary["sources_ok"] += 1
        summary["jobs_seen"] += r["found"]
        summary["new_jobs"] += r["new"]
        summary["relevant_ids"].extend(r["relevant_ids"])
    return summary


# ---------------------------------------------------------------- processing


def process_pending(first_ids: Optional[list[int]] = None, time_budget: Optional[float] = None) -> dict:
    """Retry failed emails, then score + notify relevant jobs that were never scored.

    ``first_ids`` (jobs found by this cycle) go first; the rest are taken newest first,
    because a fresh posting is worth more than one that waited in the backlog."""
    out = {"notified": 0, "low_match": 0, "notify_failures": 0, "retried_notifications": 0,
           "pending_scoring": 0, "skipped": False}
    if not _processing_lock.acquire(blocking=False):
        out["skipped"] = True  # another worker is already processing
        return out
    try:
        settings = get_settings()
        budget = settings.cycle_time_budget_seconds if time_budget is None else time_budget
        t0 = time.monotonic()
        out["retried_notifications"] = retry_failed_notifications()
        with session_scope() as s:
            backlog = list(s.scalars(select(Job.id).where(
                Job.pipeline_status == PIPELINE_RELEVANT, Job.match_details.is_(None)
            ).order_by(Job.first_detected_at.desc()).limit(200)))
        queue = list(dict.fromkeys([*(first_ids or []), *backlog]))
        for i, jid in enumerate(queue):
            if time.monotonic() - t0 > budget:
                log.warning("processing time budget exhausted; %d jobs left for the next pass", len(queue) - i)
                out["pending_scoring"] = len(queue) - i
                break
            try:
                with session_scope() as s:
                    job = s.get(Job, jid)
                    if job is None or job.pipeline_status != PIPELINE_RELEVANT or job.match_details is not None:
                        continue  # already handled (e.g. by a concurrent run_cycle)
                if score_job(jid):
                    if notify(jid):
                        out["notified"] += 1
                    else:
                        out["notify_failures"] += 1
                else:
                    out["low_match"] += 1
            except Exception:
                log.exception("processing job %s failed", jid)
        return out
    finally:
        _processing_lock.release()


def wake_processing() -> None:
    _work_available.set()


def processing_worker_loop(stop: threading.Event, idle_seconds: float = 30.0) -> None:
    """Background worker: score + email jobs as soon as discovery stores them."""
    while not stop.is_set():
        _work_available.wait(timeout=idle_seconds)
        _work_available.clear()
        if stop.is_set():
            break
        try:
            res = process_pending()
            if res["pending_scoring"]:
                _work_available.set()  # budget ran out; continue right away
            _maybe_alert_admin()
        except Exception:
            log.exception("processing worker pass failed")


SOURCE_CHECK_RETENTION_DAYS = 8  # the dashboard shows 24 h and 7 d statistics
_last_prune = 0.0


def prune_source_checks(force: bool = False) -> int:
    """At 1-2 minute polling source_checks grows by ~50k rows a day; keep 8 days, hourly."""
    global _last_prune
    if not force and time.time() - _last_prune < 3600:
        return 0
    _last_prune = time.time()
    with session_scope() as s:
        return s.query(SourceCheck).filter(
            SourceCheck.started_at < utcnow() - timedelta(days=SOURCE_CHECK_RETENTION_DAYS)
        ).delete(synchronize_session=False)


def run_background_tick(force: bool = False, only: Optional[list[str]] = None) -> dict:
    """What the 1-minute scheduler calls: start due sources and return immediately."""
    summary = run_discovery(force=force, only=only, wait=False)
    _work_available.set()  # also picks up failed-email retries and any backlog
    _ping_healthcheck_background()
    try:
        prune_source_checks()
    except Exception:
        log.exception("pruning source_checks failed")
    return summary


# ---------------------------------------------------------------- cycle


def run_cycle(force: bool = False, only: Optional[list[str]] = None) -> dict:
    if not _cycle_lock.acquire(blocking=False):
        raise CycleAlreadyRunning("a monitoring cycle is already running")
    try:
        return _run_cycle(force, only)
    finally:
        _cycle_lock.release()


def _run_cycle(force: bool, only: Optional[list[str]]) -> dict:
    """Synchronous cycle: discovery, then scoring + notification. Same summary as before."""
    t0 = time.monotonic()
    started = utcnow()
    disc = run_discovery(force=force, only=only, wait=True, http=None)
    relevant_ids = disc.pop("relevant_ids")
    summary = {"started_at": started.isoformat(), **disc, "passed_filter": len(relevant_ids)}
    budget = get_settings().cycle_time_budget_seconds - (time.monotonic() - t0)
    proc = process_pending(first_ids=relevant_ids, time_budget=max(budget, 0))
    if proc.pop("skipped"):
        proc["processing"] = "handled by the background worker"
    summary.update(proc)
    _maybe_alert_admin()
    summary["duration_seconds"] = round(time.monotonic() - t0, 1)
    _ping_healthcheck(summary)
    log.info("cycle summary: %s", {k: v for k, v in summary.items() if k != "failed_sources"})
    return summary


def _ping_healthcheck(summary: dict) -> None:
    url = get_settings().healthchecks_ping_url
    if not url:
        return
    if summary["sources_due"] and not summary["sources_ok"]:
        url = url.rstrip("/") + "/fail"  # every due source failed
    try:
        httpx.get(url, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("healthcheck ping failed: %s", exc)


def _ping_healthcheck_background() -> None:
    """Background ticks don't wait for results, so report success if any source
    succeeded in the last 10 minutes."""
    url = get_settings().healthchecks_ping_url
    if not url:
        return
    with session_scope() as s:
        recent = s.scalar(select(SourceState.last_success_at).order_by(SourceState.last_success_at.desc().nulls_last()))
    if recent is None or utcnow() - recent > timedelta(minutes=10):
        url = url.rstrip("/") + "/fail"
    try:
        httpx.get(url, timeout=10)
    except httpx.HTTPError as exc:
        log.warning("healthcheck ping failed: %s", exc)


def rescore_backlog(t0: float) -> int:
    """Kept for compatibility; processing now covers the backlog."""
    budget = get_settings().cycle_time_budget_seconds - (time.monotonic() - t0)
    return process_pending(time_budget=max(budget, 0))["pending_scoring"]
