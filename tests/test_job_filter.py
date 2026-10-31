from backend.job_filter import apply_filters, extract_experience
from backend.job_sources import RawJob


def job(title, location="Bengaluru, India", description="", **kw):
    return RawJob(source="t", source_job_id=title, title=title, url="https://x/" + title,
                  company="Co", location=location, description=description, **kw)


def test_accepts_target_roles(db):
    for t in ["Machine Learning Engineer", "Junior AI Engineer", "Generative AI Engineer", "LLM Engineer",
              "NLP Engineer", "Computer Vision Engineer", "Deep Learning Engineer", "AI/ML Engineer - Fresher"]:
        assert apply_filters(job(t)).passed, t


def test_rejects_senior_and_non_ai(db):
    for t in ["Senior Machine Learning Engineer", "Sr. ML Engineer", "Lead AI Engineer", "Staff ML Engineer",
              "Principal Data Scientist", "ML Engineering Manager", "Backend Engineer", "Frontend Developer",
              "AI Sales Executive", "Machine Learning Engineer III"]:
        r = apply_filters(job(t, description="machine learning"))
        assert not r.passed, t


def test_data_scientist_needs_ml_description(db):
    assert not apply_filters(job("Data Scientist", description="Build dashboards in Excel and SQL")).passed
    assert apply_filters(job("Data Scientist", description="Train machine learning models with PyTorch")).passed


def test_python_developer_only_with_ml(db):
    assert not apply_filters(job("Python Developer", description="Django REST APIs")).passed
    assert apply_filters(job("Python Developer", description="Deploy scikit-learn models")).passed


def test_experience_rules(db):
    assert not apply_filters(job("ML Engineer", description="Requires 3-5 years of experience in ML")).passed
    assert not apply_filters(job("ML Engineer", description="5+ years experience building models")).passed
    r = apply_filters(job("ML Engineer", description="0-2 years of experience. Freshers welcome."))
    assert r.passed and r.exp_min == 0 and r.exp_max == 2
    # numbers unrelated to experience are ignored
    assert apply_filters(job("ML Engineer", description="We are a company with 10+ years of history.")).passed


def test_extract_experience_first_requirement():
    assert extract_experience("Experience: 1-3 yrs. Nice to have 5+ years experience in Go")[:2] == (1.0, 3.0)
    assert extract_experience("minimum 2 years of relevant experience")[0] == 2.0
    assert extract_experience("Looking for freshers")[0] == 0.0
    assert extract_experience("Great team")[0] is None


def test_location_rules(db):
    assert apply_filters(job("ML Engineer", location="Chennai, Tamil Nadu")).passed
    assert apply_filters(job("ML Engineer", location="Bangalore")).passed
    assert apply_filters(job("ML Engineer", location="Remote - India")).passed
    assert apply_filters(job("ML Engineer", location="Hyderabad", is_remote=True)).passed
    assert not apply_filters(job("ML Engineer", location="Hyderabad, India")).passed
    assert not apply_filters(job("ML Engineer", location="Pune (IN)")).passed
    assert not apply_filters(job("ML Engineer", location="San Francisco, CA")).passed
    assert not apply_filters(job("ML Engineer", location="Remote - United States")).passed
    assert not apply_filters(job("ML Engineer", location="Remote in US")).passed


def test_expired_and_stale(db):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    assert not apply_filters(job("ML Engineer", expires_at=now - timedelta(days=1))).passed
    assert not apply_filters(job("ML Engineer", posted_at=now - timedelta(days=90))).passed
    assert apply_filters(job("ML Engineer", posted_at=now - timedelta(hours=1))).passed
