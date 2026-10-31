# AI/ML Job Alert Agent

Finds newly published junior AI/ML jobs (Chennai · Bengaluru · Remote-India), filters them with
rules first, scores them against your resume with Gemini/OpenAI, and emails each match to your
Gmail. Matches are emailed as soon as a cycle finds them, never batched into a daily digest. It
never applies on your behalf.

```
                       every 1 min                          ┌──────────────────────────┐
  ┌───────────┐  POST /api/run-cycle   ┌──────────────────┐ │ Sources (per-source      │
  │   n8n     │ ─────────────────────▶ │  FastAPI backend │─┤ interval, robots.txt,    │
  │ scheduler │ ◀── summary / errors ─ │  (pipeline.py)   │ │ retries, crawl-delay)    │
  └─────┬─────┘                        └───┬────┬────┬────┘ │ • Greenhouse/Lever/Ashby │
        │ hourly health check              │    │    │      │ • Workday career sites   │
        │ → admin alert email              │    │    │      │   (2/5-min polls, 304s)  │
        ▼                                  │    │    │      │ • Gmail job-alert emails │
  /api/health/sources                      │    │    │      │   (LinkedIn, Naukri,     │
                                           │    │    │      │   Indeed, Glassdoor, …;  │
  ┌──────────────┐  REST (API key)         │    │    │      │   1-min Gmail poll)      │
  │  Streamlit   │ ────────────────────────┘    │    │      └──────────────────────────┘
  │  dashboard   │                              │    │
  └──────────────┘          ┌───────────────────┘    └──────────────┐
                            ▼                                       ▼
                   ┌─────────────────┐                     ┌────────────────┐
                   │ PostgreSQL /    │                     │ Gemini/OpenAI  │ only for jobs that
                   │ Supabase        │                     │ (JSON mode)    │ passed rule filters
                   └─────────────────┘                     └────────────────┘
```

**Pipeline** (`backend/pipeline.py`). n8n ticks every minute (`POST /api/run-cycle?background=true`):
1. **Discovery.** Every source whose own interval has elapsed starts in its own worker thread
   (Greenhouse/Lever/Ashby: 2 min, Workday: 5 min, Gmail: 1 min, aggregators: 30 min). A source
   still running from an earlier tick is skipped, so one slow board never delays the others.
   ATS boards are fetched with `If-None-Match` and answer **304 Not Modified** (0 bytes) when
   nothing changed; full descriptions are downloaded only for postings not seen before.
2. **Dedupe**: `(source, source_job_id)` → canonical URL (tracking params stripped) → a
   **cross-portal** fingerprint that normalises company ("Razorpay Software Pvt Ltd" = "Razorpay"),
   title ("ML Engineer II" = "Machine Learning Engineer 2") and city ("Bangalore" = "Bengaluru")
   within 30 days. Notifications are unique per fingerprint at database level.
3. **Rule filter** (`job_filter.py`, free): AI/ML role, not senior/lead/manager, experience ≤ 2 years,
   location, not expired/stale. Every rejection reason is stored.
4. **AI match** (`ai_matcher.py`) only on survivors: score, matching/missing skills, experience and
   location fit, reasons. Skills returned by the model are **verified against the resume and job
   text**; invented ones are dropped. Hard cap via `MAX_LLM_CALLS_PER_DAY`.
5. **Notify** (`email_service.py`) if score ≥ `MIN_MATCH_SCORE`. A unique notification row is
   written *before* sending (claim-then-send), so a job can never be emailed twice.

Steps 4–5 run in a separate **processing worker** that wakes as soon as discovery stores a
relevant job, so a slow LLM call never delays the next fetch. `POST /api/run-cycle` without
`background=true` still runs everything synchronously (dashboard button, scripts).

## Job sources: what is monitored and how

