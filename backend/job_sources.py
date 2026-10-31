"""Job sources. Every source here uses an access method the provider offers for this purpose.

| Source kind      | Access method                                            | Freshness            |
|------------------|----------------------------------------------------------|----------------------|
| greenhouse       | Public Job Board API (boards-api.greenhouse.io)          | real-time, 2 min poll|
| lever            | Public Postings API (api.lever.co/v0/postings)           | real-time, 2 min poll|
| ashby            | Public Job Posting API (api.ashbyhq.com/posting-api)     | real-time, 2 min poll|
| workday          | Public career-site JSON (no login; opt-in)               | day-precision, 5 min |
| adzuna           | Official REST API, free key (aggregates Indian boards)   | ~hourly, 30 min poll |
| jooble           | Official REST API, free key                              | ~hourly, 30 min poll |
| rss              | Any RSS/Atom feed you add (robots.txt respected)         | feed dependent       |
| email:<platform> | Job-alert emails the portals send to YOUR Gmail, read    | as often as the      |
|                  | via Gmail API (1 min poll)                               | platform emails you  |

LinkedIn, Naukri, Indeed, Glassdoor, Instahyre, Cutshort, Wellfound, Foundit, iimjobs, Hirist,
TimesJobs, Internshala and Shine have no permitted public job-search API, so they are
monitored ONLY through the alert emails they send you.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import feedparser
import httpx
import yaml
from bs4 import BeautifulSoup

from backend import gmail_client
from backend.config import get_settings
from backend.http_client import PoliteClient, RobotsUnreachable

log = logging.getLogger(__name__)


@dataclass
class RawJob:
    source: str
    source_job_id: str
    title: str
    url: str
    company: str = ""
    apply_url: Optional[str] = None
    location: str = ""
    description: str = ""
    experience_text: str = ""
    salary: str = ""
    posted_at: Optional[datetime] = None  # only when the source states it
    is_remote: Optional[bool] = None
    expires_at: Optional[datetime] = None
    limited_details: bool = False  # True when only title/company/location are known (alert emails)
    raw: dict = field(default_factory=dict)


def html_to_text(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(html_lib.unescape(value), "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def from_epoch_ms(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


_TRACKING_PARAMS = {"trackingid", "refid", "ref", "src", "source", "ckey", "se", "v", "lipi", "midtoken",
                    "midsig", "trk", "trkemail", "eid", "otptoken", "gh_src", "lever-source", "lever-origin"}


def canonical_url(url: str) -> str:
    """Drop fragments and tracking parameters so the same posting dedupes,
    while keeping query parameters that identify the job (e.g. ?id=123)."""
    parts = urlsplit(url.strip())
    query = "&".join(
        kv for kv in parts.query.split("&")
        if kv and not kv.split("=")[0].lower().startswith("utm_") and kv.split("=")[0].lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme or "https", parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


# URL -> (ETag, time of the last full download). Sources load fresh from sources.yaml every
# cycle, so the cache lives at module level. An ETag is only stored in commit(), after the jobs
# were ingested, so a crash between fetch and ingest can never hide postings behind a 304.
_ETAGS: dict[str, tuple[str, float]] = {}
_ETAGS_LOCK = threading.Lock()
# Force a full download at least this often even if the board says "unchanged".
ETAG_MAX_AGE_SECONDS = 30 * 60


def reset_etag_cache() -> None:
    with _ETAGS_LOCK:
        _ETAGS.clear()


class JobSource(ABC):
    kind: str = ""
    method: str = ""  # human-readable integration method, shown on the dashboard

    def __init__(self, name: str, interval_minutes: int = 5) -> None:
        self.name = name
        self.interval_minutes = interval_minutes
        self.not_modified = False  # set by fetch() when the source answered 304
        self._pending_etags: dict[str, str] = {}

    @abstractmethod
    def fetch(self, http: PoliteClient) -> list[RawJob]: ...

    def commit(self) -> None:
        """Called after fetched jobs are safely stored (e.g. mark emails processed)."""
        now = time.time()
        with _ETAGS_LOCK:
            for url, etag in self._pending_etags.items():
                _ETAGS[url] = (etag, now)
        self._pending_etags = {}

    def _get_json_cached(self, http: PoliteClient, url: str, **kwargs) -> Any:
        """GET with If-None-Match. Returns None (and sets ``not_modified``) on 304."""
        with _ETAGS_LOCK:
            cached = _ETAGS.get(url)
        etag = cached[0] if cached and time.time() - cached[1] < ETAG_MAX_AGE_SECONDS else None
        data, new_etag = http.get_json_if_changed(url, etag, **kwargs)
        if data is None:
            self.not_modified = True
            return None
        if new_etag:
            self._pending_etags[url] = new_etag
        return data


def known_job_ids(source: str, source_job_ids: list[str]) -> set[str]:
    """Which of these ids are already stored, so detail pages are fetched only for new jobs."""
    from sqlalchemy import select

    from backend.database import Job, session_scope

    if not source_job_ids:
        return set()
    with session_scope() as s:
        return set(s.scalars(select(Job.source_job_id).where(
            Job.source == source, Job.source_job_id.in_(source_job_ids))))


# ====================================================================== ATS public APIs


class GreenhouseSource(JobSource):
    kind = "greenhouse"
    method = "Official public Job Board API"
    # Above this many unseen jobs (first run, big boards) one ?content=true download is
    # cheaper than one detail request per job.
    DETAIL_FETCH_LIMIT = 15

    def __init__(self, board: str, company: str = "", **kw) -> None:
        super().__init__(f"greenhouse:{board}", **kw)
        self.board, self.company = board, company or board

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        # The list endpoint without content is small and supports ETags; full descriptions
        # are downloaded only for postings we have not stored yet.
        base = f"https://boards-api.greenhouse.io/v1/boards/{self.board}/jobs"
        data = self._get_json_cached(http, base)
        if data is None:
            return []
        listing = data.get("jobs", [])
        new_ids = {j["id"] for j in listing} - {
            int(x.split(":", 1)[1]) for x in known_job_ids(self.kind, [f"{self.board}:{j['id']}" for j in listing])
        }
        content: dict[int, str] = {}
        if len(new_ids) > self.DETAIL_FETCH_LIMIT:
            content = {j["id"]: j.get("content", "") for j in http.get_json(f"{base}?content=true").get("jobs", [])}
        else:
            for jid in new_ids:
                try:
                    content[jid] = http.get_json(f"{base}/{jid}").get("content", "")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 404:  # 404: closed between list and detail
                        raise
        jobs = []
        for j in listing:
            if j["id"] in new_ids and j["id"] not in content:
                continue  # vanished before we could read it
            jobs.append(
                RawJob(
                    source=self.kind,
                    source_job_id=f"{self.board}:{j['id']}",
                    title=j.get("title", ""),
                    company=j.get("company_name") or self.company,
                    url=j["absolute_url"],
                    apply_url=j["absolute_url"],
                    location=(j.get("location") or {}).get("name", ""),
                    description=html_to_text(content.get(j["id"], "")),
                    # first_published is the real publication time; updated_at is not
                    posted_at=parse_iso(j.get("first_published")),
                    expires_at=parse_iso(j.get("application_deadline")),
                    raw={"id": j["id"], "updated_at": j.get("updated_at")},
                )
            )
        return jobs


class LeverSource(JobSource):
    kind = "lever"
    method = "Official public Postings API"

    def __init__(self, company_slug: str, company: str = "", **kw) -> None:
        super().__init__(f"lever:{company_slug}", **kw)
        self.slug, self.company = company_slug, company or company_slug

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        data = self._get_json_cached(http, f"https://api.lever.co/v0/postings/{self.slug}?mode=json")
        if data is None:
            return []
        jobs = []
        for j in data:
            cats = j.get("categories") or {}
            locations = cats.get("allLocations") or [cats.get("location", "")]
            parts = [j.get("descriptionPlain", "")]
            for lst in j.get("lists", []) or []:
                parts.append(lst.get("text", ""))
                parts.append(html_to_text(lst.get("content", "")))
            parts.append(j.get("additionalPlain", ""))
            salary = ""
            if j.get("salaryRange"):
                sr = j["salaryRange"]
                salary = f"{sr.get('currency', '')} {sr.get('min', '')}-{sr.get('max', '')} {sr.get('interval', '')}".strip()
            jobs.append(
                RawJob(
                    source=self.kind,
                    source_job_id=f"{self.slug}:{j['id']}",
                    title=j.get("text", ""),
                    company=self.company,
                    url=j["hostedUrl"],
                    apply_url=j.get("applyUrl") or j["hostedUrl"],
                    location=", ".join(filter(None, locations)) + (f" ({j['country']})" if j.get("country") else ""),
                    description="\n".join(p for p in parts if p).strip(),
                    salary=salary,
                    posted_at=from_epoch_ms(j.get("createdAt")),
                    is_remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
                    raw={"id": j["id"], "workplaceType": j.get("workplaceType"), "commitment": cats.get("commitment")},
                )
            )
        return jobs


class AshbySource(JobSource):
    kind = "ashby"
    method = "Official public Job Posting API"

    def __init__(self, org: str, company: str = "", **kw) -> None:
        super().__init__(f"ashby:{org}", **kw)
        self.org, self.company = org, company or org

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        data = self._get_json_cached(http, f"https://api.ashbyhq.com/posting-api/job-board/{self.org}?includeCompensation=true")
        if data is None:
            return []
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
            country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
            comp = (j.get("compensation") or {}).get("scrapeableCompensationSalarySummary") or ""
            jobs.append(
                RawJob(
                    source=self.kind,
                    source_job_id=f"{self.org}:{j['id']}",
                    title=j.get("title", ""),
                    company=self.company,
                    url=j["jobUrl"],
                    apply_url=j.get("applyUrl") or j["jobUrl"],
                    location=", ".join(filter(None, locs)) + (f" ({country})" if country else ""),
                    description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                    salary=comp,
                    posted_at=parse_iso(j.get("publishedAt")),
                    is_remote=j.get("isRemote"),
                    raw={"id": j["id"], "employmentType": j.get("employmentType")},
                )
            )
        return jobs


class SourceTemporarilyUnavailable(RuntimeError):
    """The provider is in a maintenance window. Recorded, but not counted as a failure."""


_INDIA_RE = re.compile(r"\b(india|bengaluru|bangalore|chennai|hyderabad|pune|mumbai|gurugram|gurgaon|noida|"
                       r"new delhi|delhi|kolkata|ahmedabad|coimbatore|kochi|thiruvananthapuram|ind)\b", re.I)
_IST = timezone(timedelta(hours=5, minutes=30))


class WorkdaySource(JobSource):
    """Workday-hosted career sites (<tenant>.<wdN>.myworkdayjobs.com/<site>).

    Uses the public, no-login JSON endpoint the career site itself calls (/wday/cxs/...).
    It is not a documented public API, so it is opt-in (``workday.enabled`` in sources.yaml);
    robots.txt is checked on every host. Searches are narrowed to India with the site's own
    location facets when it offers them; details are fetched only for postings not seen before.
    Workday reports the posting DATE only, so detection delay is known to the day, not minute.
    """

    kind = "workday"
    method = "Workday public career-site JSON (no login)"
    PAGE_SIZE = 20
    FACET_TTL_SECONDS = 6 * 3600
    _facet_cache: dict[str, tuple[float, dict]] = {}

    def __init__(self, tenant: str, wd: str, site: str, company: str = "", search_terms: Optional[list[str]] = None,
                 max_pages: int = 1, **kw) -> None:
        super().__init__(f"workday:{tenant}", **kw)
        self.tenant, self.site, self.company = tenant, site, company or tenant
        self.host = f"https://{tenant}.{wd}.myworkdayjobs.com"
        self.api = f"{self.host}/wday/cxs/{tenant}/{site}"
        self.search_terms = search_terms or ["machine learning"]
        self.max_pages = max_pages

    def _call(self, http: PoliteClient, method: str, url: str, **kw) -> Any:
        try:
            resp = http.request(method, url, **kw)
        except RobotsUnreachable as exc:  # Workday serves robots.txt with 5xx during maintenance
            raise SourceTemporarilyUnavailable(f"Workday robots.txt unreachable, usually a maintenance window ({exc})")
        if "community.workday.com" in resp.url.host or "maintenance" in resp.url.path:
            raise SourceTemporarilyUnavailable("Workday maintenance window (redirected to maintenance page)")
        return resp.json()

    def _search(self, http: PoliteClient, text: str, facets: dict, offset: int = 0) -> dict:
        body = {"limit": self.PAGE_SIZE, "offset": offset, "searchText": text, "appliedFacets": facets}
        return self._call(http, "POST", f"{self.api}/jobs", json=body)

    @staticmethod
    def india_facets(data: dict) -> dict:
        """Find the site's own India location filter values in a search response.
        Prefers a country-level value ("India"); otherwise Indian city values."""
        country: dict[str, list[str]] = {}
        cities: dict[str, list[str]] = {}

        def walk(facets: list, parent: str = "") -> None:
            for f in facets or []:
                param = f.get("facetParameter") or parent
                for v in f.get("values") or []:
                    if "id" in v:
                        desc = v.get("descriptor") or ""
                        if desc.strip().lower() == "india":
                            country.setdefault(param, []).append(v["id"])
                        elif _INDIA_RE.search(desc):
                            cities.setdefault(param, []).append(v["id"])
                    elif v.get("values"):
                        walk([v], param)

        walk(data.get("facets") or [])
        return country or cities

    def _facets(self, http: PoliteClient) -> dict:
        cached = self._facet_cache.get(self.api)
        if cached and time.time() - cached[0] < self.FACET_TTL_SECONDS:
            return cached[1]
        facets = self.india_facets(self._search(http, "", {}))
        self._facet_cache[self.api] = (time.time(), facets)
        return facets

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        facets = self._facets(http)
        postings: dict[str, dict] = {}
        for term in [""] + self.search_terms:  # "" = the site's default listing (newest postings)
            for page in range(self.max_pages):
                data = self._search(http, term, facets, offset=page * self.PAGE_SIZE)
                batch = data.get("jobPostings") or []
                for p in batch:
                    if p.get("externalPath"):
                        postings.setdefault(p["externalPath"], p)
                if len(batch) < self.PAGE_SIZE:
                    break
        if not facets:  # no location filter on this site: keep India (or unknown multi-location) postings
            postings = {k: p for k, p in postings.items()
                        if _INDIA_RE.search(f"{p.get('locationsText', '')} {k}") or "Locations" in p.get("locationsText", "")}
        ids = {path: f"{self.tenant}:{path.rsplit('/', 1)[-1]}" for path in postings}
        known = known_job_ids(self.kind, list(ids.values()))
        jobs = []
        for path, p in postings.items():
            if ids[path] in known:  # seen before: no detail request; ingest just refreshes last_seen
                jobs.append(RawJob(source=self.kind, source_job_id=ids[path], title=p.get("title", ""),
                                   company=self.company, url=f"{self.host}/{self.site}{path}"))
                continue
            try:
                info = self._call(http, "GET", f"{self.api}{path}").get("jobPostingInfo") or {}
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue  # closed between list and detail
                raise
            locations = [info.get("location") or p.get("locationsText", "")] + list(info.get("additionalLocations") or [])
            country = (info.get("country") or {}).get("descriptor", "")
            loc_text = ", ".join(dict.fromkeys(filter(None, locations))) + (f" ({country})" if country else "")
            if not facets and not _INDIA_RE.search(loc_text):
                continue
            posted = None
            if info.get("startDate"):  # a date, no time: midnight IST
                try:
                    posted = datetime.fromisoformat(str(info["startDate"])[:10]).replace(tzinfo=_IST)
                except ValueError:
                    posted = None
            url = info.get("externalUrl") or f"{self.host}/{self.site}{path}"
            jobs.append(RawJob(
                source=self.kind,
                source_job_id=ids[path],
                title=info.get("title") or p.get("title", ""),
                company=self.company,
                url=url,
                apply_url=url,
                location=loc_text,
                description=html_to_text(info.get("jobDescription", "")),
                posted_at=posted,
                is_remote=True if re.search(r"\bremote\b", f"{info.get('remoteType', '')} {loc_text}", re.I) else None,
                raw={"req_id": info.get("jobReqId") or (p.get("bulletFields") or [None])[0],
                     "time_type": info.get("timeType"), "posted_on": info.get("postedOn") or p.get("postedOn"),
                     "posted_precision": "day"},
            ))
        return jobs


# ====================================================================== keyed aggregator APIs


class AdzunaSource(JobSource):
    """https://developer.adzuna.com - free key; country 'in' covers Indian job boards.
    Descriptions are truncated snippets, so `limited_details` is set."""

    kind = "adzuna"
    method = "Official REST API (free key)"

    def __init__(self, queries: list[dict], **kw) -> None:
        super().__init__("adzuna", **kw)
        self.queries = queries

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        s = get_settings()
        if not (s.adzuna_app_id and s.adzuna_app_key):
            raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        jobs: dict[str, RawJob] = {}
        for q in self.queries:
            params = {
                "app_id": s.adzuna_app_id,
                "app_key": s.adzuna_app_key,
                "results_per_page": 50,
                "what": q["what"],
                "max_days_old": q.get("max_days_old", 3),
                "sort_by": "date",
                "content-type": "application/json",
            }
            if q.get("where"):
                params["where"] = q["where"]
            data = http.get_json("https://api.adzuna.com/v1/api/jobs/in/search/1", params=params, check_robots=False)
            for j in data.get("results", []):
                salary = ""
                if j.get("salary_min") and j.get("salary_is_predicted") != "1":
                    salary = f"INR {int(j['salary_min']):,}-{int(j.get('salary_max') or j['salary_min']):,} / year"
                jid = str(j["id"])
                jobs[jid] = RawJob(
                    source=self.kind,
                    source_job_id=jid,
                    title=html_to_text(j.get("title", "")),
                    company=(j.get("company") or {}).get("display_name", ""),
                    url=j["redirect_url"],
                    apply_url=j["redirect_url"],
                    location=(j.get("location") or {}).get("display_name", ""),
                    description=html_to_text(j.get("description", "")),
                    salary=salary,
                    posted_at=parse_iso(j.get("created")),
                    limited_details=True,
                    raw={"category": (j.get("category") or {}).get("label")},
                )
        return list(jobs.values())


class JoobleSource(JobSource):
    """https://jooble.org/api/about - free key on request."""

    kind = "jooble"
    method = "Official REST API (free key)"

    def __init__(self, queries: list[dict], **kw) -> None:
        super().__init__("jooble", **kw)
        self.queries = queries

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        key = get_settings().jooble_api_key
        if not key:
            raise RuntimeError("JOOBLE_API_KEY not set")
        jobs: dict[str, RawJob] = {}
        for q in self.queries:
            body = {"keywords": q["keywords"], "location": q.get("location", "India"), "page": "1"}
            data = http.request("POST", f"https://jooble.org/api/{key}", json=body, check_robots=False).json()
            for j in data.get("jobs", []):
                jid = str(j.get("id") or j.get("link"))
                jobs[jid] = RawJob(
                    source=self.kind,
                    source_job_id=jid,
                    title=html_to_text(j.get("title", "")),
                    company=j.get("company", ""),
                    url=j["link"],
                    apply_url=j["link"],
                    location=j.get("location", ""),
                    description=html_to_text(j.get("snippet", "")),
                    salary=j.get("salary", ""),
                    # Jooble's "updated" is a listing refresh time, not the publication date
                    posted_at=None,
                    limited_details=True,
                    raw={"updated": j.get("updated"), "via": j.get("source")},
                )
        return list(jobs.values())


