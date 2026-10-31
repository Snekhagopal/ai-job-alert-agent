"""Fast polling: conditional GETs, incremental Greenhouse fetch, non-blocking discovery, migrations."""
import threading
import time

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from backend import email_service, pipeline
from backend.database import SourceCheck, get_engine, session_scope
from backend.http_client import PoliteClient
from backend.job_sources import AshbySource, GreenhouseSource, JobSource, LeverSource, RawJob
from backend.migrations import MIGRATIONS, apply_migrations

KEY = {"X-API-Key": "test-key-0123456789abcdefghijkl"}


def mock_client(handler) -> tuple[PoliteClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)  # RFC 9309: no robots.txt -> allowed
        seen.append(request)
        return handler(request)

    http = PoliteClient()
    http.min_delay = 0
    http.client = httpx.Client(transport=httpx.MockTransport(wrapped))
    return http, seen


GH_LIST = {"jobs": [
    {"id": i, "title": f"ML Engineer {i}", "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
     "location": {"name": "Bengaluru, India"}, "first_published": "2026-10-31T08:00:00Z", "updated_at": "x"}
    for i in (1, 2)
]}


def gh_handler(etag='W/"v1"'):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("if-none-match") == etag:
            return httpx.Response(304)
        if req.url.path.endswith("/jobs") and req.url.params.get("content") == "true":
            return httpx.Response(200, json={"jobs": [dict(j, content="&lt;p&gt;Bulk&lt;/p&gt;") for j in GH_LIST["jobs"]]})
        if req.url.path.endswith("/jobs"):
            return httpx.Response(200, json=GH_LIST, headers={"ETag": etag})
        jid = int(req.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"id": jid, "content": f"&lt;p&gt;Detail {jid} Python 0-1 years&lt;/p&gt;"})
    return handler


def test_greenhouse_fetches_details_only_for_new_jobs_and_honours_304(db):
    http, seen = mock_client(gh_handler())
    src = GreenhouseSource("acme", "Acme")
    jobs = src.fetch(http)
    assert [j.description for j in jobs] == ["Detail 1 Python 0-1 years", "Detail 2 Python 0-1 years"]
    assert jobs[0].posted_at.isoformat() == "2026-10-31T08:00:00+00:00"
    assert sorted(r.url.path for r in seen) == ["/v1/boards/acme/jobs", "/v1/boards/acme/jobs/1", "/v1/boards/acme/jobs/2"]
    for j in jobs:
        pipeline.ingest(j)
    src.commit()  # ETag stored only now

    seen.clear()
    src2 = GreenhouseSource("acme", "Acme")
    assert src2.fetch(http) == [] and src2.not_modified
    assert len(seen) == 1 and seen[0].headers["if-none-match"] == 'W/"v1"'


def test_greenhouse_known_jobs_need_no_detail_requests(db):
    http, seen = mock_client(gh_handler(etag='W/"changed"'))
    first = GreenhouseSource("acme", "Acme").fetch(http)
    for j in first:
        pipeline.ingest(j)
    seen.clear()
    again = GreenhouseSource("acme", "Acme").fetch(http)  # no ETag stored (no commit) -> full list
    assert len(again) == 2 and [r.url.path for r in seen] == ["/v1/boards/acme/jobs"]


def test_greenhouse_bulk_download_when_many_new_jobs(db, monkeypatch):
    monkeypatch.setattr(GreenhouseSource, "DETAIL_FETCH_LIMIT", 1)
    http, seen = mock_client(gh_handler())
    jobs = GreenhouseSource("acme", "Acme").fetch(http)
    assert {j.description for j in jobs} == {"Bulk"}
    assert [str(r.url.params) for r in seen] == ["", "content=true"]


def test_etag_not_stored_without_commit(db):
    http, seen = mock_client(lambda req: httpx.Response(304) if req.headers.get("if-none-match")
                             else httpx.Response(200, json=[], headers={"ETag": "e1"}))
    LeverSource("acme").fetch(http)  # fetched but never committed
    src = LeverSource("acme")
    src.fetch(http)
    assert not src.not_modified and "if-none-match" not in seen[-1].headers


def test_lever_and_ashby_304(db):
    for cls, payload in ((LeverSource, []), (AshbySource, {"jobs": []})):
        http, seen = mock_client(lambda req, p=payload: httpx.Response(304) if req.headers.get("if-none-match") == "t"
                                 else httpx.Response(200, json=p, headers={"ETag": "t"}))
        a = cls("acme")
        a.fetch(http)
        a.commit()
        b = cls("acme")
        assert b.fetch(http) == [] and b.not_modified


