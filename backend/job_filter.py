"""Cheap rule-based filtering that runs BEFORE any LLM call.

Order: role -> seniority -> experience -> location -> freshness.
The first failing rule rejects the job with a human-readable reason (stored in the DB).
Unknown values (e.g. experience not stated) pass here and are judged by the AI matcher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from backend.config import get_settings
from backend.job_sources import RawJob, is_stale

ROLE_PATTERNS = [
    r"\bai\b", r"\ba\.i\.", r"artificial intelligence", r"\bml\b", r"machine learning", r"\bmlops\b",
    r"gen(erative)?\s*-?\s*ai", r"\bgenai\b", r"\bllms?\b", r"large language", r"\bnlp\b",
    r"natural language", r"computer vision", r"\bcv engineer", r"deep learning", r"\bdl\b",
    r"applied scientist", r"research engineer", r"prompt engineer", r"\brag\b",
]
DATA_SCIENTIST = re.compile(r"data scien", re.I)
PYTHON_DEV = re.compile(r"python", re.I)
ML_DESCRIPTION_TERMS = re.compile(
    r"machine learning|deep learning|\bml\b|pytorch|tensorflow|scikit|sklearn|\bnlp\b|\bllm|"
    r"computer vision|neural|model training|transformers|hugging ?face|xgboost|generative ai|genai",
    re.I,
)
ROLE_RE = re.compile("|".join(ROLE_PATTERNS), re.I)
# Titles that contain "AI" etc. but are not engineering roles we want.
NON_ENGINEERING = re.compile(
    r"\b(sales|account (executive|manager)|marketing|recruiter|talent|business development|"
    r"customer success|hr\b|legal|counsel|finance|accountant|content writer|annotator|data labell?er|"
    r"trainer \(freelance\)|product designer|designer)\b",
    re.I,
)
SENIOR = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|head|director|manager|architect|vp|vice president|chief|"
    r"distinguished|expert|iii|iv|l[5-9]|sde ?(2|ii|3|iii)|engineer (ii|iii|2|3)|mts ?(2|3))\b",
    re.I,
)

EXP_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", re.I)
EXP_PLUS = re.compile(r"(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)", re.I)
EXP_MIN = re.compile(r"(?:minimum|min\.?|at least|atleast)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)", re.I)
EXP_PLAIN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:\w+\s){0,4}experience", re.I)
FRESHER = re.compile(r"\b(fresher|freshers|0\s*(?:-|–|to)\s*\d|entry[- ]level|new grad|graduate program)\b", re.I)

PREFERRED_CITIES = re.compile(r"\b(chennai|bengaluru|bangalore)\b", re.I)
INDIA = re.compile(
    r"\(in\)|\b(india|chennai|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|ncr|gurugram|gurgaon|noida|"
    r"kolkata|ahmedabad|kochi|cochin|coimbatore|trivandrum|thiruvananthapuram|jaipur|indore|chandigarh|"
    r"mysore|mysuru|vizag|visakhapatnam|bhubaneswar|nagpur|lucknow)\b",
    re.I,
)
REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)
REMOTE_OK_REGION = re.compile(r"\b(india|apac|asia|anywhere|worldwide|global)\b", re.I)
NON_INDIA_REGION = re.compile(
    r"\b(united states|usa|u\.s\.|us|us only|canada|uk|united kingdom|europe|emea|germany|france|"
    r"netherlands|spain|poland|latam|brazil|mexico|australia|singapore|japan|new york|san francisco|"
    r"london|berlin|toronto|seattle|austin|boston|amsterdam|dubai|uae)\b",
    re.I,
)


@dataclass
class FilterResult:
    passed: bool
    reason: str
    exp_min: Optional[float] = None
    exp_max: Optional[float] = None
    experience_text: str = ""
    is_remote: Optional[bool] = None
    location_note: str = ""


EXP_CONTEXT = re.compile(r"experien|\bexp\b", re.I)


def extract_experience(text: str) -> tuple[Optional[float], Optional[float], str]:
    """Returns (min_years, max_years, matched_text) for the FIRST experience requirement
    stated in the text. A number only counts when "experience" appears near it, so
    "10+ years of history" is ignored. The AI matcher re-checks experience afterwards,
    so this rule errs on the side of letting borderline jobs through."""
    if not text:
        return None, None, ""
    candidates: list[tuple[int, float, Optional[float], str]] = []
    for rx in (EXP_RANGE, EXP_PLUS, EXP_MIN, EXP_PLAIN):
        for m in rx.finditer(text):
            context = text[max(0, m.start() - 60) : m.end() + 60]
            if not EXP_CONTEXT.search(context):
                continue
            lo = float(m.group(1))
            hi = float(m.group(2)) if rx is EXP_RANGE else None
            if lo <= 40 and (hi is None or lo <= hi <= 40):
                candidates.append((m.start(), lo, hi, m.group(0)))
    if not candidates:
        if FRESHER.search(text):
            return 0.0, None, "fresher / entry-level"
        return None, None, ""
    _, lo, hi, matched = min(candidates, key=lambda c: c[0])
    return lo, hi, matched


def check_role(job: RawJob) -> tuple[bool, str]:
    title = job.title
    if NON_ENGINEERING.search(title):
        return False, f"non-engineering title: '{title}'"
    if DATA_SCIENTIST.search(title):
        if ML_DESCRIPTION_TERMS.search(job.description) or (job.limited_details and not job.description):
            return True, "data scientist with ML/AI scope"
        return False, "data scientist role without ML/AI in description"
    if ROLE_RE.search(title):
        return True, "AI/ML title"
    if PYTHON_DEV.search(title) and ML_DESCRIPTION_TERMS.search(job.description):
        return True, "python developer with ML in description"
    return False, f"title not an AI/ML role: '{title}'"


def check_location(job: RawJob) -> tuple[bool, Optional[bool], str]:
    loc = job.location or ""
    text = f"{loc} {job.title}"
    remote = job.is_remote if job.is_remote is not None else bool(REMOTE.search(text))
    if PREFERRED_CITIES.search(loc):
        return True, remote, "preferred city"
    if remote:
        if NON_INDIA_REGION.search(loc) and not INDIA.search(loc):
            return False, remote, f"remote but restricted to another region: '{loc}'"
        if INDIA.search(loc) or REMOTE_OK_REGION.search(loc):
            return True, remote, "remote (India-eligible)"
        return True, remote, "remote - region not stated, verify eligibility"
    if not loc.strip():
        return True, remote, "location not stated"
    if INDIA.search(loc):
        return False, remote, f"other Indian city without remote option: '{loc}'"
    return False, remote, f"outside India: '{loc}'"


def apply_filters(job: RawJob) -> FilterResult:
    s = get_settings()
    ok, reason = check_role(job)
    if not ok:
        return FilterResult(False, reason)

    if SENIOR.search(job.title) and not re.search(r"\b(junior|associate|entry)\b", job.title, re.I):
        return FilterResult(False, f"senior/lead title: '{job.title}'")

    exp_source = f"{job.experience_text}\n{job.description}"
    exp_min, exp_max, exp_text = extract_experience(exp_source)
    if exp_min is not None and exp_min > s.max_experience_years:
        return FilterResult(False, f"requires {exp_text} (> {s.max_experience_years:g} years)", exp_min, exp_max, exp_text)

    loc_ok, remote, loc_note = check_location(job)
    if not loc_ok:
        return FilterResult(False, loc_note, exp_min, exp_max, exp_text, remote)

    if is_stale(job, s.job_max_age_days):
        return FilterResult(False, "expired or older than JOB_MAX_AGE_DAYS", exp_min, exp_max, exp_text, remote)

    return FilterResult(True, reason, exp_min, exp_max, exp_text or job.experience_text, remote, loc_note)
