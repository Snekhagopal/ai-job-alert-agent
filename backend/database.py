"""PostgreSQL (or Supabase) persistence: schema, sessions and small repository helpers.

Duplicate protection is enforced at four levels:
  1. UNIQUE (source, source_job_id)    - same posting seen again from the same source
  2. UNIQUE (url)                      - same canonical posting URL from any source
  3. fingerprint (company+title+city)  - same role cross-posted on several sources
  4. fingerprint_v2 + title similarity - same role spelled differently on another portal
     ("Razorpay Software Pvt Ltd" / "ML Engineer II" vs "Razorpay" / "Machine Learning Engineer 2")
and notifications have UNIQUE (job_id), UNIQUE (fingerprint) and UNIQUE (fingerprint_v2), so a
job can never be emailed twice.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from backend.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# Pipeline statuses (set by the system)
PIPELINE_DISCOVERED = "discovered"
PIPELINE_FILTERED_OUT = "filtered_out"
PIPELINE_DUPLICATE = "duplicate"
PIPELINE_LOW_MATCH = "low_match"
PIPELINE_RELEVANT = "relevant"
PIPELINE_NOTIFIED = "notified"

# Application statuses (set by you from the dashboard)
APPLICATION_STATUSES = ["none", "applied", "interview", "offer", "rejected", "not_interested"]


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "source_job_id", name="uq_jobs_source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_job_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    apply_url: Mapped[Optional[str]] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(512))
    company: Mapped[str] = mapped_column(String(255), index=True, default="")
    location: Mapped[str] = mapped_column(String(512), default="")
    is_remote: Mapped[Optional[bool]] = mapped_column(Boolean)
    experience_text: Mapped[str] = mapped_column(String(255), default="")
    exp_min: Mapped[Optional[float]] = mapped_column(Float)
    exp_max: Mapped[Optional[float]] = mapped_column(Float)
    salary: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    limited_details: Mapped[bool] = mapped_column(Boolean, default=False)
    # posted_at is ONLY what the source reports. None = source did not say.
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Cross-portal dedupe (v2): normalised company / title tokens / city, and their hash.
    fingerprint_v2: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    dedupe_company: Mapped[str] = mapped_column(String(255), default="", index=True)
    dedupe_title: Mapped[str] = mapped_column(String(512), default="")
    dedupe_city: Mapped[str] = mapped_column(String(64), default="")
    duplicate_of_id: Mapped[Optional[int]] = mapped_column(ForeignKey("jobs.id"))

    pipeline_status: Mapped[str] = mapped_column(String(32), default=PIPELINE_DISCOVERED, index=True)
    filter_reason: Mapped[str] = mapped_column(Text, default="")
    match_score: Mapped[Optional[int]] = mapped_column(Integer)
    match_details: Mapped[Optional[dict]] = mapped_column(JSON)

    application_status: Mapped[str] = mapped_column(String(32), default="none", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    status_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    raw: Mapped[Optional[dict]] = mapped_column(JSON)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("uq_notifications_fingerprint_v2", "fingerprint_v2", unique=True,
              postgresql_where=text("fingerprint_v2 IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    fingerprint_v2: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="sending")  # sending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    gmail_message_id: Mapped[Optional[str]] = mapped_column(String(128))
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SourceState(Base):
    """One row per configured source; drives per-source intervals and health."""

    __tablename__ = "source_state"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), default="")
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_alerted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[Optional[str]] = mapped_column(String(255))
    # First successful check; detection delay is only measured for jobs published after it.
    monitoring_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class SourceCheck(Base):
    __tablename__ = "source_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    jobs_found: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    not_modified: Mapped[bool] = mapped_column(Boolean, default=False)  # answered 304 Not Modified
    error: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")  # warnings on a successful check


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    gmail_message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    jobs_extracted: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_text: Mapped[str] = mapped_column(Text)
    profile: Mapped[dict] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------- engine / sessions

_engine = None
_SessionLocal: Optional[sessionmaker] = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = get_settings().database_url
        kwargs = {"pool_pre_ping": True}
        if url.startswith("postgresql"):
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        _engine = create_engine(url, **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    from backend.migrations import apply_migrations

    engine = get_engine()
    Base.metadata.create_all(engine)
    apply_migrations(engine)


def reset_engine() -> None:
    """Used by tests to point at a different DATABASE_URL."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionLocal = None, None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------- helpers

