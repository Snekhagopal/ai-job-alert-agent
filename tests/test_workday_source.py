"""Workday source against mocked responses shaped like the live /wday/cxs API."""
import json

import httpx
import pytest

from backend import pipeline
from backend.database import SourceState, session_scope
from backend.http_client import PoliteClient
from backend.job_sources import SourceTemporarilyUnavailable, WorkdaySource

API = "/wday/cxs/acme/External"
FACETS = [
    {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Engineering", "id": "fam1", "count": 10}]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [
            {"descriptor": "India", "id": "country-in", "count": 40},
            {"descriptor": "United States of America", "id": "country-us", "count": 90},
        ]},
        {"facetParameter": "locations", "values": [{"descriptor": "Bangalore", "id": "city-blr", "count": 30}]},
    ]},
]
POSTINGS = [
    {"title": "Machine Learning Engineer", "externalPath": "/job/Bangalore/Machine-Learning-Engineer_JR1",
     "locationsText": "Bangalore", "postedOn": "Posted Today", "bulletFields": ["JR1"]},
    {"title": "Data Scientist", "externalPath": "/job/Chennai/Data-Scientist_JR2",
     "locationsText": "Chennai, India", "postedOn": "Posted Yesterday", "bulletFields": ["JR2"]},
]
DETAIL = {
    "/job/Bangalore/Machine-Learning-Engineer_JR1": {"jobPostingInfo": {
        "title": "Machine Learning Engineer", "jobDescription": "<p>Python, PyTorch. 0-2 years.</p>",
        "location": "Bangalore", "additionalLocations": ["Hyderabad"], "country": {"descriptor": "India"},
        "startDate": "2026-10-31", "postedOn": "Posted Today", "jobReqId": "JR1", "timeType": "Full time",
        "externalUrl": "https://acme.wd1.myworkdayjobs.com/External/job/Bangalore/Machine-Learning-Engineer_JR1"}},
    "/job/Chennai/Data-Scientist_JR2": {"jobPostingInfo": {
        "title": "Data Scientist", "jobDescription": "<p>ML models</p>", "location": "Chennai",
        "country": {"descriptor": "India"}, "startDate": "2026-10-30"}},
}


def make_http(maintenance=False, facets=FACETS):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /External/\nDisallow: /talentcommunity/\n")
        if req.url.host == "community.workday.com":
            return httpx.Response(200, text="<html>maintenance</html>")
        if maintenance:
            return httpx.Response(303, headers={"location": "https://community.workday.com/maintenance-page"})
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "POST" and req.url.path == f"{API}/jobs":
            return httpx.Response(200, json={"total": 2, "facets": facets, "jobPostings": POSTINGS})
        path = req.url.path[len(API):]
        if path in DETAIL:
            return httpx.Response(200, json=DETAIL[path])
        return httpx.Response(404)

    http = PoliteClient()
    http.min_delay = 0
    http.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return http, calls


@pytest.fixture(autouse=True)
def _clear_facets():
    WorkdaySource._facet_cache.clear()


def src(**kw):
    return WorkdaySource("acme", "wd1", "External", "Acme", search_terms=["machine learning"], **kw)


def test_india_facet_detection_prefers_country():
    assert WorkdaySource.india_facets({"facets": FACETS}) == {"locationCountry": ["country-in"]}
    no_country = [{"facetParameter": "locations", "values": [
        {"descriptor": "Bengaluru, Karnataka", "id": "b"}, {"descriptor": "Austin, TX", "id": "a"},
        {"descriptor": "Indianapolis, IN", "id": "x"}]}]
    assert WorkdaySource.india_facets({"facets": no_country}) == {"locations": ["b"]}


def test_fetch_uses_india_facet_and_reads_details(db):
    http, calls = make_http()
    jobs = src().fetch(http)
    searches = [c[2] for c in calls if c[0] == "POST"]
    assert searches[0]["appliedFacets"] == {}  # first call discovers the facets
    assert all(b["appliedFacets"] == {"locationCountry": ["country-in"]} for b in searches[1:])
    assert {b["searchText"] for b in searches[1:]} == {"", "machine learning"}
    ml = next(j for j in jobs if j.title == "Machine Learning Engineer")
    assert ml.source_job_id == "acme:Machine-Learning-Engineer_JR1"
    assert ml.location == "Bangalore, Hyderabad (India)"
    assert ml.description == "Python, PyTorch. 0-2 years."
    assert ml.posted_at.isoformat() == "2026-10-31T00:00:00+05:30" and ml.raw["posted_precision"] == "day"
    assert ml.url.endswith("/External/job/Bangalore/Machine-Learning-Engineer_JR1")


