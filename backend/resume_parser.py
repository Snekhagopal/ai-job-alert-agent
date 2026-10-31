"""PDF resume -> structured profile.

1. Text is extracted with pypdf (no OCR: scanned image-only PDFs are rejected with a clear error).
2. The LLM structures the text into a JSON profile (one call per upload).
3. Every extracted skill is VERIFIED against the resume text; anything the model
   produced that does not literally appear in the resume is dropped, so the profile
   can never contain invented skills.
4. Without an LLM, a deterministic keyword extractor builds the skills list.
"""
from __future__ import annotations

import io
import logging
import re

from pypdf import PdfReader

from backend import llm_client

log = logging.getLogger(__name__)

# Deterministic vocabulary used for the no-LLM fallback and for skill matching.
SKILL_VOCABULARY = [
    "python", "java", "c++", "javascript", "typescript", "sql", "golang", "rust", "bash", "scala",
    "machine learning", "deep learning", "nlp", "natural language processing", "computer vision",
    "generative ai", "llm", "llms", "rag", "retrieval augmented generation", "prompt engineering",
    "fine-tuning", "transformers", "bert", "gpt", "llama", "langchain", "llamaindex", "hugging face",
    "huggingface", "openai", "gemini", "vector database", "faiss", "chromadb", "pinecone", "pgvector",
    "pytorch", "tensorflow", "keras", "scikit-learn", "sklearn", "xgboost", "lightgbm", "opencv", "yolo",
    "cnn", "rnn", "lstm", "gan", "diffusion", "reinforcement learning", "time series",
    "pandas", "numpy", "scipy", "matplotlib", "seaborn", "plotly", "jupyter", "streamlit", "gradio",
    "fastapi", "flask", "django", "rest api", "docker", "kubernetes", "git", "github actions", "ci/cd",
    "aws", "azure", "gcp", "google cloud", "sagemaker", "vertex ai", "mlflow", "airflow", "spark",
    "postgresql", "mysql", "mongodb", "redis", "linux", "statistics", "data analysis", "feature engineering",
    "model deployment", "mlops", "onnx", "tensorrt", "power bi", "tableau", "excel", "n8n",
]

PROFILE_SYSTEM = (
    "You extract facts from resumes. Output ONLY JSON. Copy skills, tools and technologies exactly as "
    "written in the resume. Never add a skill, project, certification or experience that is not explicitly "
    "written in the resume text. If something is absent, use an empty list or null."
)

PROFILE_PROMPT = """Extract this resume into JSON with exactly these keys:
{{
  "name": string|null,
  "technical_skills": [string],        // tools, frameworks, libraries, platforms
  "programming_languages": [string],
  "ai_ml_knowledge": [string],         // ML/AI concepts and techniques
  "projects": [{{"name": string, "summary": string, "technologies": [string]}}],
  "experience": [{{"role": string, "company": string, "duration": string, "highlights": [string]}}],
  "total_experience_years": number,    // professional full-time experience; internships count as 0.25 each; 0 for fresher
  "education": [{{"degree": string, "institution": string, "year": string|null}}],
  "certifications": [string],
  "preferred_locations": [string]
}}

RESUME TEXT:
<<<
{text}
>>>"""


class ResumeParseError(ValueError):
    pass


def extract_pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # pypdf raises many types for malformed input
        raise ResumeParseError(f"Not a readable PDF: {exc}") from exc
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) < 200:
        raise ResumeParseError(
            "Very little text could be extracted. The PDF is probably a scanned image; "
            "export a text-based PDF from your editor and upload again."
        )
    return text


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+#.]+", " ", s.lower()).strip()


def appears_in(term: str, text_norm: str) -> bool:
    t = _norm(term)
    if not t:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9+#])", text_norm) is not None


def keyword_skills(text: str) -> list[str]:
    norm = _norm(text)
    return [s for s in SKILL_VOCABULARY if appears_in(s, norm)]


def _verified(items, text_norm: str) -> list[str]:
    out, seen = [], set()
    for item in items or []:
        if isinstance(item, str) and appears_in(item, text_norm) and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item.strip())
    return out


def build_profile(text: str) -> dict:
    text_norm = _norm(text)
    profile: dict = {}
    used_llm = False
    if llm_client.is_configured():
        try:
            profile = llm_client.generate_json(PROFILE_SYSTEM, PROFILE_PROMPT.format(text=text[:30000]))
            used_llm = True
        except Exception as exc:
            log.warning("LLM resume extraction failed, falling back to keywords: %s", exc)

    dropped: list[str] = []
    for key in ("technical_skills", "programming_languages", "ai_ml_knowledge", "certifications"):
        original = [i for i in profile.get(key, []) or [] if isinstance(i, str)]
        kept = _verified(original, text_norm)
        dropped += [i for i in original if i not in kept]
        profile[key] = kept

    for project in profile.get("projects", []) or []:
        if isinstance(project, dict):
            original = [t for t in project.get("technologies", []) or [] if isinstance(t, str)]
            project["technologies"] = _verified(original, text_norm)
            dropped += [t for t in original if t not in project["technologies"]]

    # The keyword pass guarantees coverage of common skills even if the LLM missed them.
    all_skills = {s.lower() for k in ("technical_skills", "programming_languages", "ai_ml_knowledge") for s in profile[k]}
    profile["technical_skills"] += [s for s in keyword_skills(text) if s not in all_skills]

    profile.setdefault("projects", [])
    profile.setdefault("experience", [])
    profile.setdefault("education", [])
    profile.setdefault("total_experience_years", None)
    profile["_meta"] = {"extracted_with_llm": used_llm, "unverified_items_dropped": dropped}
    return profile


def all_profile_skills(profile: dict) -> list[str]:
    skills: list[str] = []
    for key in ("technical_skills", "programming_languages", "ai_ml_knowledge"):
        skills += profile.get(key, []) or []
    for p in profile.get("projects", []) or []:
        skills += p.get("technologies", []) or []
    seen, out = set(), []
    for s in skills:
        if isinstance(s, str) and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out
