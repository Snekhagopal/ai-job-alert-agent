"""Streamlit dashboard for the AI Job Alert Agent. Talks to the backend API only."""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

import httpx
import pandas as pd
import streamlit as st

API_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("BACKEND_API_KEY", "")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

st.set_page_config(page_title="AI Job Alert Agent", page_icon="🤖", layout="wide")


# ---------------------------------------------------------------- auth gate
def check_password() -> bool:
    if not PASSWORD:
        st.error("DASHBOARD_PASSWORD is not set. Refusing to start without a password.")
        return False
    if st.session_state.get("authed"):
        return True
    with st.form("login"):
        pw = st.text_input("Dashboard password", type="password")
        if st.form_submit_button("Sign in"):
            if hmac.compare_digest(pw, PASSWORD):
                st.session_state["authed"] = True
                st.rerun()
            st.error("Wrong password")
    return False


if not check_password():
    st.stop()


# ---------------------------------------------------------------- api helpers
def api(method: str, path: str, **kwargs):
    r = httpx.request(method, f"{API_URL}{path}", headers={"X-API-Key": API_KEY}, timeout=300, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text[:500]}")
    return r.json()


@st.cache_data(ttl=60)
def load_stats():
    return api("GET", "/api/stats")


@st.cache_data(ttl=60)
def load_jobs(pipeline_status: str = "", limit: int = 1000):
    params = {"limit": limit}
    if pipeline_status:
        params["pipeline_status"] = pipeline_status
    return pd.DataFrame(api("GET", "/api/jobs", params=params))


