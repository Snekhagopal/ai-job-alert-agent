from pathlib import Path

from backend.job_sources import canonical_url, parse_alert_email

HTML = (Path(__file__).parent / "fixtures/linkedin_alert.html").read_text()


def test_linkedin_alert_cards():
    cards = {c["id"]: c for c in parse_alert_email("linkedin", HTML)}
    assert set(cards) == {"4012345678", "4098765432", "4011111111"}
    c = cards["4012345678"]
    assert c["title"] == "Machine Learning Engineer"
    assert c["company"] == "Acme AI Labs"
    assert c["location"].startswith("Bengaluru")
    assert c["url"] == "https://www.linkedin.com/jobs/view/4012345678"
    assert cards["4098765432"]["location"] == "India (Remote)"
    # target URL embedded (encoded) inside a tracking redirect
    assert cards["4011111111"]["title"] == "Senior NLP Scientist"


def test_canonical_url_strips_tracking_only():
    assert canonical_url("https://Jobs.Example.com/a/123/?utm_source=x&trackingId=9") == "https://jobs.example.com/a/123"
    assert canonical_url("https://careers.example.com/job?id=55&utm_medium=email") == "https://careers.example.com/job?id=55"


# ---------------------------------------------------------------- Tier B platforms

import json  # noqa: E402

import pytest  # noqa: E402

from backend.job_sources import (  # noqa: E402
    EMAIL_DEFAULT_QUERIES,
    EMAIL_PARSER_VERIFIED,
    EMAIL_PLATFORMS,
    load_sources,
)

SAMPLES = Path(__file__).parent / "fixtures/email_alerts"
REAL_SAMPLES = sorted(p for p in SAMPLES.glob("*/*.html") if p.with_suffix(".expected.json").exists())


@pytest.mark.parametrize("sample", REAL_SAMPLES or [pytest.param(None, marks=pytest.mark.skip(
    reason="no real alert samples yet - run scripts/capture_alert_samples.py <platform>"))],
    ids=lambda p: f"{p.parent.name}/{p.name}" if p else "none")
def test_real_alert_email_samples(sample):
    """Every REAL email sample must parse to the job cards a human checked against Gmail."""
    expected = json.loads(sample.with_suffix(".expected.json").read_text())
    got = {c["id"]: c for c in parse_alert_email(sample.parent.name, sample.read_text())}
    assert set(got) == {c["id"] for c in expected}
    for c in expected:
        for field in ("title", "company", "location", "url"):
            if c.get(field):
                assert got[c["id"]][field] == c[field], (c["id"], field)


# Public job-page URL formats. These check the link REGEX only; they are NOT a check of how
# each platform formats its alert emails - that needs a real sample (test above).
PUBLIC_JOB_URLS = {
    "indeed": ("https://in.indeed.com/viewjob?from=email&jk=0a1b2c3d4e5f6789&tk=abc", "0a1b2c3d4e5f6789",
               "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"),
    "glassdoor": ("https://www.glassdoor.co.in/job-listing/ml-engineer-acme-JV_IC2940587_KO0,11_KE12,16.htm?jl=1009512345678",
                  "1009512345678", "https://www.glassdoor.co.in/job-listing/j?jl=1009512345678"),
    "foundit": ("https://www.foundit.in/job/machine-learning-engineer-acme-bengaluru-34567890?utm_source=alert",
                "machine-learning-engineer-acme-bengaluru-34567890", None),
    "iimjobs": ("https://www.iimjobs.com/j/acme-ml-engineer-0-2-yrs-1234567.html?ref=alert", "acme-ml-engineer-0-2-yrs-1234567",
                "https://www.iimjobs.com/j/acme-ml-engineer-0-2-yrs-1234567.html"),
    "hirist": ("https://www.hirist.tech/j/acme-data-scientist-1-3-yrs-1234567.html", "acme-data-scientist-1-3-yrs-1234567",
               "https://www.hirist.tech/j/acme-data-scientist-1-3-yrs-1234567.html"),
    "internshala": ("https://internshala.com/job/detail/machine-learning-engineer-job-in-chennai-at-acme1729912345",
                    "job/detail/machine-learning-engineer-job-in-chennai-at-acme1729912345", None),
    "shine": ("https://www.shine.com/jobs/machine-learning-engineer/acme-ai/16543210", "machine-learning-engineer/acme-ai/16543210", None),
    "timesjobs": ("https://www.timesjobs.com/job-detail/ml-engineer-acme-bengaluru-0-to-2-yrs-jobid-AbC123__PLUS__xyZ==&source=srp",
                  "ml-engineer-acme-bengaluru-0-to-2-yrs-jobid-AbC123__PLUS__xyZ==", None),
}


