from fastapi.testclient import TestClient

from backend.database import Job, session_scope, utcnow
from backend.email_service import build_job_email
from backend.main import app

KEY = {"X-API-Key": "test-key-0123456789abcdefghijkl"}


def test_auth_required(db):
    with TestClient(app) as c:
        assert c.get("/api/stats").status_code == 401
        assert c.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/health").json()["db"] == "ok"


def test_tracker_update_and_stats(db):
    with session_scope() as s:
        j = Job(source="t", source_job_id="1", url="https://x/1", title="ML Engineer", company="Acme",
                fingerprint="f", pipeline_status="notified", match_score=77)
        s.add(j)
        s.flush()
        jid = j.id
    with TestClient(app) as c:
        r = c.patch(f"/api/jobs/{jid}", json={"application_status": "applied", "notes": "referral"}, headers=KEY)
        assert r.status_code == 200 and r.json()["application_status"] == "applied"
        assert c.patch(f"/api/jobs/{jid}", json={"application_status": "bogus"}, headers=KEY).status_code == 400
        st = c.get("/api/stats", headers=KEY).json()
        assert st["by_application_status"]["applied"] == 1 and st["match_scores"] == [77]


def test_resume_upload_rejects_non_pdf(db):
    with TestClient(app) as c:
        r = c.post("/api/resume", files={"file": ("cv.txt", b"hello", "text/plain")}, headers=KEY)
        assert r.status_code == 400


def test_email_format():
    job = Job(id=1, source="lever", url="https://jobs.lever.co/acme/1", apply_url="https://jobs.lever.co/acme/1/apply",
              title="Junior ML Engineer", company="Acme", location="Chennai", experience_text="0-2 years",
              salary="", posted_at=None, first_detected_at=utcnow(), limited_details=False, match_score=81,
              match_details={"method": "llm", "matching_skills": ["Python"], "missing_skills": ["Docker"]})
    subject, text, html = build_job_email(job)
    assert subject == "NEW AI/ML JOB MATCH FOUND - Junior ML Engineer"
    for label in ["Company:", "Job Role:", "Location:", "Experience:", "Salary:", "Posted Date:",
                  "First Detected At:", "Resume Match Score:", "Matching Skills:", "Missing Skills:", "Application URL:"]:
        assert label in text
    assert "Posted Date: Not provided by source" in text
    assert "https://jobs.lever.co/acme/1/apply" in html