# ====================================================================== RSS


class RSSSource(JobSource):
    kind = "rss"
    method = "RSS/Atom feed"

    def __init__(self, name: str, url: str, company: str = "", **kw) -> None:
        super().__init__(f"rss:{name}", **kw)
        self.url, self.company = url, company

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        resp = http.request("GET", self.url)  # robots.txt checked
        feed = feedparser.parse(resp.content)
        jobs = []
        for e in feed.entries:
            link = e.get("link")
            if not link:
                continue
            published = None
            if e.get("published_parsed"):
                published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
            jobs.append(
                RawJob(
                    source=self.kind,
                    source_job_id=f"{self.name}:{e.get('id') or link}",
                    title=html_to_text(e.get("title", "")),
                    company=self.company or e.get("author", ""),
                    url=link,
                    apply_url=link,
                    description=html_to_text(e.get("summary", "")),
                    posted_at=published,
                    limited_details=len(e.get("summary", "")) < 400,
                )
            )
        return jobs


# ====================================================================== Gmail job-alert emails

# Platform -> (URL regex capturing the job id, canonical URL template).
# The regex matches the platform's PUBLIC JOB-PAGE URL (also when it is embedded, URL-encoded,
# inside a tracking redirect). Patterns other than EMAIL_PARSER_VERIFIED are candidates derived
# from the public URL format and have NOT yet been checked against a real alert email; keep
# those platforms disabled until `scripts/capture_alert_samples.py <platform>` confirms them.
EMAIL_PLATFORMS: dict[str, tuple[re.Pattern, str]] = {
    "linkedin": (re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[\w-]*?-)?(\d{6,})"), "https://www.linkedin.com/jobs/view/{id}"),
    "naukri": (re.compile(r"naukri\.com/job-listings-([\w-]*?-\d{8,})"), "https://www.naukri.com/job-listings-{id}"),
    "instahyre": (re.compile(r"instahyre\.com/(job-\d+[\w-]*)"), "https://www.instahyre.com/{id}"),
    "cutshort": (re.compile(r"cutshort\.io/job/([\w-]+)"), "https://cutshort.io/job/{id}"),
    "wellfound": (re.compile(r"wellfound\.com/jobs/(\d+[\w-]*)"), "https://wellfound.com/jobs/{id}"),
    "indeed": (re.compile(r"indeed\.com/(?:viewjob|rc/clk|pagead/clk|m/viewjob)\?(?:[^\s\"'<>]*?&)?jk=([0-9a-f]{16})"),
               "https://in.indeed.com/viewjob?jk={id}"),
    "glassdoor": (re.compile(r"glassdoor\.[a-z.]+/[^\s\"'<>]*?[?&](?:jl|jobListingId)=(\d{6,})"),
                  "https://www.glassdoor.co.in/job-listing/j?jl={id}"),
    "foundit": (re.compile(r"foundit\.in/job/([a-z0-9-]+-\d{6,})"), "https://www.foundit.in/job/{id}"),
    "iimjobs": (re.compile(r"iimjobs\.com/j/([\w-]+-\d{5,})\.html"), "https://www.iimjobs.com/j/{id}.html"),
    "hirist": (re.compile(r"hirist\.(?:tech|com)/j/([\w-]+-\d{5,})\.html"), "https://www.hirist.tech/j/{id}.html"),
    "timesjobs": (re.compile(r"timesjobs\.com/job-detail/([\w-]+?jobid-[\w=+]+)"), "https://www.timesjobs.com/job-detail/{id}"),
    "internshala": (re.compile(r"internshala\.com/((?:job|internship)/detail/[\w-]+?\d{6,})"), "https://internshala.com/{id}"),
    "shine": (re.compile(r"shine\.com/jobs/([\w-]+/[\w-]+/\d{6,})"), "https://www.shine.com/jobs/{id}"),
}

