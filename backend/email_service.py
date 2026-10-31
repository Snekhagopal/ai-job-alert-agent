"""Match notification + admin alert emails (sent through the Gmail API)."""
from __future__ import annotations

import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from backend import gmail_client
from backend.config import get_settings

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _fmt_dt(dt: datetime | None, missing: str) -> str:
    if not dt:
        return missing
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def build_job_email(job) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body). `job` is a database.Job."""
    d = job.match_details or {}
    score = f"{job.match_score}%" if job.match_score is not None else "N/A"
    method = {"llm": "AI analysis", "keyword": "keyword overlap (AI unavailable)",
              "title_only": "estimate from title only", "no_resume": "no resume uploaded"}.get(d.get("method", ""), "")
    if method:
        score = f"{score} ({method})"
    experience = job.experience_text or "Not stated by source"
    posted = _fmt_dt(job.posted_at, "Not provided by source")
    fields = [
        ("Company", job.company or "Not stated"),
        ("Job Role", job.title),
        ("Location", job.location or "Not stated"),
        ("Experience", experience),
        ("Salary", job.salary or "Not disclosed"),
        ("Posted Date", posted),
        ("First Detected At", _fmt_dt(job.first_detected_at, "-")),
        ("Resume Match Score", score),
        ("Matching Skills", ", ".join(d.get("matching_skills") or []) or "None identified"),
        ("Missing Skills", ", ".join(d.get("missing_skills") or []) or "None identified"),
        ("Application URL", job.apply_url or job.url),
    ]
    extra = []
    if d.get("experience_compatibility"):
        extra.append(("Experience Compatibility", d["experience_compatibility"]))
    if d.get("location_compatibility"):
        extra.append(("Location Compatibility", d["location_compatibility"]))
    extra.append(("Source", job.source))
    reasons = d.get("reasons") or []
    concerns = d.get("concerns") or []

    subject = f"NEW AI/ML JOB MATCH FOUND - {job.title}"[:250]

    lines = [f"{k}: {v}" for k, v in fields]
    if job.limited_details:
        lines.append("\nNOTE: This source only provides limited details. Open the link to verify experience and description.")
    lines.append("")
    lines += [f"{k}: {v}" for k, v in extra]
    if reasons:
        lines += ["", "Why this job:"] + [f"  - {r}" for r in reasons]
    if concerns:
        lines += ["", "Concerns:"] + [f"  - {c}" for c in concerns]
    lines += ["", f"Apply here: {job.apply_url or job.url}", "", "-- AI Job Alert Agent (no application was submitted on your behalf)"]
    text = "\n".join(lines)

    e = html.escape
    def cell(k: str, v: str) -> str:
        return f'<a href="{e(v)}">{e(v)}</a>' if k == "Application URL" else e(str(v))

    rows = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#555;vertical-align:top;white-space:nowrap'><b>{e(k)}:</b></td>"
        f"<td style='padding:6px 0'>{cell(k, v)}</td></tr>"
        for k, v in fields + extra
    )
    note = ("<p style='background:#fff4e5;padding:8px 12px;border-radius:6px'>This source only provides limited details. "
            "Open the link to verify experience and description.</p>") if job.limited_details else ""
    reason_html = ("<h3 style='margin:16px 0 4px'>Why this job</h3><ul>" + "".join(f"<li>{e(r)}</li>" for r in reasons) + "</ul>") if reasons else ""
    concern_html = ("<h3 style='margin:16px 0 4px'>Concerns</h3><ul>" + "".join(f"<li>{e(c)}</li>" for c in concerns) + "</ul>") if concerns else ""
    url = e(job.apply_url or job.url)
    html_body = f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:640px">
<h2 style="margin:0 0 12px">{e(job.title)} &middot; {e(job.company or '')}</h2>
{note}<table style="border-collapse:collapse">{rows}</table>{reason_html}{concern_html}
<p style="margin-top:20px"><a href="{url}" style="background:#1a73e8;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none">Open job &amp; apply</a></p>
<p style="color:#888;font-size:12px">Sent by your AI Job Alert Agent. Nothing was submitted on your behalf.</p></div>"""
    return subject, text, html_body


def send_job_notification(job) -> str:
    s = get_settings()
    subject, text, html_body = build_job_email(job)
    if s.dry_run_email:
        log.info("[DRY RUN] would email %s: %s\n%s", s.notify_email_to, subject, text)
        return "dry-run"
    if not s.notify_email_to:
        raise RuntimeError("NOTIFY_EMAIL_TO is not set")
    return gmail_client.send_email(s.notify_email_to, subject, text, html_body)


def send_admin_alert(subject: str, body: str) -> None:
    s = get_settings()
    if not s.admin_alerts_enabled or not s.notify_email_to:
        return
    if s.dry_run_email:
        log.warning("[DRY RUN] admin alert: %s\n%s", subject, body)
        return
    gmail_client.send_email(s.notify_email_to, f"[Job Agent] {subject}", body)
