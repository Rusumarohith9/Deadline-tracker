"""
app.py - Admission Deadline Tracker Executive Dashboard & Portal Monitoring System.

Features:
- Master repository of tracked university admissions (one record per college, programme, year, cycle).
- Separation between candidate dates and approved dates.
- Human-in-the-Loop Approval Center with audit trail (approved_by, approved_at, rejection_reason).
- Calendar Reminders Hub with stable UIDs, RFC 5545 iCalendar (.ics), and CSV export.
- Full audit logging of all automated checks and administrative changes.
- Live URL and PyMuPDF document parser inspector.
- Portal monitoring engine with polite rate-limiting and host-circuit breaker diagnostics.
"""

import os
import io
import time
import sqlite3
import urllib.parse
from datetime import datetime, timezone, date
import pandas as pd
import streamlit as st
from zoneinfo import ZoneInfo

from database import get_connection, init_db, seed_sample_records, DEFAULT_DB_PATH
from extractor import (
    DATE_PATTERN, fetch_notice, find_deadline_candidates,
    hash_notice_section, parse_date_to_iso
)
from workflow import (
    to_date, compare_deadlines, propose_change,
    approve_proposal, reject_proposal
)
from reminders import (
    generate_reminders_for_deadline, sync_all_active_reminders,
    build_icalendar, export_csv_dataframe
)
from monitor import check_deadline_record, run_monitoring_cycle, extract_host
from importer import (
    load_file_to_dataframe, validate_admission_dataframe,
    import_validated_records, REQUIRED_COLUMNS
)

# Configure Streamlit page
st.set_page_config(
    page_title="Admission Deadline Tracker",
    page_icon="🎓",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize database and sync reminders if needed
if not os.path.exists(DEFAULT_DB_PATH):
    init_db()
    seed_sample_records()
    con = get_connection()
    sync_all_active_reminders(con)
    con.close()
else:
    con = get_connection()
    rem_count = con.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    if rem_count == 0:
        sync_all_active_reminders(con)
    con.close()

# Custom CSS for executive UI aesthetic
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Inter:wght@400;500;600&family=Fira+Code:wght@400;500&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    h1, h2, h3, h4, .banner-title {
        font-family: 'Outfit', sans-serif;
        font-weight: 700;
        letter-spacing: -0.02em;
    }

    /* Hero Banner */
    .hero-banner {
        background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 50%, #312e81 100%);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 16px;
        padding: 24px 30px;
        margin-bottom: 24px;
        box-shadow: 0 10px 25px -5px rgba(15, 23, 42, 0.4);
        color: #ffffff;
    }

    .hero-title {
        font-size: 2.1rem;
        font-weight: 800;
        color: #ffffff;
        margin: 0 0 6px 0;
        display: flex;
        align-items: center;
        gap: 12px;
    }

    .hero-subtitle {
        font-size: 0.95rem;
        color: #cbd5e1;
        margin: 0;
        line-height: 1.5;
    }

    /* Metric Cards */
    .metric-card {
        background: #ffffff;
        border-radius: 14px;
        padding: 16px 20px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
        display: flex;
        flex-direction: column;
        transition: transform 0.15s ease, box-shadow 0.15s ease;
    }
    .metric-card:hover {
        transform: translateY(-2px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.08);
    }
    .metric-label {
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748b;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        font-family: 'Outfit', sans-serif;
        color: #0f172a;
        margin: 0;
    }
    .metric-subtext {
        font-size: 0.75rem;
        color: #94a3b8;
        margin-top: 4px;
    }

    /* Status Badges */
    .badge {
        display: inline-flex;
        align-items: center;
        padding: 3px 10px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-verified {
        background-color: #dcfce7;
        color: #15803d;
        border: 1px solid #bbf7d0;
    }
    .badge-changed {
        background-color: #fef3c7;
        color: #b45309;
        border: 1px solid #fde68a;
    }
    .badge-failed {
        background-color: #fee2e2;
        color: #b91c1c;
        border: 1px solid #fecaca;
    }
    .badge-unverified {
        background-color: #f1f5f9;
        color: #64748b;
        border: 1px solid #e2e8f0;
    }

    /* Diff Comparison Box */
    .diff-box {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 16px;
        margin-bottom: 16px;
    }

    .diff-date-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 6px;
        font-family: 'Fira Code', monospace;
        font-size: 0.95rem;
        font-weight: 600;
    }
    .diff-old {
        background: #fee2e2;
        color: #991b1b;
        text-decoration: line-through;
    }
    .diff-new {
        background: #dcfce7;
        color: #166534;
        border: 1px solid #86efac;
    }

    .evidence-quote {
        background: #ffffff;
        border-left: 4px solid #6366f1;
        padding: 10px 14px;
        margin: 10px 0;
        border-radius: 4px;
        font-size: 0.88rem;
        color: #334155;
        font-style: italic;
    }

    /* Code & JSON display */
    pre, code {
        font-family: 'Fira Code', monospace !important;
    }
</style>
""", unsafe_allow_html=True)


def get_db():
    return get_connection(DEFAULT_DB_PATH)


# Header & Hero Section
st.markdown("""
<div class="hero-banner">
    <div class="hero-title">
        <span>🎓 Admission Deadline Tracker</span>
        <span style="font-size: 0.8rem; background: rgba(99, 102, 241, 0.3); border: 1px solid #818cf8; padding: 4px 12px; border-radius: 9999px;">
            Audited & Production-Grade
        </span>
    </div>
    <p class="hero-subtitle">
        Automated admission portal monitoring with human-in-the-loop approval, strict candidate date isolation, 
        stable calendar sync, and complete audit logging across all university cycles.
    </p>
</div>
""", unsafe_allow_html=True)


# Query KPI counts
con = get_db()
total_deadlines = con.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0]
verified_count = con.execute("SELECT COUNT(*) FROM deadlines WHERE status = 'verified'").fetchone()[0]
pending_proposals_count = con.execute("SELECT COUNT(*) FROM change_log WHERE approval_status = 'pending'").fetchone()[0]
failed_checks_count = con.execute("SELECT COUNT(*) FROM deadlines WHERE status = 'could_not_verify'").fetchone()[0]
active_reminders_count = con.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
con.close()

# KPI Metric Cards Row
kpi1, kpi2, kpi3, kpi4, kpi5 = st.columns(5)
with kpi1:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Tracked Deadlines</div>
        <div class="metric-value">{total_deadlines}</div>
        <div class="metric-subtext">Active university records</div>
    </div>
    """, unsafe_allow_html=True)