# Gmail search used when sources.yaml gives no `query` for a platform (sender domain only).
EMAIL_DEFAULT_QUERIES: dict[str, str] = {
    "linkedin": "from:(jobalerts-noreply@linkedin.com OR jobs-listings@linkedin.com OR jobs-noreply@linkedin.com)",
    "naukri": "from:naukri.com",
    "instahyre": "from:instahyre.com",
    "cutshort": "from:cutshort.io",
    "wellfound": "from:wellfound.com",
    "indeed": "from:indeed.com",
    "glassdoor": "from:glassdoor.com",
    "foundit": "from:(foundit.in OR monsterindia.com)",
    "iimjobs": "from:iimjobs.com",
    "hirist": "from:(hirist.tech OR hirist.com)",
    "timesjobs": "from:timesjobs.com",
    "internshala": "from:internshala.com",
    "shine": "from:shine.com",
}

# Platforms whose parser is covered by a REAL alert email in tests/fixtures. Add a platform
# here only together with its sample + expected-cards file (see tests/fixtures/email_alerts/).
EMAIL_PARSER_VERIFIED: set[str] = {"linkedin"}

_JUNK_LINES = re.compile(
    r"^(apply|easy apply|view job|view|see all jobs|actively recruiting|promoted|new|be an early applicant|"
    r"\d+ (applicants|connections?)|.*school alumni.*|.*connections? work here.*|save|share)$",
    re.I,
)


