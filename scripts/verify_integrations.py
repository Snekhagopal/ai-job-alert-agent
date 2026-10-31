"""Check the Gemini and Gmail integrations with real calls. Never prints a secret.

    python scripts/verify_integrations.py            # Gemini call + Gmail auth/read check
    python scripts/verify_integrations.py --send     # ...and send one test job email to NOTIFY_EMAIL_TO

Reads settings from .env in the current directory, like the backend. Uses no database, so it
runs on your laptop before anything is deployed. The Gemini check makes one small request,
which is not counted in MAX_LLM_CALLS_PER_DAY.
"""
import argparse
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import get_settings  # noqa: E402

PLACEHOLDERS = {"", "you@gmail.com", "change-me"}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    return ok


def mask_email(addr: str) -> str:
    user, _, domain = addr.partition("@")
    return f"{user[:2]}***@{domain}" if domain else "***"


def safe_error(exc: Exception) -> str:
    """Error text without anything that could contain a credential."""
    s = get_settings()
    text = f"{type(exc).__name__}: {exc}"
    for secret in (s.gemini_api_key, s.gmail_client_secret, s.gmail_refresh_token):
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def check_env_file() -> None:
    env = Path(".env")
    if not check(".env present", env.exists(), "run from the project folder; copy .env.example to .env"):
        return
    mode = stat.S_IMODE(os.stat(env).st_mode)
    check(".env only readable by you (chmod 600)", mode & 0o077 == 0, f"mode is {oct(mode)}; run: chmod 600 .env")


def check_gemini() -> None:
    import httpx

    from backend.llm_client import _call_gemini

    s = get_settings()
    if not check("LLM_PROVIDER is gemini", s.llm_provider == "gemini", f"it is '{s.llm_provider}'"):
        return
    if not check("GEMINI_API_KEY set", bool(s.gemini_api_key)):
        return
    try:
        r = httpx.get(f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}",
                      headers={"x-goog-api-key": s.gemini_api_key}, timeout=20)
        if r.status_code == 200:
            check(f"Gemini key accepted, model {s.gemini_model} available", True, r.json().get("displayName", ""))
        else:
            msg = (r.json().get("error") or {}).get("message", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
            check(f"Gemini key / model {s.gemini_model}", False, f"HTTP {r.status_code}: {msg}")
            return
        t0 = time.monotonic()
        out = _call_gemini('Reply with JSON only: {"ok": true, "skills": ["python"]}', "Health check.")
        check("Gemini JSON-mode generation (the call the matcher uses)", '"ok"' in out,
              f"{time.monotonic() - t0:.1f}s, reply: {out.strip()[:80]}")
    except Exception as exc:
        check("Gemini request", False, safe_error(exc))


def check_gmail(send: bool) -> None:
    from backend import gmail_client

    s = get_settings()
    for var in ("gmail_client_id", "gmail_client_secret", "gmail_refresh_token"):
        check(f"{var.upper()} set", bool(getattr(s, var)))
    if not s.gmail_configured:
        return
    try:
        profile = gmail_client.get_service().users().getProfile(userId="me").execute()
        check("Gmail OAuth token valid", True, f"signed in as {mask_email(profile['emailAddress'])}")
    except Exception as exc:
        hint = " (token revoked or expired: run scripts/gmail_auth.py again)" if "invalid_grant" in str(exc) else ""
        check("Gmail OAuth token valid", False, safe_error(exc) + hint)
        return
    try:
        n = len(gmail_client.list_message_ids("newer_than:7d", max_results=10))
        check("Gmail read access (gmail.readonly)", True, f"{n} message(s) visible from the last 7 days")
    except Exception as exc:
        check("Gmail read access (gmail.readonly)", False, safe_error(exc))
    if not send:
        print("      (sending not tested; run again with --send)")
        return
    to_ok = check("NOTIFY_EMAIL_TO set", s.notify_email_to not in PLACEHOLDERS, mask_email(s.notify_email_to))
    if not to_ok:
        return
    if s.dry_run_email:
        check("Real email sending", False, "DRY_RUN_EMAIL=true only logs emails; set it to false in .env")
        return
    try:
        from backend import email_service
        from backend.database import Job, utcnow

        sample = Job(
            id=0, source="test", source_job_id="test", url="https://example.com/job/123",
            apply_url="https://example.com/job/123/apply", title="Junior Machine Learning Engineer (TEST EMAIL)",
            company="Example AI Labs", location="Chennai, India", experience_text="0-2 years", salary="Not disclosed",
            posted_at=None, first_detected_at=utcnow(), limited_details=False, fingerprint="test", match_score=82,
            match_details={"method": "llm", "matching_skills": ["Python", "PyTorch"], "missing_skills": ["Kubernetes"],
                           "reasons": ["Integration test from scripts/verify_integrations.py"]},
        )
        mid = email_service.send_job_notification(sample)
        check("Gmail send (gmail.send)", bool(mid), f"sent to {mask_email(s.notify_email_to)}; check that inbox")
    except Exception as exc:
        check("Gmail send (gmail.send)", False, safe_error(exc))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="also send one real test email")
    ap.add_argument("--only", choices=["gemini", "gmail"])
    a = ap.parse_args()
    check_env_file()
    if a.only in (None, "gemini"):
        check_gemini()
    if a.only in (None, "gmail"):
        check_gmail(a.send)
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
