"""Cross-portal duplicate detection (v2) and the never-email-twice guarantee."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from backend import email_service, pipeline
from backend.database import (
    PIPELINE_RELEVANT,
    Job,
    Notification,
    dedupe_keys,
    get_engine,
    job_fingerprint,
    job_fingerprint_v2,
    session_scope,
    utcnow,
)
from backend.job_sources import RawJob
from backend.migrations import apply_migrations

SAME_ROLE = [
    (("Razorpay Software Private Limited", "Machine Learning Engineer", "Bengaluru"),
     ("Razorpay", "Machine Learning Engineer", "Bengaluru, Karnataka, India")),
    (("HackerRank", "Deep Learning Research Engineer", "Hybrid in Bangalore, India"),
     ("HackerRank", "Deep Learning Research Engineer", "Bengaluru, Karnataka, India (Hybrid)")),
    (("Glean", "ML Engineer II", "Bangalore"), ("Glean Technologies", "Machine Learning Engineer 2", "Bengaluru")),
    (("Acme", "Software Engineer, Machine Learning", "Chennai"), ("Acme", "Machine Learning Software Engineer", "Chennai, Tamil Nadu")),
    (("Sarvam AI", "NLP Engineer (JR12345)", "Bengaluru"), ("Sarvam", "Natural Language Processing Engineer", "Bangalore Urban")),
    (("Acme Labs Pvt. Ltd.", "Gen AI Engineer - Chennai", "Chennai"), ("Acme", "Generative AI Engineer", "Chennai")),
]
DIFFERENT_ROLE = [
    # pairs seen on live boards on 2026-10-31 that must NOT merge
    (("Databricks", "Solutions Architect - Digital Native Business (Fintech)", "Remote - Illinois"),
     ("Databricks", "Solutions Architect - Digital Native Business (Healthtech)", "Remote - California")),
    (("Rubrik", "Staff Software Engineer, AI Native Web Platform", "Bengaluru"),
     ("Rubrik", "Sr. Staff Software Engineer, AI Native Web Platform", "Bengaluru")),
    (("InMobi", "Publisher Partnerships Manager - Enterprise Gaming", "Bengaluru"),
     ("InMobi", "Publisher Partnerships Manager - Enterprise Non-Gaming", "Bengaluru")),
    (("Databricks", "Account Executive - Commercial", "Remote - Tamil Nadu, India"),
     ("Databricks", "Account Executive-Commercial", "Remote - South Korea")),
    (("Acme", "Software Engineer (Backend)", "Chennai"), ("Acme", "Software Engineer (Frontend)", "Chennai")),
    (("Databricks", "Account Executive-Commercial", "Remote - South Korea"),
     ("Databricks", "Commercial Account Executive", "Remote - South Africa")),
    (("Paytm", "Area Collections Manager", "Vadodara, Gujarat (IN)"), ("Paytm", "Area Collections Manager", "Surat, Gujarat (IN)")),
    (("Acme", "Machine Learning Engineer", "Chennai"), ("Acme", "Machine Learning Engineer, NLP", "Chennai")),
    (("Acme", "Machine Learning Engineer", "Chennai"), ("Acme", "Machine Learning Engineer", "Pune")),
    (("Acme", "Machine Learning Engineer", "Chennai"), ("Zeta", "Machine Learning Engineer", "Chennai")),
    (("Acme", "Data Scientist", "Chennai"), ("Acme", "Data Engineer", "Chennai")),
    (("Acme", "Machine Learning Engineer 1", "Chennai"), ("Acme", "Machine Learning Engineer 2", "Chennai")),
]


@pytest.mark.parametrize("a,b", SAME_ROLE)
def test_v2_matches_same_role_spelled_differently(a, b):
    assert job_fingerprint(*a) != job_fingerprint(*b)  # v1 missed these
    assert job_fingerprint_v2(*a) == job_fingerprint_v2(*b)


@pytest.mark.parametrize("a,b", DIFFERENT_ROLE)
def test_v2_keeps_different_roles_apart(a, b):
    assert job_fingerprint_v2(*a) != job_fingerprint_v2(*b)


def test_keys_details():
    assert dedupe_keys("Observe.AI", "AI Agent Engineer", "Remote - India") == ("observe", "agent ai engineer", "remote")
    assert dedupe_keys("X", "Y", "Remote - South Korea")[2] == "remote korea south"
    assert dedupe_keys("X", "Y", "Karnataka, India")[2] == ""          # state only: wildcard
    assert dedupe_keys("X", "Y", "Vadodara, Gujarat (IN)")[2] == "vadodara"
    assert dedupe_keys("X", "Y", "Remote")[2] == "remote"
    assert dedupe_keys("X", "ML Engineer", "India")[2] == ""          # country only: wildcard city
    assert dedupe_keys("X", "ML Engineer", "London, UK")[2] == "london"
    assert job_fingerprint_v2("", "Machine Learning Engineer", "Chennai") is None  # no company: no v2


SENT = []


def raw(source, i, company, title, location, limited=False):
    return RawJob(source=source, source_job_id=str(i), title=title, url=f"https://{source.replace(':', '-')}.example/{i}",
                  company=company, location=location, limited_details=limited,
                  description="" if limited else "Python, PyTorch. 0-1 years experience.",
                  posted_at=None if limited else datetime.now(timezone.utc) - timedelta(hours=1))


@pytest.fixture
def mail(monkeypatch):
    SENT.clear()
    monkeypatch.setattr(email_service, "send_job_notification", lambda job: SENT.append(job.id) or f"m{job.id}")


def status(jid):
    with session_scope() as s:
        return s.get(Job, abs(jid)).pipeline_status


def test_cross_portal_copy_is_not_emailed_again(db, mail):
    ats = pipeline.ingest(raw("greenhouse", 1, "Glean", "ML Engineer", "Bangalore"))
    assert ats > 0 and pipeline.score_job(ats) and pipeline.notify(ats)
    li = pipeline.ingest(raw("email:linkedin", 9, "Glean Technologies", "Machine Learning Engineer", "Bengaluru, Karnataka", True))
    nk = pipeline.ingest(raw("email:naukri", 7, "Glean", "Machine Learning Engineer - Bangalore", "Bengaluru", True))
    assert status(li) == status(nk) == "duplicate"  # stored, never scored or emailed
    assert SENT == [ats]
    with session_scope() as s:
        dup = s.get(Job, -li)
        assert dup.pipeline_status == "duplicate" and dup.duplicate_of_id == ats
        assert "same role after normalising" in dup.filter_reason


def test_email_first_then_ats_enriches_without_second_email(db, mail):
    li = pipeline.ingest(raw("email:linkedin", 9, "Razorpay", "Machine Learning Engineer", "Bengaluru", True))
    ats = pipeline.ingest(raw("greenhouse", 1, "Razorpay Software Private Limited", "Machine Learning Engineer", "Bengaluru, India"))
    assert li > 0 and ats < 0
    with session_scope() as s:
        orig = s.get(Job, li)
        assert not orig.limited_details and "PyTorch" in orig.description  # richer ATS text kept


def test_similar_title_match_respects_city_wildcard(db, mail):
    a = pipeline.ingest(raw("lever", 1, "Acme", "Machine Learning Engineer", "Chennai, Tamil Nadu"))
    b = pipeline.ingest(raw("email:linkedin", 2, "Acme", "Machine Learning Engineer", "India", True))  # country only
    c = pipeline.ingest(raw("email:naukri", 3, "Acme", "Machine Learning Engineer", "Pune", True))    # other city
    d = pipeline.ingest(raw("email:naukri", 4, "Acme", "Software Engineer, Machine Learning - Platform", "Chennai", True))
    assert status(a) == "relevant" and status(b) == "duplicate"  # "India" matches any Indian city
    # a different city / a different role are not duplicates (Pune is then rejected by the
    # existing location rule, which is a separate decision)
    assert status(c) != "duplicate" and status(d) != "duplicate"


def test_unique_v2_index_blocks_concurrent_double_email(db, mail):
    # two jobs that slipped past ingest-time dedupe (e.g. stored concurrently by two sources)
    fp2 = job_fingerprint_v2("Acme", "Machine Learning Engineer", "Chennai")
    with session_scope() as s:
        for i, src in enumerate(("lever", "email:linkedin")):
            s.add(Job(source=src, source_job_id=str(i), url=f"https://x/{i}", title="Machine Learning Engineer",
                      company="Acme", fingerprint=f"v1-{i}", fingerprint_v2=fp2, pipeline_status=PIPELINE_RELEVANT))
    with session_scope() as s:
        ids = sorted(s.scalars(select(Job.id)))
    assert pipeline.notify(ids[0]) is True
    assert pipeline.notify(ids[1]) is False
    assert SENT == [ids[0]]
    with session_scope() as s:
        assert s.get(Job, ids[1]).pipeline_status == "duplicate"


def test_migration_backfills_and_protects_already_emailed_jobs(db, mail):
    """A job emailed BEFORE this upgrade (no v2 columns) must not be emailed again when a
    differently-spelled copy arrives after the upgrade."""
    engine = get_engine()
    with session_scope() as s:
        j = Job(source="greenhouse", source_job_id="1", url="https://x/1", title="ML Engineer", company="Glean",
                location="Bangalore", fingerprint=job_fingerprint("Glean", "ML Engineer", "Bangalore"),
                pipeline_status="notified", first_detected_at=utcnow() - timedelta(days=2))
        s.add(j)
        s.flush()
        s.add(Notification(job_id=j.id, fingerprint=j.fingerprint, status="sent"))
        old_id = j.id
    with engine.begin() as c:  # simulate the pre-upgrade state: v2 data missing
        c.execute(text("UPDATE jobs SET fingerprint_v2 = NULL, dedupe_company = '', dedupe_title = '', dedupe_city = ''"))
        c.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    apply_migrations(engine)
    with session_scope() as s:
        assert s.get(Job, old_id).fingerprint_v2 is not None
        assert s.scalar(select(Notification.fingerprint_v2)) is not None
    copy = pipeline.ingest(raw("email:linkedin", 5, "Glean Technologies", "Machine Learning Engineer", "Bengaluru", True))
    assert status(copy) == "duplicate" and SENT == []
    with session_scope() as s:
        assert s.scalar(select(func.count(Notification.id))) == 1