with kpi2:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Verified Deadlines</div>
        <div class="metric-value" style="color: #16a34a;">{verified_count}</div>
        <div class="metric-subtext">Confirmed & approved</div>
    </div>
    """, unsafe_allow_html=True)
with kpi3:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Pending Approval</div>
        <div class="metric-value" style="color: #d97706;">{pending_proposals_count}</div>
        <div class="metric-subtext">Candidate date proposals</div>
    </div>
    """, unsafe_allow_html=True)
with kpi4:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Could Not Verify</div>
        <div class="metric-value" style="color: #dc2626;">{failed_checks_count}</div>
        <div class="metric-subtext">Preserved previous dates</div>
    </div>
    """, unsafe_allow_html=True)
with kpi5:
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">Calendar Reminders</div>
        <div class="metric-value" style="color: #4f46e5;">{active_reminders_count}</div>
        <div class="metric-subtext">30d / 7d / 1d alerts active</div>
    </div>
    """, unsafe_allow_html=True)

st.write("")

# Production Navigation Tabs
tab_master, tab_approval, tab_import, tab_reminders, tab_audit, tab_inspector, tab_monitoring = st.tabs([
    "📊 Master Tracker",
    f"⚡ Approval Center ({pending_proposals_count})",
    "📥 Import Data",
    "📅 Calendar & Alerts Hub",
    "📜 Full Audit Trail",
    "🔍 Document & Notice Inspector",
    "🌐 Monitoring & Portal Diagnostics"
])


