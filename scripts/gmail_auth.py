"""One-time Gmail OAuth: run on your LAPTOP (needs a browser).

    pip install google-auth-oauthlib
    python scripts/gmail_auth.py path/to/client_secret.json                 # prints the values
    python scripts/gmail_auth.py path/to/client_secret.json --env-file .env # writes them, prints nothing secret

With --env-file the three GMAIL_* lines are added to (or replaced in) that file, which is then
restricted to your user (chmod 600). Nothing secret is shown on screen, so the output is safe to
share. Without it, the values are printed for you to copy into the server's .env yourself.
"""
import argparse
import json
import os
import re
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    for key, value in values.items():
        pattern = re.compile(rf"^\s*{key}\s*=")
        new = f"{key}={value}"
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new
                break
        else:
            lines.append(new)
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client_secret", help="OAuth client JSON downloaded from Google Cloud (Desktop app)")
    ap.add_argument("--env-file", type=Path, help="write the values into this .env instead of printing them")
    a = ap.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(a.client_secret, SCOPES)
    # access_type=offline + prompt=consent guarantees a refresh token is returned
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    client = json.load(open(a.client_secret))["installed"]
    values = {
        "GMAIL_CLIENT_ID": client["client_id"],
        "GMAIL_CLIENT_SECRET": client["client_secret"],
        "GMAIL_REFRESH_TOKEN": creds.refresh_token,
    }
    if not creds.refresh_token:
        raise SystemExit("Google returned no refresh token. Remove the app's access at "
                         "https://myaccount.google.com/permissions and run this again.")
    granted = set(creds.scopes or [])
    if not set(SCOPES) <= granted:
        raise SystemExit(f"Missing permissions: {sorted(set(SCOPES) - granted)}. Run again and tick both boxes.")

    if a.env_file:
        write_env(a.env_file, values)
        print(f"\nWrote GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN to {a.env_file} (chmod 600).")
        print("Scopes granted: gmail.send, gmail.readonly")
        return
    print("\nAdd these to .env on the server (keep them secret):\n")
    for k, v in values.items():
        print(f"{k}={v}")


if __name__ == "__main__":
    main()