def parse_alert_email(platform: str, html: str, text: str = "") -> list[dict]:
    """Extract job cards (id, url, title, company, location) from a job-alert email.

    Alert emails carry title/company/location only; we deliberately do NOT fetch the
    job pages on these platforms (their terms forbid automated access), so the
    description stays empty and the job is marked ``limited_details``.
    """
    pattern, template = EMAIL_PLATFORMS[platform]
    soup = BeautifulSoup(html or f"<pre>{html_lib.escape(text)}</pre>", "html.parser")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = unquote(unquote(a["href"]))  # tracking redirects often embed the encoded target URL
        m = pattern.search(href)
        if not m:
            continue
        job_id = m.group(1)
        title = " ".join(a.get_text(" ").split())
        if not title or _JUNK_LINES.match(title) or len(title) > 160:
            found.setdefault(job_id, {"id": job_id, "url": template.format(id=quote(job_id)), "title": ""})
            continue
        if found.get(job_id, {}).get("title"):
            continue
        company = location = ""
        node = a
        for _ in range(6):  # climb to the card container and read the lines after the title
            node = node.parent
            if node is None:
                break
            lines = [" ".join(l.split()) for l in node.get_text("\n").split("\n")]
            lines = [l for l in lines if l and not _JUNK_LINES.match(l)]
            if title in lines and len(lines) - lines.index(title) >= 3:
                after = lines[lines.index(title) + 1 :]
                if " · " in after[0]:
                    company, _, location = after[0].partition(" · ")
                else:
                    company, location = after[0], after[1] if len(after) > 1 else ""
                break
        found[job_id] = {
            "id": job_id,
            "url": template.format(id=quote(job_id)),
            "title": title,
            "company": company[:255],
            "location": location[:255],
        }
    return [j for j in found.values() if j.get("title")]