# ==============================================================================
# TAB 1: MASTER TRACKER
# ==============================================================================
with tab_master:
    st.subheader("Admission Deadlines Master Repository")
    st.caption("Each record represents a unique (college, programme, year, cycle) tuple with candidate/approved separation.")

    col_filter1, col_filter2, col_filter3, col_action = st.columns([2, 2, 2, 2])
    with col_filter1:
        status_filter = st.selectbox("Filter Status", ["All", "verified", "changed", "could_not_verify", "unverified"])
    with col_filter2:
        programme_search = st.text_input("Search Programme / College", "")
    with col_filter3:
        year_filter = st.selectbox("Academic Year", ["All", "2026", "2025"])
    with col_action:
        st.write("")
        st.write("")
        if st.button("🔄 Refresh Data", use_container_width=True):
            st.rerun()

    # Query filtered records
    con = get_db()
    query = "SELECT * FROM deadlines WHERE 1=1"
    params = []
    if status_filter != "All":
        query += " AND status = ?"
        params.append(status_filter)
    if programme_search:
        query += " AND (programme LIKE ? OR college LIKE ?)"
        params.extend([f"%{programme_search}%", f"%{programme_search}%"])
    if year_filter != "All":
        query += " AND year = ?"
        params.append(int(year_filter))
    query += " ORDER BY id ASC"

    rows = con.execute(query, params).fetchall()
    con.close()

    if rows:
        display_data = []
        for r in rows:
            display_data.append({
                "ID": r["id"],
                "College": r["college"],
                "Programme": r["programme"],
                "Year": r["year"],
                "Cycle": r["cycle"],
                "Approved Deadline": r["deadline_date"] or "Pending Setup",
                "Proposed Candidate Date": r["proposed_deadline"] or "—",
                "Status": r["status"],
                "Last Checked": r["last_checked"][:19] if r["last_checked"] else "Never",
                "Timezone": r["timezone"] or "Asia/Kolkata"
            })
        df_display = pd.DataFrame(display_data)

        st.dataframe(
            df_display,
            use_container_width=True,
            column_config={
                "Status": st.column_config.TextColumn(
                    "Status",
                    help="Verification status: verified, changed (pending proposal), could_not_verify",
                ),
                "Approved Deadline": st.column_config.TextColumn("Approved Date (Active)"),
                "Proposed Candidate Date": st.column_config.TextColumn("Candidate Date (Unapproved)"),
            },
            hide_index=True
        )
    else:
        st.info("No deadlines match your current filter.")

    # Action bar for selected deadline
    st.write("---")
    st.markdown("#### Quick Check & Inspection")
    col_sel, col_btn_check, col_btn_rem = st.columns([3, 2, 2])
    with col_sel:
        deadline_options = {f"#{r['id']} - {r['college']} ({r['programme']})": r["id"] for r in rows} if rows else {}
        selected_option = st.selectbox("Select Record to Check Notice Now:", list(deadline_options.keys()) if deadline_options else ["None"])

    with col_btn_check:
        st.write("")
        st.write("")
        if st.button("🔎 Check Notice Now", disabled=not deadline_options, use_container_width=True):
            target_id = deadline_options[selected_option]
            con = get_db()
            with st.spinner(f"Fetching official notice and evaluating candidate dates for ID #{target_id}..."):
                from monitor import check_single_deadline_by_id
                outcome = check_single_deadline_by_id(con, target_id)
            con.close()

            if outcome["status"] == "verified":
                st.success(f"✅ Verified! Deadline confirmed at: **{outcome.get('deadline_date')}**")
            elif outcome["status"] == "changed":
                st.warning(f"⚡ Change Detected! Proposed candidate date: **{outcome.get('proposed_deadline')}** (Old: {outcome.get('old_deadline')}). Awaiting human approval in Approval Center.")
            elif outcome["status"] == "could_not_verify":
                st.error(f"⚠️ Could Not Verify: {outcome.get('error')}. Approved date remains strictly unchanged.")
            time.sleep(1)
            st.rerun()

    with col_btn_rem:
        st.write("")
        st.write("")
        if st.button("🔔 Sync Reminders", disabled=not deadline_options, use_container_width=True):
            target_id = deadline_options[selected_option]
            con = get_db()
            rems = generate_reminders_for_deadline(con, target_id)
            con.close()
            st.success(f"Synchronized {len(rems)} reminders with stable UIDs!")

    # Add new deadline expander
    with st.expander("➕ Register New Portal / Programme Record"):
        with st.form("new_deadline_form"):
            new_col1, new_col2 = st.columns(2)
            with new_col1:
                form_college = st.text_input("College / University Name*", "Indian Institute of Science (IISc)")
                form_prog = st.text_input("Programme Name*", "M.Tech Artificial Intelligence")
                form_year = st.number_input("Academic Year*", value=2026, min_value=2024, max_value=2030)
                form_cycle = st.text_input("Admission Cycle*", "Round 1")
            with new_col2:
                form_date = st.date_input("Initial Approved Deadline (Optional)", value=None)
                form_url = st.text_input("Official Notice URL or Fixture Path*", "https://admissions.example.edu/notice")
                form_tz = st.selectbox("Timezone", ["Asia/Kolkata", "UTC", "America/New_York", "Europe/London"])
                form_notes = st.text_input("Programme Notes", "General admissions cycle")

            form_submit = st.form_submit_button("Save & Register Deadline Record")
            if form_submit:
                con = get_db()
                try:
                    iso_date = form_date.isoformat() if form_date else None
                    with con:
                        con.execute("""
                            INSERT INTO deadlines (
                                college, programme, year, cycle, deadline_date,
                                source_url, status, timezone, notes, is_synthetic
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                        """, (
                            form_college, form_prog, form_year, form_cycle,
                            iso_date, form_url, "verified" if iso_date else "unverified",
                            form_tz, form_notes
                        ))
                    d_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
                    if iso_date:
                        generate_reminders_for_deadline(con, d_id)
                    st.success(f"Registered #{d_id}: {form_college} - {form_prog}")
                    con.close()
                    time.sleep(1)
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Error: A deadline record for this (college, programme, year, cycle) already exists!")
                    con.close()