_CITY_ALIASES = {"bangalore": "bengaluru", "madras": "chennai", "gurgaon": "gurugram", "bombay": "mumbai"}


def normalize_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"\(.*?\)", " ", value)  # drop "(Remote)", "(Contract)" etc.
    value = re.sub(r"[^a-z0-9]+", " ", value)
    for alias, canonical in _CITY_ALIASES.items():
        value = re.sub(rf"\b{alias}\b", canonical, value)
    return " ".join(value.split())


# ---------------------------------------------------------------- cross-portal dedupe (v2)
#
# The same posting shows up with different spellings on different portals, e.g.
#   "Razorpay Software Private Limited" | "ML Engineer II"   | "Hybrid in Bangalore, India"
#   "Razorpay"                          | "Machine Learning Engineer 2" | "Bengaluru, Karnataka"
# v2 normalises company, title and city before hashing; a location that names only the country
# ("India") matches any Indian city of the same company + title. Title words are never fuzzily
# matched: on real boards near-identical titles are usually different roles ("Staff" vs
# "Sr. Staff", "(Fintech)" vs "(Healthtech)", "Gaming" vs "Non-Gaming"), and a wrong merge
# would hide a job. Notifications keep the v1 fingerprint too, so nothing emailed before is re-sent.

_INDIAN_CITIES = {
    "bengaluru": "bengaluru", "bangalore": "bengaluru", "chennai": "chennai", "madras": "chennai",
    "hyderabad": "hyderabad", "secunderabad": "hyderabad", "pune": "pune", "mumbai": "mumbai", "bombay": "mumbai",
    "navi mumbai": "mumbai", "thane": "mumbai", "gurugram": "gurugram", "gurgaon": "gurugram", "noida": "noida",
    "greater noida": "noida", "delhi": "delhi", "new delhi": "delhi", "kolkata": "kolkata", "calcutta": "kolkata",
    "ahmedabad": "ahmedabad", "coimbatore": "coimbatore", "kochi": "kochi", "cochin": "kochi",
    "thiruvananthapuram": "thiruvananthapuram", "trivandrum": "thiruvananthapuram", "jaipur": "jaipur",
    "indore": "indore", "chandigarh": "chandigarh", "mysuru": "mysuru", "mysore": "mysuru",
    "visakhapatnam": "visakhapatnam", "bhubaneswar": "bhubaneswar",
}
INDIAN_CITY_KEYS = frozenset(_INDIAN_CITIES.values())
_CITY_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _INDIAN_CITIES), key=len, reverse=True)) + r")\b")
_REMOTE_RE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b")
_INDIA_RE = re.compile(r"\b(india|ind|karnataka|tamil nadu|maharashtra|telangana|kerala|haryana|uttar pradesh|"
                       r"west bengal|gujarat|andhra pradesh|odisha|rajasthan|punjab|madhya pradesh)\b")
_LOCATION_NOISE = {"remote", "work", "from", "home", "wfh", "anywhere", "hybrid", "onsite", "on", "site", "in",
                   "based", "first", "friendly", "only", "or", "and"}

_COMPANY_NOISE = {
    "private", "pvt", "limited", "ltd", "inc", "incorporated", "llc", "llp", "plc", "gmbh", "ag", "bv", "sa",
    "technologies", "technology", "tech", "software", "solutions", "services", "systems", "labs", "lab",
    "india", "global", "group", "corp", "corporation", "co", "company", "the", "holdings", "international",
    "com", "in", "io",
}
_TITLE_ABBREVIATIONS = [
    (r"\bml\b", "machine learning"), (r"\bnlp\b", "natural language processing"), (r"\bcv\b", "computer vision"),
    (r"\bgen ?ai\b", "generative ai"), (r"\bdl\b", "deep learning"), (r"\bswe\b", "software engineer"),
    (r"\bsde\b", "software development engineer"), (r"\bjr\b", "junior"), (r"\bsr\b", "senior"),
    (r"\bengg\b", "engineer"), (r"\biii\b", "3"), (r"\bii\b", "2"), (r"\bi\b", "1"), (r"\biv\b", "4"),
]
_TITLE_NOISE = {"and", "the", "of", "for", "with", "a", "an", "in", "at", "role", "position", "job", "opening",
                "hiring", "urgent", "immediate", "joiner", "joiners", "remote", "hybrid", "onsite", "on", "site",
                "office", "wfh", "india", "full", "time", "fulltime", "permanent", "contract"}


