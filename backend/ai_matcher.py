"""Resume <-> job match scoring.

The LLM is only called for jobs that already passed the rule-based filter.
Its output is never trusted blindly:
  * matching_skills must appear in BOTH the resume and the job text
  * missing_skills must appear in the job text and NOT in the resume
  * the score is capped when experience or role relevance is judged incompatible
If no LLM is available (unconfigured / budget exhausted / error) a deterministic
keyword-overlap score is used and labelled as such.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Optional

from backend import llm_client
from backend.config import get_settings
from backend.resume_parser import SKILL_VOCABULARY, _norm, all_profile_skills, appears_in

log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    score: Optional[int]
    method: str  # "llm" | "keyword" | "title_only" | "no_resume"
    matching_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    experience_compatibility: str = ""
    location_compatibility: str = ""
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    relevant_role: bool = True
    entry_level_ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


MATCH_SYSTEM = (
    "You are a strict technical recruiter evaluating a junior AI/ML candidate (0-2 years) against one job. "
    "Use ONLY facts present in the RESUME PROFILE and JOB text. Never assume the candidate has a skill that "
    "is not listed in the profile. Never invent job requirements. Output ONLY JSON."
)

MATCH_PROMPT = """CANDIDATE PREFERENCES: roles = AI/ML, ML, GenAI, LLM, NLP, CV, Deep Learning, Python ML engineer, \
or Data Scientist with ML scope; experience = fresher to {max_exp:g} years; locations = Chennai, Bengaluru, \
or remote from India (other Indian cities only if remote).

RESUME PROFILE (verified against the resume text):
{profile}

JOB:
Title: {title}
Company: {company}
Location: {location}
Experience stated: {experience}
Description{limited}:
<<<
{description}
>>>