@st.cache_data(ttl=60)
def load_sources():
    return api("GET", "/api/health/sources")


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    mins = int(delta.total_seconds() // 60)
    return f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def refresh():
    st.cache_data.clear()
    st.rerun()


# ---------------------------------------------------------------- layout
st.title("🤖 AI/ML Job Alert Agent")
try:
    stats = load_stats()
except Exception as exc:
    st.error(f"Backend unreachable at {API_URL}: {exc}")
    st.stop()

top = st.columns([6, 1])
top[0].caption(f"Last successful source check: **{ago(stats['last_successful_check'])}** · "
               f"LLM calls today: {stats['llm_calls_today']}/{stats['llm_daily_budget']}")
if top[1].button("↻ Refresh"):
    refresh()

ps = stats["by_pipeline_status"]
aps = stats["by_application_status"]
passed_total = ps.get("relevant", 0) + ps.get("notified", 0) + ps.get("low_match", 0)
c = st.columns(6)
c[0].metric("Discovered", stats["total_discovered"])
c[1].metric("Passed filters", passed_total, help="Passed rule filters and were AI-scored")
c[2].metric("Notified", ps.get("notified", 0))
c[3].metric("Applied", aps.get("applied", 0))
c[4].metric("Interviews", aps.get("interview", 0))
c[5].metric("Rejected", aps.get("rejected", 0))

tab_overview, tab_jobs, tab_tracker, tab_resume, tab_sources = st.tabs(
    ["Overview", "Recent jobs", "Application tracker", "Resume", "Source health"]
)

with tab_overview:
    left, right = st.columns(2)
    with left:
        st.subheader("Relevant jobs by company")
        if stats["by_company"]:
            st.bar_chart(pd.DataFrame(stats["by_company"]).set_index("company"), horizontal=True)
        else:
            st.info("No relevant jobs yet.")
        st.subheader("Pipeline funnel")
        funnel = pd.DataFrame(
            [{"stage": k, "count": v} for k, v in sorted(ps.items(), key=lambda kv: -kv[1])]
        )
        if not funnel.empty:
            st.bar_chart(funnel.set_index("stage"))
    with right:
        st.subheader("Relevant jobs by location")
        if stats["by_location"]:
            st.bar_chart(pd.DataFrame(stats["by_location"]).set_index("location"), horizontal=True)
        else:
            st.info("No relevant jobs yet.")
        st.subheader("Match score distribution")
        if stats["match_scores"]:
            bins = pd.cut(pd.Series(stats["match_scores"]), bins=range(0, 101, 10), include_lowest=True)
            dist = bins.value_counts().sort_index()
            dist.index = [f"{int(i.left)}-{int(i.right)}" for i in dist.index]
            st.bar_chart(dist)
        else:
            st.info("No scored jobs yet.")
    st.subheader("Jobs by source")
    st.bar_chart(pd.Series(stats["by_source"], name="jobs"))

with tab_jobs:
    status = st.selectbox(
        "Show", ["relevant,notified", "notified", "low_match", "filtered_out", "duplicate", ""],
        format_func=lambda v: {"": "all discovered", "relevant,notified": "relevant (incl. notified)"}.get(v, v),
    )
    jobs = load_jobs(status)
    if jobs.empty:
        st.info("No jobs in this view yet.")
    else:
        view = jobs[["first_detected_at", "title", "company", "location", "experience_text", "match_score",
                     "pipeline_status", "filter_reason", "source", "apply_url"]].copy()
        view["first_detected_at"] = pd.to_datetime(view["first_detected_at"]).dt.tz_convert("Asia/Kolkata")
        st.dataframe(
            view, use_container_width=True, hide_index=True,
            column_config={
                "apply_url": st.column_config.LinkColumn("Apply", display_text="open"),
                "match_score": st.column_config.ProgressColumn("Match", min_value=0, max_value=100, format="%d%%"),
                "first_detected_at": st.column_config.DatetimeColumn("Detected (IST)", format="DD MMM, hh:mm a"),
            },
        )

with tab_tracker:
    st.caption("Edit the **Status** or **Notes** column, then press Save. Nothing is ever applied automatically.")
    tracked = load_jobs("relevant,notified,low_match")
    if tracked.empty:
        st.info("Relevant jobs will appear here.")
    else:
        statuses = stats["application_statuses"]
        only_active = st.toggle("Hide 'none' and 'not_interested'", value=False)
        if only_active:
            tracked = tracked[~tracked["application_status"].isin(["none", "not_interested"])]
        editable = tracked[["id", "title", "company", "match_score", "application_status", "notes", "apply_url"]].copy()
        edited = st.data_editor(
            editable, hide_index=True, use_container_width=True, key="tracker",
            disabled=["id", "title", "company", "match_score", "apply_url"],
            column_config={
                "application_status": st.column_config.SelectboxColumn("Status", options=statuses, required=True),
                "notes": st.column_config.TextColumn("Notes"),
                "apply_url": st.column_config.LinkColumn("Apply", display_text="open"),
            },
        )
        if st.button("💾 Save changes", type="primary"):
            changed = 0
            merged = edited.merge(editable, on="id", suffixes=("", "_old"))
            for _, row in merged.iterrows():
                if row["application_status"] != row["application_status_old"] or row["notes"] != row["notes_old"]:
                    api("PATCH", f"/api/jobs/{int(row['id'])}",
                        json={"application_status": row["application_status"], "notes": row["notes"] or ""})
                    changed += 1
            st.success(f"Saved {changed} change(s).")
            refresh()

with tab_resume:
    try:
        resume = api("GET", "/api/resume")
    except RuntimeError:
        resume = None
    if resume:
        p = resume["profile"]
        st.success(f"Active resume: **{resume['filename']}** (uploaded {ago(resume['uploaded_at'])})")
        a, b = st.columns(2)
        a.markdown("**Technical skills:** " + ", ".join(p.get("technical_skills", []) or ["-"]))
        a.markdown("**Languages:** " + ", ".join(p.get("programming_languages", []) or ["-"]))
        a.markdown("**AI/ML knowledge:** " + ", ".join(p.get("ai_ml_knowledge", []) or ["-"]))
        a.markdown("**Certifications:** " + ", ".join(p.get("certifications", []) or ["-"]))
        b.markdown(f"**Experience (years):** {p.get('total_experience_years', '-')}")
        for e in p.get("education", []) or []:
            b.markdown(f"🎓 {e.get('degree', '')} — {e.get('institution', '')} {e.get('year') or ''}")
        for pr in p.get("projects", []) or []:
            b.markdown(f"🛠 **{pr.get('name', '')}** — {', '.join(pr.get('technologies', []) or [])}")
        dropped = (p.get("_meta") or {}).get("unverified_items_dropped") or []
        if dropped:
            st.caption("Items the AI suggested but which are not in your resume text (discarded): " + ", ".join(dropped))
    else:
        st.warning("No resume uploaded yet — jobs are filtered but not scored.")
    up = st.file_uploader("Upload / replace resume (PDF)", type=["pdf"])
    if up and st.button("Analyse resume", type="primary"):
        with st.spinner("Extracting and analysing…"):
            try:
                api("POST", "/api/resume", files={"file": (up.name, up.getvalue(), "application/pdf")})
                st.success("Resume analysed. New jobs will be scored against it.")
                refresh()
            except RuntimeError as exc:
                st.error(str(exc))

AVAILABILITY_ICON = {"available": "✅ available", "temporarily unavailable": "🛠 maintenance",
                     "failing / stale": "⚠️ failing / stale", "never checked": "… never checked"}

with tab_sources:
    data = load_sources()
    st.markdown("✅ All sources healthy" if data["healthy"] else f"⚠️ {data['unhealthy_count']} source(s) unhealthy")
    st.caption("Only sources marked *5–10 min target: yes* can deliver an alert within 5–10 minutes of publication. "
               "Email-alert sources depend on when the platform sends its alert (usually daily). "
               "Detection delay is measured only for jobs published after monitoring of that source began.")
    df = pd.DataFrame(data["sources"])
    if not df.empty:
        kinds = sorted(df["kind"].unique())
        pick = st.multiselect("Source type", kinds, default=kinds)
        df = df[df["kind"].isin(pick)]
        view = pd.DataFrame({
            "Source": df["source"],
            "Integration method": df["method"],
            "Every": df["interval_minutes"].map(lambda m: f"{m} min"),
            "Last successful check": df["last_success_at"].map(ago),
            "Jobs 24 h": df["jobs_24h"], "Jobs 7 d": df["jobs_7d"], "Jobs total": df["jobs_total"],
            "Emailed": df["notified_total"],
            "Availability": df["availability"].map(lambda a: AVAILABILITY_ICON.get(a, a)),
            "Checks OK 24 h": df["availability_24h_pct"].map(lambda v: "–" if pd.isna(v) else f"{v:.0f}%"),
            "Detection delay": df["detection_delay"].fillna("–"),
            "Expected delay": df["expected_delay"],
            "5–10 min target": df["meets_5_10_min_target"].map({True: "yes", False: "no"}),
            "Notes": [" · ".join(x for x in (
                f"error: {e[:120]}" if e and not h else "",
                (n or "")[:120],
                "parser not yet verified with a real email" if pv is False else "",
            ) if x) for e, h, n, pv in zip(df["last_error"], df["healthy"], df["last_note"], df["parser_verified"])],
        })
        st.dataframe(view, hide_index=True, use_container_width=True)
        with st.expander("How detection delay is measured, per source"):
            st.dataframe(df[["source", "detection_delay_basis", "publish_to_email_median", "unchanged_24h",
                             "checks_24h", "monitoring_since"]].rename(columns={
                "detection_delay_basis": "basis", "publish_to_email_median": "publish → email (median)",
                "unchanged_24h": "304 unchanged (24 h)", "checks_24h": "checks (24 h)"}),
                hide_index=True, use_container_width=True)
    if data.get("not_monitored"):
        st.subheader("Not monitored")
        st.dataframe(pd.DataFrame(data["not_monitored"]), hide_index=True, use_container_width=True)
    a, b = st.columns(2)
    if a.button("▶ Run a monitoring cycle now"):
        with st.spinner("Checking due sources…"):
            try:
                st.json(api("POST", "/api/run-cycle"))
            except RuntimeError as exc:
                st.error(str(exc))
    if b.button("✉ Send test email"):
        try:
            st.json(api("POST", "/api/test-email"))
        except RuntimeError as exc:
            st.error(str(exc))
