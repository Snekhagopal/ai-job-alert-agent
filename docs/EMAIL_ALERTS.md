# Job-alert emails: setup per platform

LinkedIn, Naukri, Indeed, Glassdoor and the other portals below have no permitted public
job-search API, and their terms forbid scraping. The agent watches them only through the alert
emails they send to the Gmail account it reads (the one whose `GMAIL_REFRESH_TOKEN` is in `.env`).

**What determines the delay.** Gmail is checked every minute, so a job is scored and emailed to
you about 1–2 minutes after the alert arrives. When the platform sends the alert is up to the
platform, usually once a day. **These sources cannot meet a 5–10 minute target**, and the
dashboard says so. Choose the fastest frequency each platform offers.

Platform menus change often. The steps below say what to look for; the exact labels may differ.

## Settings for every platform

- **Email address:** the Gmail account the agent reads.
- **Keywords:** create several narrow alerts instead of one broad one, e.g. "machine learning
  engineer", "AI engineer", "data scientist", "NLP engineer", "computer vision", "GenAI".
- **Experience:** fresher / entry level / 0–2 years, where the filter exists.
- **Location:** Chennai, Bengaluru, and Remote (India).
- **Frequency:** the fastest available ("as soon as posted"/instant > daily > weekly).
- **Spam:** in Gmail, create a filter for each sender (e.g. `from:naukri.com`) with *Never send
  it to Spam*. The agent searches all mail except Spam and Bin, so labels and archiving are fine.

## Platforms

| Platform | Where to create the alert | Parser status |
|---|---|---|
| LinkedIn | Jobs → search with filters (Date posted, Experience level: Internship/Entry level, location) → switch on **Set alert**. Manage under Jobs → *Job alerts*; set frequency and *email*. | ✅ verified with a sample |
| Naukri | Search → on the results page choose **Create job alert** (or *Job alerts* in your profile menu). Also keep *Recommended jobs* emails on. | ⚠️ existing parser, needs a real sample |
| Instahyre | Complete *Job preferences* (role, location, experience); keep email notifications on in *Settings*. Instahyre emails matching opportunities. | ⚠️ needs a real sample |
| Cutshort | Set *Job preferences* in your profile; keep email notifications on. | ⚠️ needs a real sample |
| Wellfound | Set *Job preferences*/filters; turn on job-alert/digest emails in *Settings → Notifications*. | ⚠️ needs a real sample |
| Indeed (in.indeed.com) | Search → **Get new jobs for this search by email** / bell *Job alert* toggle. | 🔒 disabled until sample |
| Glassdoor (glassdoor.co.in) | Search → **Create job alert** toggle → choose frequency. | 🔒 disabled until sample |
| Foundit | Search → **Create job alert** on the results page. | 🔒 disabled until sample |
| iimjobs | Search → **Create job alert** (also check *Settings → Email notifications*). | 🔒 disabled until sample |
| Hirist | Search → **Create job alert** (same family of sites as iimjobs). | 🔒 disabled until sample |
| TimesJobs | Search → **Create job alert** / *Save as alert*. | 🔒 disabled until sample |
| Internshala | Set *Preferences* (jobs and internships, location, category such as Data Science / ML); keep email alerts on. | 🔒 disabled until sample |
| Shine | Search → **Create job alert**. | 🔒 disabled until sample |

## Turning on a platform: from sample to enabled

The parsers for the new platforms recognise each platform's **public job-page links** (also when
they are wrapped in a tracking redirect). Whether a platform's emails contain such links, and
how its job cards are laid out, can only be confirmed with a real email. So:

1. Create the alert and wait for the first alert email to arrive.
2. Save samples with personal data removed (run on a machine with your `.env`):
   ```bash
   python scripts/capture_alert_samples.py indeed --redact "Your Name"
   ```
   It prints the job cards it found and writes
   `tests/fixtures/email_alerts/indeed/<date>_<id>.html` and a `.cards.draft.json`.
3. Compare the printed cards with the email in Gmail.
   - **Correct:** rename the `.cards.draft.json` to `.expected.json`, run `pytest`, add the
     platform to `EMAIL_PARSER_VERIFIED` in `backend/job_sources.py`, and set
     `enabled: true` in `config/sources.yaml`.
   - **Missing or wrong** (for example "0 job card(s) parsed", which happens when every link is an
     opaque tracking redirect): check the `.html` contains no personal data, then share it
     so the parser can be adapted.
4. If a platform was enabled while its parser was broken, its emails were marked as processed
   with 0 jobs. After the fix, read them again (only the last 3 days):
   ```bash
   python scripts/reprocess_alert_emails.py indeed
   ```

The dashboard's *Source health* tab shows a note when an email from a platform had no
recognisable job links. That is the signal a parser needs a new sample.

## What is never done

- No logging in to, scraping or crawling of these portals. The job pages linked from the emails
  are **not** fetched, so these jobs are scored on title, company and location only
  (*limited details*).
- No CAPTCHA, login wall or anti-bot measure is bypassed.
- Nothing is ever applied for on your behalf.
