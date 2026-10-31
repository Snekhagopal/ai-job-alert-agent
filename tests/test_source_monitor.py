"""Source monitoring dashboard data: per-source stats, availability and honest delay figures."""
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend import pipeline, source_monitor
from backend.database import Job, Notification, SourceCheck, SourceState, session_scope, utcnow
from backend.job_sources import GmailAlertSource, GreenhouseSource, WorkdaySource

KEY = {"X-API-Key": "test-key-0123456789abcdefghijkl"}


def sources():
    return [GreenhouseSource("acme", "Acme", interval_minutes=2),
            WorkdaySource("wdco", "wd1", "External", "WdCo", interval_minutes=5),
            GmailAlertSource("linkedin", "from:x", interval_minutes=1),
            GmailAlertSource("naukri", "from:y", interval_minutes=1)]


def seed():
    now = utcnow()
    since = now - timedelta(days=3)
    with session_scope() as s:
        for name, kind in (("greenhouse:acme", "greenhouse"), ("workday:wdco", "workday"), ("email:linkedin", "email")):
            s.add(SourceState(name=name, kind=kind, last_run_at=now, last_success_at=now, consecutive_failures=0,
                              last_error="", monitoring_since=since))
        s.add(SourceState(name="email:naukri", kind="email", last_run_at=now, last_success_at=now - timedelta(hours=2),
                          consecutive_failures=0, last_error="unavailable: maintenance", monitoring_since=since))
        # greenhouse: two jobs published while monitored (3 min / 5 min delay), one old backfill job
        for i, (pub_ago, det_ago) in enumerate([(13, 10), (25, 20), (60 * 24 * 10, 60 * 24 * 3)]):
            s.add(Job(source="greenhouse", source_job_id=f"acme:{i}", url=f"https://g/{i}", title="ML Engineer",
                      company="Acme", fingerprint=f"g{i}", posted_at=now - timedelta(minutes=pub_ago),
                      first_detected_at=now - timedelta(minutes=det_ago), pipeline_status="notified" if i == 0 else "filtered_out"))
        s.add(Job(source="workday", source_job_id="wdco:JR1", url="https://w/1", title="Data Scientist", company="WdCo",
                  fingerprint="w1", posted_at=now - timedelta(hours=5), raw={"posted_precision": "day"}))
        s.add(Job(source="email:linkedin", source_job_id="9", url="https://l/9", title="AI Engineer", company="X",
                  fingerprint="l9", first_detected_at=now - timedelta(minutes=58),
                  raw={"alert_received_at": (now - timedelta(minutes=59)).isoformat()}))
        s.flush()
        first = s.scalar(select(Job.id).where(Job.source_job_id == "acme:0"))
        s.add(Notification(job_id=first, fingerprint="g0", status="sent", sent_at=now - timedelta(minutes=9)))
        for ok in (True, True, True, False):
            s.add(SourceCheck(source_name="greenhouse:acme", started_at=now - timedelta(minutes=30), finished_at=now,
                              success=ok, not_modified=ok, error="" if ok else "boom"))
        s.add(SourceCheck(source_name="email:linkedin", started_at=now - timedelta(minutes=5), success=True,
                          note="1 email(s) had no recognisable job links - parser may need a sample"))


def test_report_numbers(db, monkeypatch):
    monkeypatch.setattr(source_monitor, "load_sources", sources)
    seed()
    r = {x["source"]: x for x in source_monitor.source_report()["sources"]}

    gh = r["greenhouse:acme"]
    assert gh["method"] == "Official public Job Board API" and gh["interval_minutes"] == 2
    assert gh["jobs_total"] == 3 and gh["jobs_24h"] == 2 and gh["notified_total"] == 1
    assert gh["detection_delay"] == "4 min"  # median of 3 and 5 min; the old backfilled job is excluded
    assert "2 jobs" in gh["detection_delay_basis"]
    assert gh["publish_to_email_median"] == "4 min"
    assert gh["availability"] == "available" and gh["availability_24h_pct"] == 75.0 and gh["unchanged_24h"] == 3
    assert gh["meets_5_10_min_target"] is True

    wd = r["workday:wdco"]
    assert wd["detection_delay"] is None and "date only" in wd["detection_delay_basis"]

    li = r["email:linkedin"]
    assert li["detection_delay"] == "60 s" and "platform's own delay" in li["detection_delay_basis"]
    assert li["meets_5_10_min_target"] is False and "cannot be met" in li["expected_delay"]
    assert li["parser_verified"] is True and "no recognisable job links" in li["last_note"]

    nk = r["email:naukri"]
    assert nk["availability"] == "temporarily unavailable" and nk["parser_verified"] is False


def test_not_monitored_lists_disabled_and_impossible_sources(db):
    rows = {x["source"]: x["reason"] for x in source_monitor.not_monitored()}
    assert "Google Jobs" in rows and "SmartRecruiters" in rows


def test_api_returns_dashboard_fields_and_keeps_old_ones(db, monkeypatch):
    monkeypatch.setattr(source_monitor, "load_sources", sources)
    seed()
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as c:
        body = c.get("/api/health/sources", headers=KEY).json()
    assert {"healthy", "unhealthy_count", "sources", "not_monitored"} <= set(body)
    row = body["sources"][0]
    for field in ("source", "kind", "interval_minutes", "last_success_at", "consecutive_failures", "last_error",
                  "healthy", "method", "availability", "jobs_total", "detection_delay", "meets_5_10_min_target"):
        assert field in row
    assert body["unhealthy_count"] == 1  # naukri: last success 2 h ago


def test_monitoring_since_set_on_first_success(db, monkeypatch):
    class Ok(GreenhouseSource):
        def fetch(self, http):
            return []
    monkeypatch.setattr(pipeline, "load_sources", lambda: [Ok("b")])
    pipeline.run_cycle(force=True)
    with session_scope() as s:
        first = s.get(SourceState, "greenhouse:b").monitoring_since
    pipeline.run_cycle(force=True)
    with session_scope() as s:
        assert s.get(SourceState, "greenhouse:b").monitoring_since == first


def test_prune_keeps_recent_checks(db):
    with session_scope() as s:
        s.add(SourceCheck(source_name="a", started_at=utcnow() - timedelta(days=9), success=True))
        s.add(SourceCheck(source_name="a", started_at=utcnow() - timedelta(days=1), success=True))
    assert pipeline.prune_source_checks(force=True) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count(SourceCheck.id))) == 1