@pytest.mark.parametrize("platform", sorted(PUBLIC_JOB_URLS))
def test_candidate_link_pattern_matches_public_job_url(platform):
    url, job_id, canonical = PUBLIC_JOB_URLS[platform]
    pattern, template = EMAIL_PLATFORMS[platform]
    assert pattern.search(url).group(1) == job_id
    # also when wrapped, URL-encoded, in a tracking redirect
    from urllib.parse import quote
    assert pattern.search(__import__("urllib.parse").parse.unquote(f"https://click.example.com/r?u={quote(url, safe='')}")).group(1) == job_id
    if canonical:
        assert template.format(id=job_id) == canonical


def test_candidate_parser_extracts_cards_from_simple_markup():
    html = ('<table><tr><td><a href="https://in.indeed.com/rc/clk?jk=0a1b2c3d4e5f6789&amp;from=ja">Machine Learning Engineer</a>'
            '<div>Acme AI</div><div>Chennai, Tamil Nadu</div><div>Easily apply</div></td></tr></table>')
    [card] = parse_alert_email("indeed", html)
    assert card["id"] == "0a1b2c3d4e5f6789" and card["title"] == "Machine Learning Engineer"
    assert card["url"] == "https://in.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"


def test_every_platform_has_default_query_and_unverified_ones_are_off_by_default():
    assert set(EMAIL_DEFAULT_QUERIES) == set(EMAIL_PLATFORMS)
    enabled = {s.name.split(":")[1] for s in load_sources(str(Path(__file__).parents[1] / "config/sources.yaml"))
               if s.kind == "email"}
    new_platforms = {"indeed", "glassdoor", "foundit", "iimjobs", "hirist", "timesjobs", "internshala", "shine"}
    assert not (enabled & (new_platforms - EMAIL_PARSER_VERIFIED)), "enable only after a real sample passes"
    assert all(s.interval_minutes == 1 for s in load_sources(str(Path(__file__).parents[1] / "config/sources.yaml"))
               if s.kind == "email")


def test_unknown_platform_rejected(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("email_alerts:\n  myspace: { enabled: false }\n")
    with pytest.raises(ValueError):
        load_sources(str(cfg))


def test_unparsed_emails_can_be_reprocessed(db):
    from backend.database import ProcessedEmail, session_scope
    from backend.job_sources import reset_unparsed_emails
    with session_scope() as s:
        s.add_all([ProcessedEmail(gmail_message_id="a", source="email:naukri", jobs_extracted=0),
                   ProcessedEmail(gmail_message_id="b", source="email:naukri", jobs_extracted=4),
                   ProcessedEmail(gmail_message_id="c", source="email:indeed", jobs_extracted=0)])
    assert reset_unparsed_emails("naukri") == 1
    with session_scope() as s:
        assert {r.gmail_message_id for r in s.query(ProcessedEmail)} == {"b", "c"}


def test_gmail_source_records_unparsed_email_note(db, monkeypatch):
    from sqlalchemy import select

    from backend import gmail_client, pipeline
    from backend.database import SourceCheck, session_scope
    from backend.job_sources import GmailAlertSource
    monkeypatch.setattr(gmail_client, "list_message_ids", lambda q, max_results=50: ["m1", "m2"])
    monkeypatch.setattr(gmail_client, "get_message", lambda mid: {
        "internal_date_ms": 1761900000000, "subject": "Your job alert", "text": "",
        "html": HTML if mid == "m1" else "<p>Newsletter without job links</p>"})
    monkeypatch.setattr(pipeline, "load_sources", lambda: [GmailAlertSource("linkedin", "from:x", interval_minutes=1)])
    summary = pipeline.run_cycle(force=True)
    assert summary["new_jobs"] == 3
    with session_scope() as s:
        assert "1 email(s) had no recognisable job links" in s.scalar(select(SourceCheck.note))