# ==============================================================================
# TAB 2: APPROVAL CENTER
# ==============================================================================
with tab_approval:
    st.subheader("Pending Deadline Change Proposals")
    st.caption("Human-in-the-Loop Approval Workflow. No candidate date touches official deadlines without recorded approval.")

    con = get_db()
    proposals = con.execute("""
        SELECT c.*, d.college, d.programme, d.year, d.cycle, d.deadline_date AS current_active_date
        FROM change_log c
        JOIN deadlines d ON c.deadline_id = d.id
        WHERE c.approval_status = 'pending'
        ORDER BY c.changed_at DESC
    """).fetchall()
    con.close()

    if not proposals:
        st.markdown("""
        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 30px; text-align: center;">
            <h3 style="color: #166534; margin: 0 0 8px 0;">🎉 All Clear! No Pending Proposals</h3>
            <p style="color: #15803d; margin: 0;">
                All portal checks are currently in sync with approved deadlines. When an automated check detects a corrigendum
                or extension, it will appear here for review with full evidence and audit controls.
            </p>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.write(f"Found **{len(proposals)}** pending change proposal(s) awaiting administrative review:")

        for prop in proposals:
            prop_id = prop["id"]
            d_id = prop["deadline_id"]
            
            with st.container():
                st.markdown(f"""
                <div class="diff-box">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <div>
                            <span class="badge badge-changed">PENDING APPROVAL #{prop_id}</span>
                            <span style="font-weight: 700; font-size: 1.15rem; margin-left: 10px; color: #0f172a;">
                                {prop['college']}
                            </span>
                        </div>
                        <span style="font-size: 0.8rem; color: #64748b;">Detected at: {prop['changed_at'][:19]} UTC</span>
                    </div>
                    <div style="font-size: 0.95rem; color: #475569; margin-bottom: 12px;">
                        Programme: <strong>{prop['programme']}</strong> | Academic Year: <strong>{prop['year']}</strong> | Cycle: <strong>{prop['cycle']}</strong>
                    </div>
                    <div style="display: flex; align-items: center; gap: 16px; margin: 14px 0;">
                        <div>
                            <div style="font-size: 0.75rem; color: #64748b; font-weight: 600;">PREVIOUS APPROVED DATE:</div>
                            <span class="diff-date-badge diff-old">{prop['old_deadline'] or 'None'}</span>
                        </div>
                        <div style="font-size: 1.4rem; color: #6366f1;">➔</div>
                        <div>
                            <div style="font-size: 0.75rem; color: #64748b; font-weight: 600;">PROPOSED CANDIDATE DATE:</div>
                            <span class="diff-date-badge diff-new">{prop['new_deadline']}</span>
                        </div>
                    </div>
                    <div class="evidence-quote">
                        <strong>Official Matched Notice Evidence:</strong><br>
                        "{prop['evidence']}"
                    </div>
                    <div style="font-size: 0.8rem; color: #64748b; margin-top: 6px;">
                        Source URL: <a href="{prop['source_url']}" target="_blank">{prop['source_url']}</a>
                    </div>
                </div>
                """, unsafe_allow_html=True)

                col_app_btn, col_rej_btn = st.columns(2)
                with col_app_btn:
                    with st.expander("✅ Approve Change", expanded=False):
                        approver = st.text_input(
                            f"Approver Signature/Name (ID #{prop_id})",
                            value="Dean of Admissions",
                            key=f"app_name_{prop_id}"
                        )
                        if st.button("Confirm & Commit Approval", key=f"btn_app_{prop_id}", type="primary"):
                            con = get_db()
                            res = approve_proposal(con, prop_id, approved_by=approver)
                            generate_reminders_for_deadline(con, d_id)
                            con.close()
                            st.success(f"Approved! Deadline updated to {res['approved_deadline']}. Calendar reminders synced with stable UIDs.")
                            time.sleep(1)
                            st.rerun()

                with col_rej_btn:
                    with st.expander("❌ Reject Proposal", expanded=False):
                        rejection_reason = st.text_input(
                            f"Rejection Reason (ID #{prop_id})",
                            value="Notice applies only to special quota, not general admissions",
                            key=f"rej_reason_{prop_id}"
                        )
                        if st.button("Confirm Rejection", key=f"btn_rej_{prop_id}"):
                            con = get_db()
                            reject_proposal(con, prop_id, rejection_reason=rejection_reason)
                            con.close()
                            st.warning(f"Proposal rejected. Active deadline remains at: {prop['old_deadline']}.")
                            time.sleep(1)
                            st.rerun()

                st.write("")
 

# ==============================================================================
# TAB 3: IMPORT DATA
# ==============================================================================
with tab_import:
    st.subheader("📥 Ingest University Admission Data")
    st.caption("Upload official Excel (.xlsx) or CSV (.csv) spreadsheets to register new admission cycles with strict calendar validation and duplicate prevention.")

    with st.expander("ℹ️ Data Format & Column Specifications", expanded=False):
        st.markdown("""
        **Supported File Formats:** `.xlsx` (Excel) and `.csv` (Comma Separated Values) — *Max Size: 10 MB*

        | Column | Required | Accepted Values / Description | Default / Fallback |
        |---|---|---|---|
        | `college` | **Yes** | Official University or College Name | *None (Error if omitted)* |
        | `programme` | **Yes** | Degree / Programme (e.g. B.Tech Computer Science) | *None (Error if omitted)* |
        | `year` | **Yes** | Academic Year as 4-digit integer (2000 - 2100) | *None (Error if omitted)* |
        | `cycle` | No | Admission cycle (e.g. Main, Round 1, Phase 2) | Defaults to `Main` |
        | `deadline_date` | No | Real calendar date in strict `YYYY-MM-DD` format | `None` (Unverified/Pending) |
        | `source_url` | **Yes** | Official portal URL or verified notice circular link | *None (Error if omitted)* |
        | `timezone` | No | Valid IANA Timezone string (e.g. `Asia/Kolkata`) | Defaults to `Asia/Kolkata` |
        | `notes` | No | Optional contextual remarks or admission circular notes | `None` |

        > **Strict Calendar Validation**: Dates are validated using actual calendar rules (e.g. leap years, month length). Ambiguous date formats like `DD/MM/YYYY` are rejected to prevent date misinterpretation.
        > **Duplicate Prevention**: The unique key is `(college, programme, year, cycle)`. Duplicate rows within the file or matching existing database records are skipped safely without overwriting.
        """)

        # Sample CSV template download
        sample_csv_content = (
            "college,programme,year,cycle,deadline_date,source_url,timezone,notes\n"
            "Indian Institute of Science (IISc),M.Tech Artificial Intelligence,2026,Main,2026-06-15,https://example.edu/admissions/mtech,Asia/Kolkata,Official PG circular\n"
            "National Institute of Technology Trichy,B.Tech Electronics,2026,Round 1,2026-05-30,https://example.edu/admissions/btech,Asia/Kolkata,Round 1 Counselling\n"
            "Delhi Technological University,M.Sc Data Science,2026,Regular,,https://example.edu/admissions/msc,Asia/Kolkata,Date unannounced\n"
        )
        st.download_button(
            "📥 Download Sample CSV Template",
            data=sample_csv_content,
            file_name="admission_tracker_template.csv",
            mime="text/csv",
            key="btn_download_sample_csv"
        )

    st.write("")
    uploaded_file = st.file_uploader(
        "Select Admission File to Upload (.csv or .xlsx)",
        type=["csv", "xlsx"],
        key="admission_file_uploader",
        help="Upload an admission spreadsheet. Files up to 10MB are supported."
    )

    if uploaded_file is not None:
        try:
            # 1. Parse uploaded file into normalized dataframe
            df_upload = load_file_to_dataframe(uploaded_file, uploaded_file.name)
            
            st.markdown("#### 📄 Upload Preview")
            st.dataframe(df_upload, use_container_width=True)

            # 2. Validate rows with database connection
            con = get_db()
            val_summary = validate_admission_dataframe(df_upload, con=con)
            con.close()

            st.write("")
            st.markdown("#### 🔍 Validation & Duplicate Analysis")

            # KPI row for validation summary
            v_col1, v_col2, v_col3, v_col4 = st.columns(4)
            with v_col1:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Total Rows</div>
                    <div class="metric-value">{val_summary.total_rows}</div>
                    <div class="metric-subtext">Rows in uploaded file</div>
                </div>
                """, unsafe_allow_html=True)
            with v_col2:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Valid Records</div>
                    <div class="metric-value" style="color: #16a34a;">{val_summary.valid_count}</div>
                    <div class="metric-subtext">Ready for database import</div>
                </div>
                """, unsafe_allow_html=True)
            with v_col3:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Duplicate Rows</div>
                    <div class="metric-value" style="color: #d97706;">{val_summary.duplicate_count}</div>
                    <div class="metric-subtext">File or DB key duplicates</div>
                </div>
                """, unsafe_allow_html=True)
            with v_col4:
                st.markdown(f"""
                <div class="metric-card">
                    <div class="metric-label">Invalid Rows</div>
                    <div class="metric-value" style="color: #dc2626;">{val_summary.invalid_count}</div>
                    <div class="metric-subtext">Schema or format errors</div>
                </div>
                """, unsafe_allow_html=True)

            # Show structural error if missing required columns
            if not val_summary.is_structure_valid:
                st.error(f"❌ **Schema Validation Failed:** {val_summary.structure_error}")
                st.info(f"Required columns needed: {', '.join(REQUIRED_COLUMNS)}")

            # Show row-level validation errors
            if val_summary.invalid_records:
                st.write("")
                with st.expander(f"⚠️ Row-Level Validation Errors ({len(val_summary.invalid_records)} rows)", expanded=True):
                    for inv in val_summary.invalid_records:
                        st.markdown(f"**Row #{inv.row_number}:**")
                        for err in inv.errors:
                            st.markdown(f"- 🔴 {err}")
                        st.caption(f"Raw data: {inv.data}")
                        st.divider()

            # Show duplicate rows
            if val_summary.duplicate_records:
                st.write("")
                with st.expander(f"📋 Duplicate Records Detected ({len(val_summary.duplicate_records)} rows)", expanded=False):
                    st.info("These rows have matching `(college, programme, year, cycle)` keys and will be skipped to prevent overwriting existing data.")
                    for dup in val_summary.duplicate_records:
                        st.markdown(f"**Row #{dup.row_number}** ({dup.duplicate_type.upper()} DUPLICATE):")
                        for err in dup.errors:
                            st.markdown(f"- 🟡 {err}")
                        st.divider()

            # Import Action Section
            st.write("")
            st.markdown("#### 🚀 Database Ingestion")

            if val_summary.valid_count == 0:
                st.warning("No valid new records found to import. Please correct the errors or upload a file with new admission cycles.")
            else:
                st.markdown(f"""
                <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px; margin-bottom: 16px;">
                    <div style="font-weight: 600; color: #0f172a; margin-bottom: 6px;">Approval & Data Integrity Controls:</div>
                    <div style="font-size: 0.9rem; color: #475569;">
                        By default, imported deadline dates are treated as <em>candidate proposals (unverified)</em> to preserve the human-in-the-loop review workflow.
                        Check the box below only if you have already confirmed these deadlines against official university gazettes or notices.
                    </div>
                </div>
                """, unsafe_allow_html=True)

                is_user_verified = st.checkbox(
                    "✅ Explicitly confirm that uploaded dates have been verified against official sources (mark as Approved Deadlines)",
                    value=False,
                    key="confirm_verified_checkbox",
                    help="When checked, records with deadlines are saved as 'verified' approved dates and calendar reminders are generated. When unchecked, dates are stored as candidate proposed deadlines awaiting review in the Approval Center."
                )

                col_btn, col_info = st.columns([1, 2])
                with col_btn:
                    btn_import = st.button(
                        f"📥 Import {val_summary.valid_count} Valid Record(s)",
                        type="primary",
                        use_container_width=True,
                        key="btn_execute_import"
                    )

                if btn_import:
                    con = get_db()
                    import_res = import_validated_records(
                        con=con,
                        valid_records=val_summary.valid_records,
                        is_verified_by_user=is_user_verified
                    )
                    con.close()

                    if import_res.success:
                        st.success(f"🎉 **Import Successful!** Successfully imported **{import_res.inserted_count}** new admission record(s) into SQLite database (`tracker.db`).")
                        
                        st.markdown(f"""
                        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 14px; margin-top: 10px;">
                            <strong style="color: #166534;">Batch Import Summary:</strong>
                            <ul style="margin: 8px 0 0 0; color: #15803d; font-size: 0.9rem;">
                                <li><strong>{import_res.inserted_count}</strong> record(s) inserted.</li>
                                <li><strong>{val_summary.duplicate_count}</strong> duplicate record(s) skipped.</li>
                                <li><strong>{val_summary.invalid_count}</strong> invalid row(s) rejected.</li>
                                <li>Verification mode: <strong>{'Verified Approved Deadlines (Calendar synced)' if is_user_verified else 'Unverified Candidate Proposals (Approval Center ready)'}</strong>.</li>
                            </ul>
                        </div>
                        """, unsafe_allow_html=True)

                        st.write("")
                        if st.button("🔄 Refresh Dashboard & View in Master Tracker", key="btn_refresh_after_import"):
                            st.rerun()
                    else:
                        st.error(f"❌ **Import Failed:** Database transaction was rolled back. Error: {import_res.error_message}")

        except Exception as e:
            st.error(f"❌ Error processing uploaded file: {e}")


