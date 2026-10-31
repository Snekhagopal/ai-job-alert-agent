"""Fetch every configured source once WITHOUT touching the database or sending email,
and show how many jobs pass the rule filter. Use it to validate sources.yaml changes.

    python scripts/check_sources.py            # all sources (email sources need Gmail creds)
    python scripts/check_sources.py greenhouse lever ashby
"""
import sys
from collections import Counter

sys.path.insert(0, ".")
from backend.http_client import PoliteClient  # noqa: E402
from backend.job_filter import apply_filters  # noqa: E402
from backend.job_sources import load_sources  # noqa: E402

wanted = set(sys.argv[1:])
http = PoliteClient()
total_pass = 0
for src in load_sources():
    if wanted and src.kind not in wanted and src.name not in wanted:
        continue
    try:
        jobs = src.fetch(http)
    except Exception as exc:
        print(f"FAIL  {src.name:40s} {type(exc).__name__}: {str(exc)[:150]}")
        continue
    results = [(j, apply_filters(j)) for j in jobs]
    passed = [(j, r) for j, r in results if r.passed]
    reasons = Counter(r.reason.split(":")[0] for _, r in results if not r.passed)
    total_pass += len(passed)
    print(f"OK    {src.name:40s} {len(jobs):4d} jobs, {len(passed):3d} pass filter   rejected: {dict(reasons.most_common(4))}")
    for j, r in passed:
        posted = j.posted_at.date().isoformat() if j.posted_at else "date n/a"
        print(f"        + {j.title[:70]:70s} | {j.location[:35]:35s} | {posted} | {r.experience_text or 'exp n/a'}")
http.close()
print(f"\n{total_pass} job(s) would go on to AI scoring.")
