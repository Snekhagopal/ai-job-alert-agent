"""Save REAL job-alert emails from your Gmail as parser test samples, with personal data removed.

Run it on a machine with the Gmail credentials in .env (read-only use of your inbox):

    python scripts/capture_alert_samples.py indeed                 # latest 3 alerts from Indeed
    python scripts/capture_alert_samples.py naukri --limit 5 --redact "Your Name"
    python scripts/capture_alert_samples.py glassdoor --query "from:glassdoor.com subject:jobs"

For each email it writes, under tests/fixtures/email_alerts/<platform>/:
    <date>_<id>.html             the email body (email addresses, the names you pass with
                                 --redact, and opaque tracking tokens removed)
    <date>_<id>.cards.draft.json what the current parser extracts from it

Open the email in Gmail and compare it with the draft. If the cards are right, rename the
draft to <date>_<id>.expected.json; tests/test_email_alert_parser.py then checks it on every
run. If cards are missing or wrong, share the .html file so the parser can be fixed.
Only after that should the platform be enabled in config/sources.yaml.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import gmail_client  # noqa: E402
from backend.job_sources import EMAIL_DEFAULT_QUERIES, EMAIL_PLATFORMS, parse_alert_email  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests/fixtures/email_alerts"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
HREF_RE = re.compile(r"""(href=["'])([^"']+)(["'])""", re.I)


def scrub_url(url: str) -> str:
    """Blank opaque query values (user/session tokens) but keep anything that looks like a
    URL or id the parser may need (embedded target URLs, numeric/hex job ids)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        decoded = unquote(unquote(v))
        if "http" in decoded or "/" in decoded or re.fullmatch(r"[0-9a-f]{16}|\d{4,20}", v) or len(v) <= 12:
            kept.append((k, v))
        else:
            kept.append((k, "REDACTED"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept, safe=":/%"), ""))


def redact(html: str, names: list[str]) -> str:
    html = HREF_RE.sub(lambda m: m.group(1) + scrub_url(m.group(2)) + m.group(3), html)
    html = EMAIL_RE.sub("redacted@example.com", html)
    for n in names:
        html = re.sub(re.escape(n), "Jane Doe", html, flags=re.I)
    return html


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("platform", choices=sorted(EMAIL_PLATFORMS))
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--days", type=int, default=30, help="look back this many days")
    ap.add_argument("--query", help="Gmail search (default: the platform's sender query)")
    ap.add_argument("--redact", action="append", default=[], help="text to remove, e.g. your name (repeatable)")
    a = ap.parse_args()

    query = f"({a.query or EMAIL_DEFAULT_QUERIES[a.platform]}) newer_than:{a.days}d"
    ids = gmail_client.list_message_ids(query, max_results=a.limit)
    if not ids:
        sys.exit(f"No emails matched: {query}\nCreate an alert on {a.platform} first (docs/EMAIL_ALERTS.md).")
    out = OUT / a.platform
    out.mkdir(parents=True, exist_ok=True)
    for mid in ids:
        msg = gmail_client.get_message(mid)
        day = datetime.fromtimestamp(msg["internal_date_ms"] / 1000, tz=timezone.utc).strftime("%Y%m%d")
        stem = f"{day}_{mid[-8:]}"
        html = redact(msg["html"] or f"<pre>{msg['text']}</pre>", a.redact)
        (out / f"{stem}.html").write_text(html)
        cards = parse_alert_email(a.platform, html)
        (out / f"{stem}.cards.draft.json").write_text(json.dumps(cards, indent=2, ensure_ascii=False))
        print(f"\n{stem}  subject: {msg['subject'][:90]}")
        print(f"  {len(cards)} job card(s) parsed" + ("" if cards else "  <-- parser does not understand this email"))
        for c in cards[:10]:
            print(f"   - {c['title'][:60]:60s} | {c.get('company', '')[:25]:25s} | {c.get('location', '')[:25]}")
    print(f"\nSaved to {out}. Check personal data is gone before sharing the .html files.")


if __name__ == "__main__":
    main()