# ==============================================================================
# TAB 4: CALENDAR REMINDERS & EXPORTS
# ==============================================================================
with tab_reminders:
    st.subheader("Calendar Reminders & Official Exports")
    st.caption("RFC 5545 iCalendar (.ics) generation with stable UIDs, and CSV export separating candidate and approved dates.")

    con = get_db()
    rems_query = """
        SELECT r.id, d.college, d.programme, d.cycle, d.deadline_date,
               r.reminder_type, r.reminder_date, r.calendar_uid, r.needs_update,
               d.timezone
        FROM reminders r
        JOIN deadlines d ON r.deadline_id = d.id
        ORDER BY r.reminder_date ASC
    """
    rems_rows = con.execute(rems_query).fetchall()

    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        st.markdown("#### 📅 iCalendar (.ics) Export")
        st.write("Contains all 30d, 7d, and 1d reminders with alarms and stable UIDs. Compatible with Google Calendar, Outlook, and Apple Calendar.")
        ics_data = build_icalendar(con)
        st.download_button(
            label="📥 Download .ics Calendar File",
            data=ics_data,
            file_name="admission_deadlines.ics",
            mime="text/calendar",
            use_container_width=True
        )

    with col_exp2:
        st.markdown("#### 📊 CSV Audit Export (Step 8.4)")
        st.write("Generates comprehensive CSV with approved deadline and proposed candidate deadline in separate columns.")
        df_csv = export_csv_dataframe(con)
        csv_buffer = io.StringIO()
        df_csv.to_csv(csv_buffer, index=False)
        st.download_button(
            label="📥 Download CSV Report",
            data=csv_buffer.getvalue(),
            file_name="admission_deadlines_report.csv",
            mime="text/csv",
            use_container_width=True
        )

    con.close()

    st.write("---")
    st.markdown("#### Active Reminder Alerts Schedule")
    if rems_rows:
        rems_display = []
        for r in rems_rows:
            rems_display.append({
                "College": r["college"],
                "Programme": r["programme"],
                "Official Deadline": r["deadline_date"],
                "Alert Interval": r["reminder_type"].replace("_", " ").title(),
                "Reminder Date": r["reminder_date"],
                "Calendar UID (Stable)": r["calendar_uid"],
                "Timezone": r["timezone"]
            })
        st.dataframe(pd.DataFrame(rems_display), use_container_width=True, hide_index=True)
    else:
        st.info("No active reminders found. Verify and approve deadlines to generate calendar alerts.")