| Tier | Source | Method | Poll | Realistic publication → email |
|---|---|---|---|---|
| A | **Greenhouse, Lever, Ashby** career boards (31 companies) | Official public job-board APIs; robots.txt checked, Lever crawl-delay honoured, ETag/304 | 2 min | **~2–4 min** |
| A | **Workday** career sites (17 companies; on by default, `workday.enabled: false` turns it off) | Public no-login JSON the career site itself uses; robots.txt checked; India location filter | 5 min | **~5–7 min** (Workday gives the date only, so the delay cannot be measured) |
| – | **Adzuna**, **Jooble** | Official APIs, free key | 30 min | ~30–90 min |
| – | Any **RSS/Atom** feed you add | robots.txt checked | 15 min | feed dependent |
| B | **LinkedIn, Naukri, Instahyre, Cutshort, Wellfound** | Job-alert emails in *your* Gmail (Gmail API) | 1 min | 1–2 min after the alert arrives; platforms usually send alerts daily |
| B | **Indeed, Glassdoor, Foundit, iimjobs, Hirist, TimesJobs, Internshala, Shine** | Job-alert emails; parsers ready, **disabled until a real sample email is verified** ([docs/EMAIL_ALERTS.md](docs/EMAIL_ALERTS.md)) | 1 min | as above |
| C | Google Jobs, SmartRecruiters, Remotive | Not integrated (no permitted access; see dashboard → *Not monitored*) | – | – |

Only Tier A can meet a 5–10 minute target, and the dashboard marks every source as able or
unable to meet it.

**Limitations**:
- LinkedIn, Naukri, Indeed, Glassdoor and the other Tier B portals offer **no permitted public
  job-search API**. Their terms forbid scraping, and they use login walls and anti-bot measures.
  This project does not scrape them or work around those protections. It reads only the alerts
  they send to you. That means the 5–10 minute target **cannot be met for these platforms**: you
  are notified 1–2 minutes after their alert email arrives, and the posting date is reported as
  "Not provided by source". Alert emails also contain only the title, company and location.
  These jobs are marked *limited details* and scored on the title alone. Open the link to check
  the experience required.
- **Parser status:** only the LinkedIn parser is verified against a real email so far. Naukri,
  Instahyre, Cutshort and Wellfound are enabled but unverified. The eight new platforms stay
  disabled until `scripts/capture_alert_samples.py` confirms them with a real email.
- **Workday** is not a documented public API. It is used only through the no-login endpoint
  each career site itself calls, with robots.txt respected, and it can be turned off with
  `workday.enabled: false`. During Workday's maintenance windows the dashboard shows
  *maintenance*, and these are not counted as failures for up to 12 hours.
- **Workaround for speed:** most Indian AI startups and product companies host their careers page
  on Greenhouse, Lever, Ashby or Workday. Add those companies to `config/sources.yaml`; they are
  checked every 2–5 minutes and include the real publication time. On LinkedIn, many companies'
  "Apply" buttons lead to these ATS pages. Follow those links to find more companies to add.
  Check that the board belongs to the company: for example, `jobs.lever.co/linkedin` is a
  "Partner Sandbox", not LinkedIn's careers board.
- **Excluded on purpose:** Remotive (its robots.txt disallows `/api/*` and its jobs are delayed
  24 h); SmartRecruiters (its robots.txt disallows all bots except LinkedInBot); Google Jobs (no
  public search API). Indeed is monitored only through its alert emails, since its public search
  API and RSS feeds were shut down.
- Email parsing uses heuristics. If a platform changes its email layout, the dashboard shows
  "email(s) had no recognisable job links". Capture a sample with
  `scripts/capture_alert_samples.py`, fix `parse_alert_email`, then run
  `scripts/reprocess_alert_emails.py <platform>`.

## Cloud infrastructure and monthly cost

**Recommended:** one small Linux VM running Docker Compose (Postgres + backend + dashboard + n8n).
The whole stack uses about 1–1.3 GB RAM, so pick 2 GB or more.

| Option | Specs | Approx. cost / month* |
|---|---|---|
| **Hetzner Cloud CX22** (recommended) | 2 vCPU, 4 GB RAM, 40 GB SSD | ~€4–5 (≈ ₹400–480) |
| DigitalOcean droplet (Bangalore region) | 1 vCPU, 2 GB | ~$12 (≈ ₹1,000) |
| AWS Lightsail (Mumbai) | 2 GB plan | ~$12 (≈ ₹1,000) |
| Oracle Cloud Always Free (ARM, Mumbai/Hyderabad) | up to 4 OCPU / 24 GB | ₹0, but sign-up and capacity can be difficult |