Return JSON:
{{
  "match_percentage": integer 0-100,      // how well THIS candidate fits THIS job
  "relevant_role": boolean,               // is it genuinely an AI/ML/DS-with-ML engineering role?
  "entry_level_ok": boolean,              // false if it needs more than {max_exp:g} years or is senior/lead
  "required_experience": string,          // as stated in the job, or "not stated"
  "experience_compatibility": string,     // one sentence
  "location_compatibility": string,       // one sentence
  "matching_skills": [string],            // skills required/mentioned by the job that ARE in the profile
  "missing_skills": [string],             // skills required/mentioned by the job that are NOT in the profile
  "reasons": [string],                    // 2-4 short reasons to apply, grounded in the profile
  "concerns": [string]                    // 0-3 short concerns
}}"""


def _job_skills(job_text: str) -> list[str]:
    norm = _norm(job_text)
    return [s for s in SKILL_VOCABULARY if appears_in(s, norm)]


def _profile_has(skill: str, profile_skills_norm: str, resume_norm: str) -> bool:
    return appears_in(skill, profile_skills_norm) or appears_in(skill, resume_norm)


def keyword_match(job: dict, profile: dict, resume_text: str) -> MatchResult:
    job_text = f"{job['title']}\n{job.get('description', '')}"
    resume_norm = _norm(resume_text)
    profile_norm = _norm(" ".join(all_profile_skills(profile)))
    required = _job_skills(job_text)
    matching = [s for s in required if _profile_has(s, profile_norm, resume_norm)]
    missing = [s for s in required if s not in matching]
    if not required:
        return MatchResult(score=None, method="title_only", reasons=["Job text lists no recognisable skills"])
    score = round(30 + 70 * len(matching) / len(required))
    return MatchResult(
        score=score,
        method="keyword",
        matching_skills=matching,
        missing_skills=missing,
        reasons=[f"{len(matching)} of {len(required)} skills mentioned in the job appear in your resume"],
    )


def _verify_skills(result: MatchResult, job_text: str, profile: dict, resume_text: str) -> None:
    job_norm = _norm(job_text)
    resume_norm = _norm(resume_text)
    profile_norm = _norm(" ".join(all_profile_skills(profile)))
    result.matching_skills = [
        s for s in result.matching_skills
        if isinstance(s, str) and appears_in(s, job_norm) and _profile_has(s, profile_norm, resume_norm)
    ]
    result.missing_skills = [
        s for s in result.missing_skills
        if isinstance(s, str) and appears_in(s, job_norm) and not _profile_has(s, profile_norm, resume_norm)
    ]


def _compact_profile(profile: dict) -> str:
    keep = {k: profile.get(k) for k in (
        "technical_skills", "programming_languages", "ai_ml_knowledge", "total_experience_years",
        "certifications", "education")}
    keep["projects"] = [
        {"name": p.get("name"), "technologies": p.get("technologies"), "summary": (p.get("summary") or "")[:200]}
        for p in (profile.get("projects") or [])[:6] if isinstance(p, dict)
    ]
    keep["experience"] = [
        {"role": e.get("role"), "company": e.get("company"), "duration": e.get("duration")}
        for e in (profile.get("experience") or [])[:5] if isinstance(e, dict)
    ]
    return json.dumps(keep, ensure_ascii=False)


def match_job(job: dict, profile: Optional[dict], resume_text: str) -> MatchResult:
    """job: dict with title, company, location, description, experience_text, limited_details."""
    s = get_settings()
    if not profile:
        return MatchResult(score=None, method="no_resume", reasons=["Upload your resume to enable match scoring"])

    job_text = f"{job['title']}\n{job.get('description', '')}"
    if llm_client.is_configured():
        try:
            data = llm_client.generate_json(
                MATCH_SYSTEM,
                MATCH_PROMPT.format(
                    max_exp=s.max_experience_years,
                    profile=_compact_profile(profile),
                    title=job["title"],
                    company=job.get("company") or "unknown",
                    location=job.get("location") or "not stated",
                    experience=job.get("experience_text") or "not stated",
                    limited=" (ONLY title/company/location known - from an alert email; be cautious)"
                    if job.get("limited_details") else "",
                    description=(job.get("description") or "(no description available)")[:12000],
                ),
            )
            result = MatchResult(
                score=max(0, min(100, int(data.get("match_percentage", 0)))),
                method="llm",
                matching_skills=list(data.get("matching_skills") or []),
                missing_skills=list(data.get("missing_skills") or []),
                experience_compatibility=str(data.get("experience_compatibility") or ""),
                location_compatibility=str(data.get("location_compatibility") or ""),
                reasons=[str(r) for r in (data.get("reasons") or [])][:4],
                concerns=[str(c) for c in (data.get("concerns") or [])][:3],
                relevant_role=bool(data.get("relevant_role", True)),
                entry_level_ok=bool(data.get("entry_level_ok", True)),
            )
            _verify_skills(result, job_text, profile, resume_text)
            if not result.relevant_role:
                result.score = min(result.score, 20)
            if not result.entry_level_ok:
                result.score = min(result.score, 35)
            return result
        except llm_client.LLMUnavailable as exc:
            log.info("LLM unavailable, using keyword match: %s", exc)
        except Exception as exc:
            log.warning("LLM match failed for '%s', using keyword match: %s", job["title"], exc)

    result = keyword_match(job, profile, resume_text)
    if result.method == "title_only":
        if job.get("limited_details"):
            # Alert-email jobs without a description: the rule filter already confirmed an
            # AI/ML title, junior-compatible seniority and location. Score at the threshold so
            # you still hear about them, clearly labelled as an estimate.
            result.score = s.min_match_score
            result.reasons = ["Title matches your target roles; full description unavailable - verify on the job page"]
        else:
            # A full description that names no recognisable technical skill is usually not a
            # hands-on ML role. Keep it below the threshold (visible on the dashboard, not emailed).
            result.method = "keyword"
            result.score = max(0, s.min_match_score - 10)
            result.reasons = ["Description names no recognisable technical skills - review manually"]
    return result