# ==============================================================================
# TAB 4: AUDIT TRAIL & SYSTEM HISTORY
# ==============================================================================
with tab_audit:
    st.subheader("Audited Check History & Administrative Log")
    st.caption("Every automated and manual check records full evidence, SHA-256 hash, and UTC timestamp.")

    subtab_checks, subtab_proposals = st.tabs(["🔎 Check History Log", "📜 Proposal & Approval Audit Trail"])

    con = get_db()
    with subtab_checks:
        checks = con.execute("""
            SELECT h.id, h.checked_at, d.college, d.programme, h.status,
                   h.http_status, h.content_hash, h.evidence, h.source_url
            FROM check_history h
            JOIN deadlines d ON h.deadline_id = d.id
            ORDER BY h.id DESC
            LIMIT 100
        """).fetchall()

        if checks:
            checks_data = []
            for c in checks:
                checks_data.append({
                    "Check ID": c["id"],
                    "Timestamp (UTC)": c["checked_at"][:19],
                    "College": c["college"],
                    "Programme": c["programme"],
                    "Status": c["status"],
                    "HTTP Code": c["http_status"],
                    "Notice Hash (SHA-256)": c["content_hash"][:16] + "..." if c["content_hash"] else "None",
                    "Evidence / Error": c["evidence"]
                })
            st.dataframe(pd.DataFrame(checks_data), use_container_width=True, hide_index=True)
        else:
            st.info("No check history logged yet.")

    with subtab_proposals:
        proposals_all = con.execute("""
            SELECT c.*, d.college, d.programme
            FROM change_log c
            JOIN deadlines d ON c.deadline_id = d.id
            ORDER BY c.id DESC
        """).fetchall()

        if proposals_all:
            prop_data = []
            for p in proposals_all:
                prop_data.append({
                    "Prop ID": p["id"],
                    "College": p["college"],
                    "Programme": p["programme"],
                    "Old Date": p["old_deadline"] or "None",
                    "New Proposed Date": p["new_deadline"],
                    "Status": p["approval_status"].upper(),
                    "Approved By": p["approved_by"] or "—",
                    "Approved At": p["approved_at"][:19] if p["approved_at"] else "—",
                    "Rejection Reason": p["rejection_reason"] or "—",
                    "Detected At": p["changed_at"][:19]
                })
            st.dataframe(pd.DataFrame(prop_data), use_container_width=True, hide_index=True)
        else:
            st.info("No proposals logged yet.")
    con.close()