| Service | Cost |
|---|---|
| Gemini API (Flash model, ≤150 calls/day after filtering) | free tier usually suffices; paid ≈ < $1/month |
| Gmail API | free |
| Adzuna / Jooble API keys | free |
| Supabase (optional instead of local Postgres) | free tier (500 MB) |
| Healthchecks.io dead-man's switch (optional) | free |
| **Total** | **≈ ₹400–500/month on Hetzner, or ₹0 on Oracle Free** |

\*Prices change; check the provider's pricing page before you buy. VM location doesn't matter
for this workload. The APIs and Gmail are global.

## Project layout

```
backend/
  main.py           FastAPI app: run-cycle, resume, jobs/tracker, stats, health
  pipeline.py       discovery workers, processing worker, dedupe, notify-once, retries, admin alerts
  job_sources.py    Greenhouse/Lever/Ashby/Workday/Adzuna/Jooble/RSS/Gmail-alert sources
  source_monitor.py per-source monitoring report (method, frequency, availability, delay)
  migrations.py     ordered, idempotent schema migrations (run at startup)
  job_filter.py     rule-based role / seniority / experience / location / freshness filter
  resume_parser.py  PDF text → verified structured profile
  ai_matcher.py     LLM + keyword scoring with skill verification
  llm_client.py     Gemini / OpenAI REST JSON client with daily budget
  email_service.py  notification + admin alert emails
  gmail_client.py   Gmail API (send + read alerts) via OAuth refresh token
  http_client.py    polite HTTP: robots.txt (RFC 9309), crawl-delay, retry/backoff, ETag/304
  database.py       SQLAlchemy models (Postgres/Supabase)
  config.py         settings from environment
dashboard/app.py    Streamlit dashboard (password protected)
workflows/job_monitor.json   n8n: 1-min background tick, error alert, hourly source-health alert
config/sources.yaml          which companies/feeds/alerts to monitor
scripts/            gmail_auth.py, check_sources.py, capture_alert_samples.py,
                    reprocess_alert_emails.py, verify_deployment.sh, backup_db.sh
tests/              pytest suite (runs against a real Postgres test DB)
docs/DEPLOYMENT.md  step-by-step server deployment (and upgrading an existing one)
docs/EMAIL_ALERTS.md how to set up alerts on each portal and verify its parser
```

## Environment variables

All settings are in `.env` (template: `.env.example`).

| Variable | Purpose |
|---|---|
| `BACKEND_API_KEY` | Shared secret for every `/api/*` call (n8n, dashboard). ≥ 24 random chars; the backend won't start without it. |
| `DASHBOARD_PASSWORD` | Password for the Streamlit dashboard. |
| `N8N_ENCRYPTION_KEY` | Encrypts n8n's stored credentials. Keep it stable. |
| `POSTGRES_USER/PASSWORD/DB` | Bundled Postgres container. |
| `DATABASE_URL` | Optional override, e.g. Supabase. Must start with `postgresql+psycopg://`. |
| `LLM_PROVIDER` | `gemini`, `openai`, or `none` (keyword scoring only). |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | From https://aistudio.google.com/apikey. |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | If you use OpenAI instead. |
| `MAX_LLM_CALLS_PER_DAY` | Hard daily cap; falls back to keyword scoring when reached. |
| `MIN_MATCH_SCORE` | Only jobs scoring ≥ this are emailed (default 60). |
| `MAX_EXPERIENCE_YEARS` | Default 2. |
| `JOB_MAX_AGE_DAYS` | Postings older than this (when the source gives a date) are treated as stale. |
| `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN` | Gmail API OAuth (see below). |
| `NOTIFY_EMAIL_TO` | Where match emails go. |
| `ADMIN_ALERTS_ENABLED` | Email you when a source fails 3 times in a row (max once per 6 h per source). |
| `DRY_RUN_EMAIL` | `true` logs emails instead of sending them. |
| `ADZUNA_APP_ID/KEY`, `JOOBLE_API_KEY` | Optional aggregator APIs (also set `enabled: true` in sources.yaml). |
| `HEALTHCHECKS_PING_URL` | Optional external dead-man's switch. |
| `INTERNAL_SCHEDULER_ENABLED` | Run ticks without n8n (keep `false` when n8n is used). |
| `CYCLE_INTERVAL_SECONDS` | Internal-scheduler tick (default 60). Per-source intervals live in `config/sources.yaml`. |
| `CYCLE_TIME_BUDGET_SECONDS` | Max seconds per scoring/email pass before it yields (default 240). |
| `PROCESSING_WORKER_ENABLED` | Background worker that scores + emails jobs as soon as they are found (default `true`). |

