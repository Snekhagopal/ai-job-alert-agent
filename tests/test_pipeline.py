from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from backend import email_service, pipeline
from backend.database import Job, Notification, SourceState, session_scope
from backend.job_sources import JobSource, RawJob

SENT = []


class FakeSource(JobSource):
    kind = "fake"

    def __init__(self, name, jobs, fail=False):
        super().__init__(name, interval_minutes=5)
        self.jobs, self.fail = jobs, fail

    def fetch(self, http):
        if self.fail:
            raise RuntimeError("boom")
        return list(self.jobs)


def raw(i, title="Junior Machine Learning Engineer", location="Chennai, India", source="fake", **kw):
    return RawJob(source=source, source_job_id=str(i), title=title, url=f"https://jobs.example.com/{source}/{i}?utm_source=x",
                  company="Acme AI", location=location,
                  description="Python, PyTorch. 0-1 years experience.",
                  posted_at=datetime.now(timezone.utc) - timedelta(hours=1), **kw)


def setup(monkeypatch, sources):
    SENT.clear()
    monkeypatch.setattr(pipeline, "load_sources", lambda: sources)
    monkeypatch.setattr(email_service, "send_job_notification", lambda job: SENT.append(job.title) or f"msg-{job.id}")


def test_full_cycle_notifies_once(db, monkeypatch):
    src = FakeSource("fake:a", [raw(1), raw(2, title="Senior ML Engineer"), raw(3, title="Accountant"),
                                raw(4, title="NLP Engineer", location="London, UK")])
    setup(monkeypatch, [src])
    s1 = pipeline.run_cycle(force=True)
    assert s1["new_jobs"] == 4 and s1["passed_filter"] == 1 and s1["notified"] == 1
    assert SENT == ["Junior Machine Learning Engineer"]

    s2 = pipeline.run_cycle(force=True)  # same jobs again
    assert s2["new_jobs"] == 0 and s2["notified"] == 0
    assert len(SENT) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count(Notification.id))) == 1
        statuses = dict(s.execute(select(Job.title, Job.pipeline_status)).all())
    assert statuses["Junior Machine Learning Engineer"] == "notified"
    assert statuses["Senior ML Engineer"] == "filtered_out"


def test_cross_source_duplicate_not_notified_twice(db, monkeypatch):
    a = FakeSource("fake:a", [raw(1, source="lever")])
    b = FakeSource("fake:b", [raw(99, source="email:linkedin", location="Chennai")])
    setup(monkeypatch, [a, b])
    pipeline.run_cycle(force=True)
    assert len(SENT) == 1
    with session_scope() as s:
        assert s.scalar(select(func.count(Job.id)).where(Job.pipeline_status == "duplicate")) == 1


def test_tracking_params_do_not_create_duplicates(db, monkeypatch):
    j1 = raw(1)
    j2 = raw(2)
    j2.url = j1.url.replace("utm_source=x", "utm_source=y")
    setup(monkeypatch, [FakeSource("fake:a", [j1, j2])])
    s1 = pipeline.run_cycle(force=True)
    assert s1["new_jobs"] == 1


def test_failed_email_is_retried_without_duplicates(db, monkeypatch):
    setup(monkeypatch, [FakeSource("fake:a", [raw(1)])])
    calls = {"n": 0}

    def flaky(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gmail 503")
        SENT.append(job.title)
        return "ok"

    monkeypatch.setattr(email_service, "send_job_notification", flaky)
    s1 = pipeline.run_cycle(force=True)
    assert s1["notify_failures"] == 1 and SENT == []
    s2 = pipeline.run_cycle(force=True)
    assert s2["retried_notifications"] == 1 and SENT == ["Junior Machine Learning Engineer"]
    pipeline.run_cycle(force=True)
    assert len(SENT) == 1


def test_source_failure_recorded_and_interval_respected(db, monkeypatch):
    bad = FakeSource("fake:bad", [], fail=True)
    good = FakeSource("fake:good", [raw(1)])
    setup(monkeypatch, [bad, good])
    s1 = pipeline.run_cycle()
    assert s1["failed_sources"][0]["source"] == "fake:bad" and s1["sources_ok"] == 1
    s2 = pipeline.run_cycle()  # not forced, interval not elapsed
    assert s2["sources_due"] == 0
    with session_scope() as s:
        st = s.get(SourceState, "fake:bad")
        assert st.consecutive_failures == 1 and "boom" in st.last_error


def test_low_match_not_notified(db, monkeypatch, settings):
    from backend import ai_matcher
    settings(min_match_score=80)
    monkeypatch.setattr(ai_matcher, "match_job", lambda *a: ai_matcher.MatchResult(score=50, method="llm"))
    setup(monkeypatch, [FakeSource("fake:a", [raw(1)])])
    s1 = pipeline.run_cycle(force=True)
    assert s1["low_match"] == 1 and SENT == []