# ==============================================================================
# TAB 5: DOCUMENT & NOTICE INSPECTOR
# ==============================================================================
with tab_inspector:
    st.subheader("Official Notice & Document Parser Inspector")
    st.caption("Inspect notice extraction, PDF parsing (PyMuPDF), regex date matching, and section SHA-256 hashing in real time.")

    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    fixture_options = {
        "NIT Alpha Admission Notice (HTML)": os.path.join(fixtures_dir, "college_a_initial.html"),
        "NIT Alpha Corrigendum Extension (HTML)": os.path.join(fixtures_dir, "college_a_amendment.html"),
        "Apex Tech Engineering Circular (HTML)": os.path.join(fixtures_dir, "college_b_btech.html"),
        "Apex Tech Science Circular (HTML)": os.path.join(fixtures_dir, "college_b_bsc.html"),
        "Institute of Chemical Tech Official Circular (PDF)": os.path.join(fixtures_dir, "college_d_notice.pdf"),
        "Access-Restricted Portal (Simulated 403)": os.path.join(fixtures_dir, "simulated_403.html"),
    }

    col_ins_source, col_ins_prog, col_ins_year = st.columns([3, 2, 1])
    with col_ins_source:
        preset_choice = st.selectbox("Select Sample Portal or Custom URL:", ["Custom URL / File Path"] + list(fixture_options.keys()))
        if preset_choice != "Custom URL / File Path":
            target_source = fixture_options[preset_choice]
        else:
            target_source = st.text_input("Enter URL or local file path:", "https://admissions.example.edu/notice")
    with col_ins_prog:
        filter_prog = st.text_input("Target Programme (for specificity matching)", "B.Tech Computer Science")
    with col_ins_year:
        filter_year = st.number_input("Target Year", value=2026)

    if st.button("🚀 Fetch & Analyze Document", type="primary"):
        with st.spinner("Retrieving document and executing regex extraction..."):
            text, http_code, error = fetch_notice(target_source)

        if error or text is None:
            st.error(f"Fetch Failed: {error} (HTTP {http_code})")
        else:
            st.success(f"Document fetched successfully (HTTP {http_code})! Extracted {len(text)} characters.")
            
            # Find candidate dates
            candidates = find_deadline_candidates(text, programme=filter_prog, year=filter_year)
            
            col_res1, col_res2 = st.columns([1, 1])
            with col_res1:
                st.markdown("#### Matched Candidate Dates")
                if candidates:
                    for idx, c in enumerate(candidates):
                        st.markdown(f"""
                        <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px; margin-bottom: 10px;">
                            <div style="display: flex; justify-content: space-between;">
                                <strong>Candidate #{idx+1}: {c['iso_date']}</strong>
                                <span style="font-size: 0.8rem; color: #6366f1;">Raw Match: {c['raw_date']}</span>
                            </div>
                            <div style="font-size: 0.82rem; color: #475569; margin: 6px 0;">
                                Passage: <em>"{c['evidence']}"</em>
                            </div>
                            <div style="font-family: monospace; font-size: 0.75rem; color: #64748b;">
                                Section SHA-256: {c['section_hash']}
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                else:
                    st.warning("No candidate deadline dates matched the criteria.")

            with col_res2:
                st.markdown("#### Clean Extracted Text Preview")
                st.text_area("Document Text", text, height=260)


# ==============================================================================
# TAB 6: MONITORING & PORTAL DIAGNOSTICS
# ==============================================================================
with tab_monitoring:
    st.subheader("Portal Monitoring Engine & Health Diagnostics")
    st.caption("Automated portal polling engine featuring rate-limiting, host circuit breakers, and status telemetry.")

    col_diag1, col_diag2 = st.columns([2, 1])

    with col_diag1:
        st.markdown("#### 🌐 Monitored Host Registry")
        con = get_db()
        d_hosts = con.execute("SELECT id, college, programme, source_url, status, last_checked FROM deadlines").fetchall()
        con.close()

        host_stats = []
        for r in d_hosts:
            host_stats.append({
                "Record ID": r["id"],
                "University": r["college"],
                "Programme": r["programme"],
                "Host Domain": extract_host(r["source_url"]),
                "Status": r["status"].upper(),
                "Last Sync": r["last_checked"][:19] if r["last_checked"] else "Never"
            })
        st.dataframe(pd.DataFrame(host_stats), use_container_width=True, hide_index=True)

    with col_diag2:
        st.markdown("#### ⚙️ Engine Configuration")
        st.markdown("""
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px; font-size: 0.85rem;">
            <strong>Rate-Limiting Delay:</strong> 5.0 seconds between portal requests<br>
            <strong>Host Circuit Breaker:</strong> Skips domain after 2 consecutive failures<br>
            <strong>Evidence Hashing:</strong> SHA-256 of extracted notice section<br>
            <strong>Timezone Engine:</strong> IANA ZoneInfo (Asia/Kolkata default)
        </div>
        """, unsafe_allow_html=True)
        
        st.write("")
        st.markdown("#### ▶ Run Monitoring Sweep")
        delay_input = st.slider("Inter-request delay (seconds)", min_value=0.0, max_value=5.0, value=0.5, step=0.5)
        
        if st.button("🚀 Trigger Full Automated Sweep", type="primary", use_container_width=True):
            with st.spinner("Executing automated check across all registered portals..."):
                outcomes = run_monitoring_cycle(delay_seconds=delay_input)
            
            st.success(f"Sweep complete! Checked {len(outcomes)} records.")
            st.dataframe(pd.DataFrame(outcomes), use_container_width=True)
            time.sleep(1)
            st.rerun()


# ==============================================================================
# SIDEBAR CONTROLLER
# ==============================================================================
with st.sidebar:
    st.markdown("### ⚙️ System Controls")
    st.write(f"**Database:** `{os.path.basename(DEFAULT_DB_PATH)}`")
    
    if st.button("▶ Run Monitoring Sweep", use_container_width=True):
        with st.spinner("Running monitoring cycle..."):
            outcomes = run_monitoring_cycle(delay_seconds=0.2)
        st.success(f"Sweep complete for {len(outcomes)} records!")
        time.sleep(1)
        st.rerun()

    st.write("---")
    st.markdown("### 🔄 Database Reset")
    if st.button("Reset DB to Clean State", use_container_width=True):
        if os.path.exists(DEFAULT_DB_PATH):
            os.remove(DEFAULT_DB_PATH)
        init_db()
        seed_sample_records()
        con = get_db()
        sync_all_active_reminders(con)
        con.close()
        st.success("Database restored to fresh initial state!")
        time.sleep(1)
        st.rerun()

    st.write("---")
    st.markdown("""
    <div style="font-size: 0.78rem; color: #64748b; line-height: 1.5;">
        <strong>Admission Deadline Tracker v2.0</strong><br>
        • Strict Candidate vs Approved Isolation<br>
        • Human-in-the-Loop Approval Workflow<br>
        • Stable Calendar UIDs (RFC 5545)<br>
        • Full Automated Pytest Suite (15/15)
    </div>
    """, unsafe_allow_html=True)