## Gmail API setup (one time, about 10 minutes)

1. https://console.cloud.google.com → create a project, e.g. `job-alert-agent`.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **OAuth consent screen** (Google Auth Platform): User type **External**, app name of your
   choice, your email as support/developer contact. Under **Audience**, add your Gmail as a
   **test user**.
4. **Clients → Create client → Desktop app**. Download the JSON as `client_secret.json`. Never commit it.
5. On your laptop:
   ```bash
   pip install google-auth-oauthlib
   python scripts/gmail_auth.py client_secret.json
   ```
   Sign in, approve **send** and **read** access, and copy the three printed values into `.env`.
6. **Important:** while the app's publishing status is *Testing*, Google expires refresh tokens
   after **7 days**. Under **Audience → Publish app**, switch it to *In production*. For your own
   account, no verification is needed. You'll see an "unverified app" warning when you sign in;
   choose *Advanced → Continue*. Then run step 5 again to get a long-lived token.
7. Security notes: the token can only send mail and read mail; it can't delete anything. It lives
   only in `.env` on the server (`chmod 600 .env`). You can revoke it anytime at
   https://myaccount.google.com/permissions.

**Create the alerts the agent reads.** Set each up to email the same Gmail address:
- LinkedIn → Jobs → search "Machine Learning Engineer", location Chennai / Bengaluru / India-Remote,
  experience *Entry level / Associate* → **Set alert**, frequency **Daily** (the fastest LinkedIn
  offers by email). Repeat for "AI Engineer", "Generative AI", "NLP", "Computer Vision".
- Naukri → save searches with experience 0–2 years and turn on job alerts.
- Instahyre, Cutshort, Wellfound → set role preferences and enable email job recommendations.

## Database

- **Default:** the bundled `postgres:16-alpine` container stores data in the `pgdata` volume, which
  survives restarts and image upgrades. Tables are created automatically at startup.
  `scripts/backup_db.sh` creates a daily compressed dump; schedule it with cron.
- **Supabase:** create a project in the Mumbai region, then copy **Connect → Session pooler** (IPv4).
  Set `DATABASE_URL=postgresql+psycopg://postgres.<ref>:<pwd>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require`,
  and delete the `db` service and the backend's `depends_on: db` in `docker-compose.yml`.

## n8n workflow

`workflows/job_monitor.json` contains:
- **Every minute → POST /api/run-cycle?background=true**. It returns immediately; due sources
  run in the background. On HTTP 5xx or 401 it emails you through `/api/admin-alert`, which
  sends the same alert at most once per 30 minutes.
- **Every hour → GET /api/health/sources** → emails you if any source has not succeeded within
  5× its interval (minimum 10 min). Provider maintenance windows shorter than 12 h are excluded.

## Source monitoring dashboard

Dashboard → **Source health** shows, for every source: integration method, polling frequency,
last successful check, jobs discovered (24 h / 7 d / total), jobs emailed, availability
(available / maintenance / failing) and % successful checks in 24 h. It also shows the
**measured detection delay** and whether the source can meet the 5–10 min target.

Detection delay is measured only for jobs published **after** monitoring of that source began.
For ATS boards it is detection time minus the publication time the board reports. For email
alerts it is measured from when the alert email arrived, because the platform's own delay is
unknown. For Workday it is not measured, because Workday gives dates only. A second table
lists sources that are **not monitored** and why. The data comes from `GET /api/health/sources`.

The backend URL and API key come from n8n's environment (`$env`), so there are no credentials to
configure inside n8n. Import and activate it as described in `docs/DEPLOYMENT.md`.

## Local development & tests

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
createdb jobagent_test                                 # tests use a real Postgres
.venv/bin/python -m pytest -q                          # 88 tests; no email, no LLM calls
.venv/bin/python scripts/check_sources.py greenhouse lever ashby   # live fetch, no DB writes
```
Run everything locally: `cp .env.example .env`, fill it in, set `DRY_RUN_EMAIL=true`, then
`docker compose up -d --build`.

## Deployment

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.