class GmailAlertSource(JobSource):
    kind = "email"
    method = "Job-alert emails via Gmail API"

    def __init__(self, platform: str, query: str, lookback_days: int = 3, **kw) -> None:
        super().__init__(f"email:{platform}", **kw)
        self.platform, self.query, self.lookback_days = platform, query, lookback_days
        self._pending: list[tuple[str, int]] = []
        self.unparsed_emails = 0  # emails from this sender with no recognisable job links

    def fetch(self, http: PoliteClient) -> list[RawJob]:
        from backend.database import ProcessedEmail, session_scope

        ids = gmail_client.list_message_ids(f"({self.query}) newer_than:{self.lookback_days}d", max_results=50)
        with session_scope() as s:
            seen = {
                r.gmail_message_id
                for r in s.query(ProcessedEmail.gmail_message_id).filter(ProcessedEmail.gmail_message_id.in_(ids))
            } if ids else set()
        jobs: list[RawJob] = []
        for mid in ids:
            if mid in seen:
                continue
            msg = gmail_client.get_message(mid)
            received = from_epoch_ms(msg["internal_date_ms"])
            cards = parse_alert_email(self.platform, msg["html"], msg["text"])
            if not cards:
                self.unparsed_emails += 1
            for c in cards:
                jobs.append(
                    RawJob(
                        source=self.name,
                        source_job_id=c["id"],
                        title=c["title"],
                        company=c.get("company", ""),
                        url=c["url"],
                        apply_url=c["url"],
                        location=c.get("location", ""),
                        posted_at=None,  # alert emails don't state the publication time
                        limited_details=True,
                        raw={"gmail_message_id": mid, "alert_received_at": received.isoformat() if received else None,
                             "email_subject": msg["subject"][:200]},
                    )
                )
            self._pending.append((mid, len(cards)))
            if not cards:
                log.warning("%s: no job cards parsed from email %s (%s) - parser may need updating",
                            self.name, mid, msg["subject"][:80])
        return jobs

    def commit(self) -> None:
        from backend.database import ProcessedEmail, session_scope

        super().commit()
        with session_scope() as s:
            for mid, n in self._pending:
                s.merge(ProcessedEmail(gmail_message_id=mid, source=self.name, jobs_extracted=n))
        self._pending = []


