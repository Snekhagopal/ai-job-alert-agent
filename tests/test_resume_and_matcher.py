import io

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from backend import ai_matcher, llm_client, resume_parser

RESUME_LINES = [
    "Priya Example - Chennai - priya@example.com",
    "EDUCATION: B.Tech Computer Science, Anna University, 2025",
    "SKILLS: Python, SQL, PyTorch, scikit-learn, Pandas, NumPy, FastAPI, Docker, Git",
    "AI/ML: Machine Learning, Deep Learning, NLP, Transformers, Hugging Face, RAG, LangChain",
    "PROJECTS: Resume Matcher - RAG pipeline with LangChain, FAISS and FastAPI.",
    "Plant Disease Detector - CNN in PyTorch with OpenCV, deployed with Docker.",
    "EXPERIENCE: ML Intern, Example Labs, Jan 2025 - Jun 2025. Fine-tuned BERT for classification.",
    "CERTIFICATIONS: Google Machine Learning Crash Course",
]


def make_pdf(lines) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines * 2:
        c.drawString(40, y, line)
        y -= 18
    c.save()
    return buf.getvalue()


def test_pdf_text_and_keyword_profile(db):
    text = resume_parser.extract_pdf_text(make_pdf(RESUME_LINES))
    profile = resume_parser.build_profile(text)  # LLM_PROVIDER=none -> keyword fallback
    skills = {s.lower() for s in profile["technical_skills"]}
    assert {"python", "pytorch", "fastapi", "langchain", "faiss", "docker", "opencv"} <= skills
    assert "kubernetes" not in skills and "tensorflow" not in skills
    assert profile["_meta"]["extracted_with_llm"] is False


def test_rejects_non_text_pdf():
    with pytest.raises(resume_parser.ResumeParseError):
        resume_parser.extract_pdf_text(make_pdf([""]))


def test_llm_invented_skills_are_dropped(db, monkeypatch):
    text = resume_parser.extract_pdf_text(make_pdf(RESUME_LINES))
    monkeypatch.setattr(llm_client, "is_configured", lambda: True)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a: {
        "technical_skills": ["PyTorch", "Kubernetes", "AWS SageMaker"],   # last two are NOT in the resume
        "programming_languages": ["Python", "Scala"],
        "ai_ml_knowledge": ["NLP", "Reinforcement Learning"],
        "projects": [{"name": "Resume Matcher", "summary": "x", "technologies": ["LangChain", "Pinecone"]}],
        "certifications": ["AWS Certified ML Specialty"],
        "total_experience_years": 0.25,
    })
    p = resume_parser.build_profile(text)
    everything = " ".join(resume_parser.all_profile_skills(p) + p["certifications"]).lower()
    for invented in ["kubernetes", "sagemaker", "scala", "reinforcement", "pinecone", "aws certified"]:
        assert invented not in everything
    assert "Kubernetes" in p["_meta"]["unverified_items_dropped"]


def _profile():
    text = resume_parser.extract_pdf_text(make_pdf(RESUME_LINES))
    return resume_parser.build_profile(text), text


JOB = {"title": "Junior ML Engineer", "company": "Acme", "location": "Chennai",
       "description": "Need Python, PyTorch, Docker and Kubernetes. 0-2 years experience.",
       "experience_text": "", "limited_details": False}


def test_keyword_match(db):
    profile, text = _profile()
    r = ai_matcher.match_job(JOB, profile, text)
    assert r.method == "keyword"
    assert {"python", "pytorch", "docker"} <= {s.lower() for s in r.matching_skills}
    assert r.missing_skills == ["kubernetes"]
    assert 70 <= r.score <= 100


def test_llm_match_is_verified_and_capped(db, monkeypatch):
    profile, text = _profile()
    monkeypatch.setattr(llm_client, "is_configured", lambda: True)
    monkeypatch.setattr(llm_client, "generate_json", lambda *a: {
        "match_percentage": 95, "relevant_role": True, "entry_level_ok": False,
        "matching_skills": ["Python", "Kubernetes", "Rust"],   # candidate lacks Kubernetes; job doesn't mention Rust
        "missing_skills": ["Kubernetes", "Python", "Spark"],   # Python is in resume; Spark not in job
        "reasons": ["Strong Python"], "concerns": [],
        "experience_compatibility": "Needs 4 years", "location_compatibility": "Chennai matches",
    })
    r = ai_matcher.match_job(JOB, profile, text)
    assert r.matching_skills == ["Python"]
    assert r.missing_skills == ["Kubernetes"]
    assert r.score == 35  # capped because not entry-level


def test_no_resume_means_no_score(db):
    r = ai_matcher.match_job(JOB, None, "")
    assert r.score is None and r.method == "no_resume"


def test_title_only_fallback(db):
    profile, text = _profile()
    vague = {**JOB, "description": "Join our team to shape the future of payments.", "limited_details": False}
    assert ai_matcher.match_job(vague, profile, text).score < 60
    alert = {**JOB, "description": "", "limited_details": True}
    r = ai_matcher.match_job(alert, profile, text)
    assert r.score == 60 and r.method == "title_only"
