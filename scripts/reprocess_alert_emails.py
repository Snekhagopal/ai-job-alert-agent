"""Re-read alert emails that produced no jobs (for example after a parser fix).

    python scripts/reprocess_alert_emails.py naukri

Emails from the last 3 days (the Gmail lookback) are parsed again on the next 1-minute check.
Jobs already stored are never duplicated or re-notified.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.job_sources import EMAIL_PLATFORMS, reset_unparsed_emails  # noqa: E402

if len(sys.argv) != 2 or sys.argv[1] not in EMAIL_PLATFORMS:
    sys.exit(__doc__ + "\nPlatforms: " + ", ".join(sorted(EMAIL_PLATFORMS)))
print(f"{reset_unparsed_emails(sys.argv[1])} email(s) will be read again on the next check.")