def reset_unparsed_emails(platform: str) -> int:
    """Forget emails from which no job could be parsed, so they are read again on the next
    check (e.g. after fixing the parser). Only emails still inside the source's lookback
    window are picked up again."""
    from backend.database import ProcessedEmail, session_scope

    with session_scope() as s:
        return s.query(ProcessedEmail).filter(
            ProcessedEmail.source == f"email:{platform}", ProcessedEmail.jobs_extracted == 0
        ).delete(synchronize_session=False)


# ====================================================================== config loader


def load_sources(path: Optional[str] = None) -> list[JobSource]:
    cfg = yaml.safe_load(Path(path or get_settings().sources_config_path).read_text()) or {}
    default_iv = cfg.get("default_interval_minutes", 5)
    sources: list[JobSource] = []
    for b in cfg.get("greenhouse", []) or []:
        sources.append(GreenhouseSource(b["board"], b.get("company", ""), interval_minutes=b.get("interval_minutes", default_iv)))
    for b in cfg.get("lever", []) or []:
        sources.append(LeverSource(b["company_slug"], b.get("company", ""), interval_minutes=b.get("interval_minutes", default_iv)))
    for b in cfg.get("ashby", []) or []:
        sources.append(AshbySource(b["org"], b.get("company", ""), interval_minutes=b.get("interval_minutes", default_iv)))
    wd = cfg.get("workday") or {}
    if wd.get("enabled"):
        for t in wd.get("tenants", []) or []:
            sources.append(WorkdaySource(
                t["tenant"], t["wd"], t["site"], t.get("company", ""),
                search_terms=t.get("search_terms") or wd.get("search_terms"),
                max_pages=t.get("max_pages", wd.get("max_pages", 1)),
                interval_minutes=t.get("interval_minutes", wd.get("interval_minutes", 5)),
            ))
    for kind, cls in (("adzuna", AdzunaSource), ("jooble", JoobleSource)):
        c = cfg.get(kind) or {}
        if c.get("enabled"):
            sources.append(cls(c["queries"], interval_minutes=c.get("interval_minutes", 30)))
    for r in cfg.get("rss", []) or []:
        sources.append(RSSSource(r["name"], r["url"], r.get("company", ""), interval_minutes=r.get("interval_minutes", 15)))
    email_iv = cfg.get("email_interval_minutes", 1)  # Gmail API quota easily allows 1-minute polling
    for platform, c in (cfg.get("email_alerts") or {}).items():
        if platform not in EMAIL_PLATFORMS:
            raise ValueError(f"Unknown email alert platform '{platform}'")
        if c and c.get("enabled"):
            sources.append(GmailAlertSource(platform, c.get("query") or EMAIL_DEFAULT_QUERIES[platform],
                                            interval_minutes=c.get("interval_minutes", email_iv)))
    return sources


def is_stale(job: RawJob, max_age_days: int) -> bool:
    now = datetime.now(timezone.utc)
    if job.expires_at and job.expires_at < now:
        return True
    return bool(job.posted_at and job.posted_at < now - timedelta(days=max_age_days))