def dedupe_keys(company: str, title: str, location: str) -> tuple[str, str, str]:
    """(company key, sorted title tokens, city key) used for cross-portal dedupe."""
    raw_company = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).split()
    words = [w for w in raw_company if w not in _COMPANY_NOISE]
    if len(words) > 1 and words[-1] == "ai":  # "Sarvam AI" == "Sarvam"; keep a bare "AI" name
        words = words[:-1]
    company_key = " ".join(words) or " ".join(raw_company)

    # Text in brackets is kept ("(Backend)" vs "(Frontend)" are different roles); req ids,
    # cities and work-mode words inside it are dropped below like anywhere else.
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
    for pattern, repl in _TITLE_ABBREVIATIONS:
        t = re.sub(pattern, repl, t)
    t = _CITY_RE.sub(" ", t)
    tokens = {w for w in t.split() if w not in _TITLE_NOISE and not re.fullmatch(r"[a-z]*\d{4,}[a-z0-9]*", w)}
    title_key = " ".join(sorted(tokens))

    loc = re.sub(r"[^a-z0-9]+", " ", (location or "").lower())
    m = _CITY_RE.search(loc)
    in_india = bool(_INDIA_RE.search(loc))
    # place words left after removing country/state names and work-mode words
    places = [w for w in _INDIA_RE.sub(" ", loc).split() if w not in _LOCATION_NOISE and not w.isdigit()]
    if m:
        city = _INDIAN_CITIES[m.group(1)]
    elif _REMOTE_RE.search(loc):
        # "Remote - Tamil Nadu, India" and "Remote - South Korea" are different postings
        city = "remote" if in_india or not places else "remote " + " ".join(sorted(set(places)))[:56]
    elif places:
        city = places[0]  # a city we have no alias for (e.g. "Vadodara") or a foreign city
    else:
        city = ""  # only a country/state or nothing: matches any Indian city of the same company + title
    return company_key[:255], title_key[:512], city


def job_fingerprint_v2(company: str, title: str, location: str) -> Optional[str]:
    company_key, title_key, city = dedupe_keys(company, title, location)
    if not company_key or not title_key:
        return None  # e.g. alert emails without a company: never dedupe on title alone
    return hashlib.sha256(f"{company_key}|{title_key}|{city}".encode()).hexdigest()


def job_fingerprint(company: str, title: str, location: str) -> str:
    city = normalize_text(location).split(" ")[0] if location else ""
    company_norm = re.sub(r"\b(private|pvt|limited|ltd|inc|llc|technologies|technology)\b", "", normalize_text(company))
    key = f"{' '.join(company_norm.split())}|{normalize_text(title)}|{city}"
    return hashlib.sha256(key.encode()).hexdigest()


def get_active_resume(session: Session) -> Optional[Resume]:
    return session.scalars(
        select(Resume).where(Resume.is_active.is_(True)).order_by(Resume.uploaded_at.desc())
    ).first()


def llm_calls_today(session: Session) -> int:
    row = session.get(LLMUsage, utcnow().date())
    return row.calls if row else 0


def record_llm_call(session: Session, success: bool) -> None:
    today = utcnow().date()
    row = session.get(LLMUsage, today)
    if row is None:
        row = LLMUsage(day=today, calls=0, failures=0)
        session.add(row)
    row.calls += 1
    if not success:
        row.failures += 1
    session.flush()


def count_by(session: Session, column) -> list[tuple[str, int]]:
    return list(session.execute(select(column, func.count()).group_by(column).order_by(func.count().desc())).all())