def test_known_postings_skip_detail_requests(db):
    http, calls = make_http()
    for j in src().fetch(http):
        pipeline.ingest(j)
    calls.clear()
    again = src().fetch(http)
    assert len(again) == 2 and all(c[0] == "POST" for c in calls)


def test_maintenance_window_is_unavailable_not_failure(db, monkeypatch):
    http, _ = make_http(maintenance=True)
    with pytest.raises(SourceTemporarilyUnavailable):
        src().fetch(http)
    monkeypatch.setattr(pipeline, "load_sources", lambda: [src()])
    monkeypatch.setattr(pipeline, "shared_client", lambda: http)
    s = pipeline.run_cycle(force=True)
    assert s["failed_sources"][0]["error"].startswith("unavailable: Workday maintenance")
    with session_scope() as s_:
        st = s_.get(SourceState, "workday:acme")
        assert st.consecutive_failures == 0 and st.last_error.startswith("unavailable")


def test_without_facets_keeps_only_india(db):
    POSTINGS.append({"title": "ML Engineer", "externalPath": "/job/Austin/ML-Engineer_JR3", "locationsText": "Austin, TX"})
    try:
        http, _ = make_http(facets=[])
        titles = {j.title for j in src().fetch(http)}
    finally:
        POSTINGS.pop()
    assert titles == {"Machine Learning Engineer", "Data Scientist"}


def test_workday_disabled_flag(tmp_path):
    from backend.job_sources import load_sources
    cfg = tmp_path / "s.yaml"
    cfg.write_text("workday:\n  enabled: false\n  tenants:\n    - { tenant: a, wd: wd1, site: X }\n")
    assert load_sources(str(cfg)) == []
    cfg.write_text("workday:\n  enabled: true\n  tenants:\n    - { tenant: a, wd: wd1, site: X }\n")
    [w] = load_sources(str(cfg))
    assert w.name == "workday:a" and w.interval_minutes == 5


def test_robots_5xx_during_maintenance_is_unavailable_and_retried_soon(db, monkeypatch):
    from backend import http_client
    state = {"robots": 503}

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(state["robots"], text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, json={"total": 0, "facets": [], "jobPostings": []})

    http = PoliteClient()
    http.min_delay = 0
    http.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceTemporarilyUnavailable, match="robots.txt unreachable"):
        src().fetch(http)
    state["robots"] = 200  # maintenance over
    with pytest.raises(SourceTemporarilyUnavailable):
        src().fetch(http)  # still cached as unreachable...
    monkeypatch.setattr(http_client, "ROBOTS_UNREACHABLE_TTL_SECONDS", 0)
    assert src().fetch(http) == []  # ...but re-checked after 10 min, not 6 h


def test_maintenance_excluded_from_alerts_for_12_hours(db, monkeypatch):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from backend import source_monitor
    from backend.database import utcnow
    monkeypatch.setattr(source_monitor, "load_sources", lambda: [src(), WorkdaySource("old", "wd1", "X")])
    with session_scope() as s:
        s.add(SourceState(name="workday:acme", kind="workday", last_success_at=utcnow() - timedelta(hours=2),
                          consecutive_failures=0, last_error="unavailable: Workday maintenance window"))
        s.add(SourceState(name="workday:old", kind="workday", last_success_at=utcnow() - timedelta(hours=13),
                          consecutive_failures=0, last_error="unavailable: Workday maintenance window"))
    with TestClient(__import__("backend.main", fromlist=["app"]).app) as c:
        body = c.get("/api/health/sources", headers={"X-API-Key": "test-key-0123456789abcdefghijkl"}).json()
    av = {x["source"]: x["availability"] for x in body["sources"]}
    assert av == {"workday:acme": "temporarily unavailable", "workday:old": "failing / stale"}
    assert body["unhealthy_count"] == 2 and body["alertable_unhealthy_count"] == 1