def test_throttle_does_not_block_other_hosts():
    http = PoliteClient()
    http._throttle("slow.example", 0)
    t = threading.Thread(target=http._throttle, args=("slow.example", 1.5))
    t.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    http._throttle("fast.example", 0)  # must not wait for slow.example's crawl delay
    assert time.monotonic() - t0 < 0.2
    t.join()


class SlowSource(JobSource):
    kind = "fake"

    def __init__(self, name, jobs, gate: threading.Event):
        super().__init__(name, interval_minutes=2)
        self.jobs, self.gate = jobs, gate

    def fetch(self, http):
        self.gate.wait(10)
        return list(self.jobs)


def _raw(i, source="fake"):
    from datetime import datetime, timedelta, timezone
    return RawJob(source=source, source_job_id=str(i), title="Junior Machine Learning Engineer",
                  url=f"https://jobs.example.com/{source}/{i}", company=f"Acme {i}", location="Chennai, India",
                  description="Python, PyTorch. 0-1 years experience.",
                  posted_at=datetime.now(timezone.utc) - timedelta(minutes=3))


def test_slow_source_does_not_block_next_tick_and_is_not_started_twice(db, monkeypatch):
    gate = threading.Event()
    slow = SlowSource("fake:slow", [_raw(1)], gate)
    fast = SlowSource("fake:fast", [_raw(2)], threading.Event())
    fast.gate.set()
    monkeypatch.setattr(pipeline, "load_sources", lambda: [slow, fast])
    sent = []
    monkeypatch.setattr(email_service, "send_job_notification", lambda job: sent.append(job.company) or "m")

    t0 = time.monotonic()
    tick1 = pipeline.run_discovery(force=True, wait=False)
    assert time.monotonic() - t0 < 1 and tick1["sources_started"] == 2
    deadline = time.time() + 5
    while time.time() < deadline and not pipeline.process_pending()["notified"]:
        time.sleep(0.05)
    assert sent == ["Acme 2"]  # fast source scored + emailed while the slow one is still fetching

    tick2 = pipeline.run_discovery(force=True, wait=False)
    assert "fake:slow" in tick2["sources_still_running"]
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and "Acme 1" not in sent:
        pipeline.process_pending()
        time.sleep(0.05)
    assert sorted(sent) == ["Acme 1", "Acme 2"]
    _wait_idle()


def _wait_idle(timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline and pipeline._inflight:
        time.sleep(0.02)
    assert not pipeline._inflight


def test_not_modified_recorded(db, monkeypatch):
    class NotModified(JobSource):
        kind = "fake"

        def fetch(self, http):
            self.not_modified = True
            return []

    monkeypatch.setattr(pipeline, "load_sources", lambda: [NotModified("fake:nm", interval_minutes=2)])
    s = pipeline.run_cycle(force=True)
    assert s["sources_ok"] == 1
    with session_scope() as s_:
        assert s_.scalar(select(SourceCheck.not_modified)) is True


def test_migrations_add_missing_columns_and_are_idempotent(db):
    engine = get_engine()
    with engine.begin() as c:
        c.execute(text("ALTER TABLE source_checks DROP COLUMN not_modified"))
        c.execute(text("ALTER TABLE source_checks DROP COLUMN note"))
        c.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    applied = apply_migrations(engine)
    assert applied == [m[0] for m in MIGRATIONS]
    assert apply_migrations(engine) == []
    with engine.begin() as c:
        cols = set(c.scalars(text("SELECT column_name FROM information_schema.columns WHERE table_name='source_checks'")))
    assert {"not_modified", "note"} <= cols


def test_background_api_mode_returns_immediately(db, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(pipeline, "load_sources", lambda: [SlowSource("fake:slow", [], gate)])
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as c:
        t0 = time.monotonic()
        r = c.post("/api/run-cycle?background=true&force=true", headers=KEY)
        assert r.status_code == 200 and r.json()["mode"] == "background"
        assert time.monotonic() - t0 < 2
        again = c.post("/api/run-cycle?background=true&force=true", headers=KEY).json()
        assert again["sources_still_running"] == ["fake:slow"]  # never 409, never doubled
    gate.set()
    _wait_idle()
    with session_scope() as s:
        assert s.scalar(select(func.count(SourceCheck.id))) == 1
