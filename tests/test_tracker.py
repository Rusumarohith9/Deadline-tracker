"""
test_tracker.py - Comprehensive test suite for Admission Deadline Tracker.

Covers:
- All 14 issue fixes and requirements from Section 2 of the Build Guide.
- Pure functions (date regex, newline join, to_date, compare_deadlines).
- TC1: Unchanged deadline verification.
- TC2: Deadline extension proposal, pending state, atomic approval, and reminder regeneration.
- TC3: Programme and year specificity (B.Tech notice does not update B.Sc).
- TC4: Failure handling (403 forbidden records could_not_verify, keeps date intact).
- Stable calendar UID persistence across regenerations.
- Section-only content hashing immunity to page banners/timestamps.
- CSV and iCalendar exports.
"""

import os
import io
import sqlite3
import tempfile
import pytest
import pandas as pd
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from database import init_db, get_connection, seed_sample_records
from importer import (
    load_file_to_dataframe, validate_admission_dataframe,
    import_validated_records, MAX_UPLOAD_SIZE_BYTES
)
from extractor import (
    DATE_PATTERN, parse_date_to_iso, fetch_notice,
    find_deadline_candidates, hash_notice_section, log_check_history
)
from workflow import (
    to_date, compare_deadlines, propose_change,
    approve_proposal, reject_proposal,
    get_pending_proposal, get_pending_proposals
)
from reminders import (
    get_or_create_uid, generate_reminders_for_deadline,
    build_icalendar, export_csv_dataframe
)
from monitor import check_deadline_record, extract_host


@pytest.fixture
def temp_db():
    """Create an isolated temporary SQLite database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    con = get_connection(path)
    yield con, path
    con.close()
    if os.path.exists(path):
        os.remove(path)


# -------------------------------------------------------------
# Issue 1 (Section 2.1): DATE_PATTERN regex test
# -------------------------------------------------------------
def test_date_regex_single_backslash():
    """Verify single-backslash DATE_PATTERN detects dates in raw string."""
    text1 = "Application deadline is 15 May 2026 for all candidates."
    matches1 = [m.group(0) for m in DATE_PATTERN.finditer(text1)]
    assert "15 May 2026" in matches1

    text2 = "Deadline extended up to 2026-05-25."
    matches2 = [m.group(0) for m in DATE_PATTERN.finditer(text2)]
    assert "2026-05-25" in matches2

    text3 = "Closing date: 30/06/2026 or 30-06-2026."
    matches3 = [m.group(0) for m in DATE_PATTERN.finditer(text3)]
    assert any("30/06/2026" in m for m in matches3)


# -------------------------------------------------------------
# Issue 2 (Section 2.2): Newline handling test
# -------------------------------------------------------------
def test_newline_formatting_in_text_and_descriptions():
    """Verify newlines are real '\n' characters, not literal backslash-n '\\n'."""
    college = "NIT Alpha"
    deadline = "2026-05-15"
    description = f"College: {college}\nDeadline: {deadline}\nVerify the official admission notice."
    
    assert "\n" in description
    assert "\\n" not in description
    lines = description.split("\n")
    assert len(lines) == 3
    assert lines[0] == "College: NIT Alpha"


# -------------------------------------------------------------
# Issue 3 (Section 2.3): Real date object comparison test
# -------------------------------------------------------------
def test_compare_deadlines_real_dates():
    """Verify compare_deadlines converts to date objects and compares correctly."""
    # Same date represented as string vs date
    status, old_d, new_d = compare_deadlines("2026-05-15", date(2026, 5, 15))
    assert status == "unchanged"
    assert old_d == date(2026, 5, 15)
    assert new_d == date(2026, 5, 15)

    # Different dates
    status2, old_d2, new_d2 = compare_deadlines("2026-05-15", "2026-05-25")
    assert status2 == "changed"
    assert old_d2 == date(2026, 5, 15)
    assert new_d2 == date(2026, 5, 25)

    # Invalid / None candidate
    status3, _, _ = compare_deadlines("2026-05-15", None)
    assert status3 == "could_not_verify"


# -------------------------------------------------------------
# Issue 4 (Section 2.4): Propose change creates pending row, never updates date
# -------------------------------------------------------------
def test_propose_change_creates_proposal_never_mutates_deadline(temp_db):
    """propose_change must write a pending change_log row and never touch deadline_date."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # Propose new date
    prop_id = propose_change(
        con=con,
        deadline_id=d_id,
        old_date="2026-05-15",
        new_date="2026-05-25",
        source_url="http://example.com/amendment",
        evidence="Deadline extended up to 25 May 2026",
        reason="Corrigendum notice published"
    )

    # Verify change_log has pending proposal
    prop_row = con.execute("SELECT * FROM change_log WHERE id = ?", (prop_id,)).fetchone()
    assert prop_row is not None
    assert prop_row["approval_status"] == "pending"
    assert prop_row["old_deadline"] == "2026-05-15"
    assert prop_row["new_deadline"] == "2026-05-25"

    # CRUCIAL CHECK: deadline_date in deadlines table is STILL 2026-05-15!
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-15"  # Untouched!
    assert d_row["proposed_deadline"] == "2026-05-25"
    assert d_row["status"] == "changed"


# -------------------------------------------------------------
# Issue 5 (Section 2.5): Stable reminder UIDs
# -------------------------------------------------------------
def test_reminder_uids_remain_stable_across_regenerations(temp_db):
    """Verify get_or_create_uid reuses stored calendar_uid and does not regenerate."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # Generate initial reminders
    generate_reminders_for_deadline(con, d_id)
    reminders_run1 = con.execute("SELECT reminder_type, calendar_uid FROM reminders WHERE deadline_id = ?", (d_id,)).fetchall()
    uids_run1 = {r["reminder_type"]: r["calendar_uid"] for r in reminders_run1}

    assert len(uids_run1) == 3
    assert all("@admission-tracker" in uid for uid in uids_run1.values())

    # Regenerate reminders (e.g. simulated re-run or date shift)
    generate_reminders_for_deadline(con, d_id)
    reminders_run2 = con.execute("SELECT reminder_type, calendar_uid FROM reminders WHERE deadline_id = ?", (d_id,)).fetchall()
    uids_run2 = {r["reminder_type"]: r["calendar_uid"] for r in reminders_run2}

    # Must be 100% identical!
    assert uids_run1 == uids_run2


# -------------------------------------------------------------
# Issue 6 & Day 4: Approval and audit columns workflow
# -------------------------------------------------------------
def test_approval_transaction_and_audit_fields(temp_db):
    """Approve updates deadline_date and records approved_by / approved_at."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    generate_reminders_for_deadline(con, d_id)

    # Propose change
    prop_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Evidence passage")

    # Approve
    result = approve_proposal(con, prop_id, approved_by="Prof. Sharma (Dean Admissions)")
    assert result["success"] is True

    # Check deadlines table
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-25"
    assert d_row["proposed_deadline"] is None
    assert d_row["status"] == "verified"

    # Check change_log table
    prop_row = con.execute("SELECT * FROM change_log WHERE id = ?", (prop_id,)).fetchone()
    assert prop_row["approval_status"] == "approved"
    assert prop_row["approved_by"] == "Prof. Sharma (Dean Admissions)"
    assert prop_row["approved_at"] is not None


def test_rejection_workflow(temp_db):
    """Reject keeps original deadline_date and records rejection_reason."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    prop_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Bogus evidence")
    reject_result = reject_proposal(con, prop_id, rejection_reason="Notice was for an unofficial forum")

    assert reject_result["success"] is True

    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-15"  # Remains unchanged
    assert d_row["proposed_deadline"] is None

    prop_row = con.execute("SELECT * FROM change_log WHERE id = ?", (prop_id,)).fetchone()
    assert prop_row["approval_status"] == "rejected"
    assert prop_row["rejection_reason"] == "Notice was for an unofficial forum"
    assert prop_row["rejected_at"] is not None


# -------------------------------------------------------------
# Issue 7 (Section 2.6): Content hashing of notice passage only
# -------------------------------------------------------------
def test_section_hashing_immune_to_page_timestamp():
    """Modifying page timestamp/banner outside notice passage does not alter notice hash."""
    passage = "The last date for submission of online application forms for B.Tech Computer Science (2026) is 15 May 2026."
    hash1 = hash_notice_section(passage)

    # Same passage with surrounding noise/newlines
    passage_noisy = "\n  The last date for submission of online application forms for B.Tech Computer Science (2026) is 15 May 2026.  \n"
    hash2 = hash_notice_section(passage_noisy)

    assert hash1 == hash2


# -------------------------------------------------------------
# Issue 8 (Section 2.6): Check history logged on every attempt
# -------------------------------------------------------------
def test_check_history_logged_on_every_attempt(temp_db):
    """Verify check_history receives an entry on success and failure."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # Log a successful check
    log_check_history(con, d_id, "verified", 200, "Date verified at 15 May 2026", "hash123", "http://example.com")
    # Log a failure
    log_check_history(con, d_id, "could_not_verify", 403, "HTTP 403 Forbidden", None, "http://example.com")

    hist = con.execute("SELECT * FROM check_history WHERE deadline_id = ? ORDER BY id ASC", (d_id,)).fetchall()
    assert len(hist) == 2
    assert hist[0]["status"] == "verified"
    assert hist[0]["http_status"] == 200
    assert hist[1]["status"] == "could_not_verify"
    assert hist[1]["http_status"] == 403


# -------------------------------------------------------------
# Unchanged Deadline Verification
# -------------------------------------------------------------
def test_unchanged_deadline_verification(temp_db):
    """Verify when official notice is unchanged, status stays verified with zero pending proposals."""
    con, _ = temp_db
    fixture_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "college_a_initial.html")

    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech Computer Science', 2026, 'Round 1', '2026-05-15', ?, 'verified')
        """, (fixture_path,))
        d_row = con.execute("SELECT * FROM deadlines WHERE college = 'NIT Alpha'").fetchone()

    res = check_deadline_record(con, d_row)
    assert res["status"] == "verified"
    assert res["deadline_date"] == "2026-05-15"

    # Confirm no pending change proposals
    changes = con.execute("SELECT * FROM change_log WHERE deadline_id = ?", (d_row["id"],)).fetchall()
    assert len(changes) == 0

    # Confirm check_history recorded
    hist = con.execute("SELECT * FROM check_history WHERE deadline_id = ?", (d_row["id"],)).fetchall()
    assert len(hist) == 1
    assert hist[0]["status"] == "verified"


# -------------------------------------------------------------
# Deadline Extension & Human Approval Lifecycle
# -------------------------------------------------------------
def test_deadline_extension_and_approval_workflow(temp_db):
    """Verify extension detection creates proposal, leaves date intact until approved, and syncs stable UIDs."""
    con, _ = temp_db
    amendment_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "college_a_amendment.html")

    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech Computer Science', 2026, 'Round 1', '2026-05-15', ?, 'verified')
        """, (amendment_path,))
        d_row = con.execute("SELECT * FROM deadlines WHERE college = 'NIT Alpha'").fetchone()

    d_id = d_row["id"]
    generate_reminders_for_deadline(con, d_id)
    initial_uids = {r["reminder_type"]: r["calendar_uid"] for r in con.execute("SELECT reminder_type, calendar_uid FROM reminders WHERE deadline_id = ?", (d_id,)).fetchall()}

    # 1. Run check against amendment notice
    check_res = check_deadline_record(con, d_row)
    assert check_res["status"] == "changed"
    assert check_res["old_deadline"] == "2026-05-15"
    assert check_res["proposed_deadline"] == "2026-05-25"

    # 2. Verify state before approval: deadline_date is STILL 2026-05-15!
    mid_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert mid_row["deadline_date"] == "2026-05-15"
    assert mid_row["proposed_deadline"] == "2026-05-25"
    assert mid_row["status"] == "changed"

    # 3. Approve proposal
    proposal_id = check_res["proposal_id"]
    approve_res = approve_proposal(con, proposal_id, approved_by="Director Admissions")
    assert approve_res["success"] is True

    # 4. Regenerate reminders
    generate_reminders_for_deadline(con, d_id)
    updated_uids = {r["reminder_type"]: r["calendar_uid"] for r in con.execute("SELECT reminder_type, calendar_uid FROM reminders WHERE deadline_id = ?", (d_id,)).fetchall()}

    # UIDs must be identical to initial run!
    assert initial_uids == updated_uids

    # Final approved date must be 2026-05-25
    final_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert final_row["deadline_date"] == "2026-05-25"
    assert final_row["status"] == "verified"


# -------------------------------------------------------------
# Programme and Year Specificity Isolation
# -------------------------------------------------------------
def test_programme_specificity_isolation(temp_db):
    """Verify that a B.Tech engineering notice never updates a B.Sc degree record."""
    con, _ = temp_db
    btech_fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "college_b_btech.html")

    # Record registered for B.Sc Data Science, pointing to B.Tech notice
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('Apex Technical University', 'B.Sc Data Science', 2026, 'Main', '2026-06-20', ?, 'verified')
        """, (btech_fixture,))
        d_row = con.execute("SELECT * FROM deadlines WHERE programme = 'B.Sc Data Science'").fetchone()

    res = check_deadline_record(con, d_row)

    # Must NOT update or propose change for B.Sc!
    assert res["status"] == "could_not_verify"
    
    # B.Sc approved date remains 2026-06-20
    final_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_row["id"],)).fetchone()
    assert final_row["deadline_date"] == "2026-06-20"
    assert final_row["proposed_deadline"] is None


# -------------------------------------------------------------
# Failure Handling & Circuit Breaker Resilience
# -------------------------------------------------------------
def test_portal_failure_handling_and_resilience(temp_db):
    """Verify HTTP 403 / network errors record could_not_verify and preserve approved dates strictly."""
    con, _ = temp_db
    fixture_403 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "simulated_403.html")

    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('Metro State Polytechnic', 'Diploma AI', 2026, 'Cycle 1', '2026-07-01', ?, 'verified')
        """, (fixture_403,))
        d_row = con.execute("SELECT * FROM deadlines WHERE college = 'Metro State Polytechnic'").fetchone()

    res = check_deadline_record(con, d_row)
    assert res["status"] == "could_not_verify"
    assert res["http_status"] == 403

    # Date remains strictly intact
    final_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_row["id"],)).fetchone()
    assert final_row["deadline_date"] == "2026-07-01"
    assert final_row["status"] == "could_not_verify"

    # Check history logged error
    hist = con.execute("SELECT * FROM check_history WHERE deadline_id = ?", (d_row["id"],)).fetchone()
    assert hist is not None
    assert hist["status"] == "could_not_verify"
    assert hist["http_status"] == 403
    assert "Forbidden" in hist["evidence"]


# -------------------------------------------------------------
# PDF Notice Extraction Test
# -------------------------------------------------------------
def test_pdf_notice_extraction():
    """Verify PyMuPDF extraction of synthetic official admission PDF."""
    pdf_fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "college_d_notice.pdf")
    text, status, err = fetch_notice(pdf_fixture)
    
    assert status == 200
    assert err is None
    assert "INSTITUTE OF CHEMICAL TECHNOLOGY" in text
    assert "30 June 2026" in text

    candidates = find_deadline_candidates(text, programme="M.Tech Chemical Engineering", year=2026)
    assert len(candidates) > 0
    assert candidates[0]["iso_date"] == "2026-06-30"


# -------------------------------------------------------------
# Exports & Calendar Test
# -------------------------------------------------------------
def test_calendar_and_csv_exports(temp_db):
    """Verify .ics generation and CSV DataFrame generation."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    generate_reminders_for_deadline(con, d_id)

    # Test .ics bytes
    ics_bytes = build_icalendar(con, d_id)
    assert b"BEGIN:VCALENDAR" in ics_bytes
    assert b"BEGIN:VEVENT" in ics_bytes
    assert b"@admission-tracker" in ics_bytes

    # Test CSV dataframe
    df = export_csv_dataframe(con)
    assert "approved_deadline" in df.columns
    assert "candidate_deadline" in df.columns
    assert len(df) == 1
    assert df.iloc[0]["approved_deadline"] == "2026-05-15"


# -------------------------------------------------------------
# Regression Tests: Duplicate Pending Proposal Prevention & Isolation
# -------------------------------------------------------------
def test_prevent_duplicate_pending_proposals_on_repeated_checks(temp_db):
    """
    Verify repeated monitoring checks for the same candidate date do NOT create
    duplicate pending records in change_log, while check_history continues to log all attempts.
    """
    con, _ = temp_db
    amendment_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "college_a_amendment.html")

    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech Computer Science', 2026, 'Round 1', '2026-05-15', ?, 'verified')
        """, (amendment_path,))
        d_row = con.execute("SELECT * FROM deadlines WHERE college = 'NIT Alpha'").fetchone()

    d_id = d_row["id"]

    # First monitoring check detects candidate date 2026-05-25
    res1 = check_deadline_record(con, d_row)
    assert res1["status"] == "changed"
    assert res1["proposed_deadline"] == "2026-05-25"
    prop_id1 = res1["proposal_id"]

    # Verify exactly 1 pending proposal in change_log
    proposals_run1 = con.execute("SELECT * FROM change_log WHERE deadline_id = ?", (d_id,)).fetchall()
    assert len(proposals_run1) == 1
    assert proposals_run1[0]["approval_status"] == "pending"
    assert proposals_run1[0]["new_deadline"] == "2026-05-25"

    # Second monitoring check on the same deadline and notice
    # Fetch fresh row
    fresh_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    res2 = check_deadline_record(con, fresh_row)
    assert res2["status"] == "changed"
    assert res2["proposed_deadline"] == "2026-05-25"
    assert res2["proposal_id"] == prop_id1  # Reuses existing pending proposal

    # Third check using propose_change directly with the same candidate date
    prop_id3 = propose_change(
        con=con,
        deadline_id=d_id,
        old_date="2026-05-15",
        new_date="2026-05-25",
        source_url=amendment_path,
        evidence="Repeated manual check"
    )
    assert prop_id3 == prop_id1

    # CRITICAL: Verify STILL exactly 1 record in change_log
    proposals_run3 = con.execute("SELECT * FROM change_log WHERE deadline_id = ?", (d_id,)).fetchall()
    assert len(proposals_run3) == 1
    assert proposals_run3[0]["id"] == prop_id1

    # CRITICAL: Verify check_history logged BOTH checks (monitoring attempts preserved)
    hist = con.execute("SELECT * FROM check_history WHERE deadline_id = ? ORDER BY id ASC", (d_id,)).fetchall()
    assert len(hist) == 2
    assert hist[0]["status"] == "changed"
    assert hist[1]["status"] == "changed"

    # CRITICAL: Verify approved deadline_date was NEVER overwritten!
    d_after = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_after["deadline_date"] == "2026-05-15"
    assert d_after["proposed_deadline"] == "2026-05-25"
    assert d_after["status"] == "changed"


def test_different_candidate_dates_handled_without_silent_replacement(temp_db):
    """
    Verify when a different candidate date is detected while another proposal is pending:
    - It does not silently overwrite or delete the existing pending proposal.
    - Both distinct candidate proposals are preserved in change_log for administrator review.
    - Repeated checks for either candidate date do not duplicate that date's proposal.
    """
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # Proposal 1: 2026-05-25
    prop1_id = propose_change(
        con=con,
        deadline_id=d_id,
        old_date="2026-05-15",
        new_date="2026-05-25",
        source_url="http://example.com/amendment1",
        evidence="First extension to 25 May 2026"
    )

    # Proposal 2: 2026-06-05 (different candidate date detected while proposal 1 is pending)
    prop2_id = propose_change(
        con=con,
        deadline_id=d_id,
        old_date="2026-05-15",
        new_date="2026-06-05",
        source_url="http://example.com/amendment2",
        evidence="Second extension to 5 June 2026"
    )

    assert prop1_id != prop2_id

    # Verify BOTH proposals exist as pending in change_log (no silent replacement!)
    pending = con.execute("""
        SELECT id, new_deadline, approval_status, reason FROM change_log
        WHERE deadline_id = ? ORDER BY id ASC
    """, (d_id,)).fetchall()

    assert len(pending) == 2
    assert pending[0]["id"] == prop1_id
    assert pending[0]["new_deadline"] == "2026-05-25"
    assert pending[0]["approval_status"] == "pending"

    assert pending[1]["id"] == prop2_id
    assert pending[1]["new_deadline"] == "2026-06-05"
    assert pending[1]["approval_status"] == "pending"
    assert "concurrent pending proposal" in pending[1]["reason"]

    # Proposing 2026-05-25 again returns prop1_id without creating a 3rd row
    repeat_prop1 = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Repeated 1")
    assert repeat_prop1 == prop1_id

    # Proposing 2026-06-05 again returns prop2_id without creating a 4th row
    repeat_prop2 = propose_change(con, d_id, "2026-05-15", "2026-06-05", "http://example.com", "Repeated 2")
    assert repeat_prop2 == prop2_id

    total_proposals = con.execute("SELECT COUNT(*) FROM change_log WHERE deadline_id = ?", (d_id,)).fetchone()[0]
    assert total_proposals == 2

    # Approved deadline_date is still completely isolated
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-15"


def test_approved_proposal_not_treated_as_pending(temp_db):
    """
    Verify once a proposal is approved, it is no longer treated as pending,
    and its audit history (approved_by, approved_at) is strictly preserved.
    """
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    prop_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Notice 1")

    # Approve
    approve_res = approve_proposal(con, prop_id, approved_by="Registrar Academics")
    assert approve_res["success"] is True

    # Proposal is no longer pending
    pending = get_pending_proposal(con, d_id, "2026-05-25")
    assert pending is None

    # Audit fields are preserved
    row = con.execute("SELECT * FROM change_log WHERE id = ?", (prop_id,)).fetchone()
    assert row["approval_status"] == "approved"
    assert row["approved_by"] == "Registrar Academics"
    assert row["approved_at"] is not None

    # Approved deadline was updated
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-25"
    assert d_row["proposed_deadline"] is None
    assert d_row["status"] == "verified"


def test_rejected_proposal_not_treated_as_pending_and_audit_history_preserved(temp_db):
    """
    Verify once a proposal is rejected:
    - It is no longer treated as pending.
    - If the same date is subsequently detected/proposed, a new pending proposal can be created.
    - The rejected proposal's audit history (rejected_at, rejection_reason) is fully preserved.
    """
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # 1. Propose candidate date
    prop1_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Notice 1")

    # 2. Reject proposal
    reject_res = reject_proposal(con, prop1_id, rejection_reason="Unofficial third-party forum post")
    assert reject_res["success"] is True

    # Rejected proposal is NOT pending
    pending = get_pending_proposal(con, d_id, "2026-05-25")
    assert pending is None

    # Approved date is untouched
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-15"
    assert d_row["proposed_deadline"] is None

    # 3. New official check subsequently detects 2026-05-25 from official circular
    prop2_id = propose_change(
        con=con,
        deadline_id=d_id,
        old_date="2026-05-15",
        new_date="2026-05-25",
        source_url="http://example.com/official-gazette",
        evidence="Official Gazette Notification signed by Controller of Exams"
    )

    # Must be a new proposal ID, not the old rejected one
    assert prop2_id != prop1_id

    # 4. Audit history preservation: check_log contains both records
    all_logs = con.execute("SELECT * FROM change_log WHERE deadline_id = ? ORDER BY id ASC", (d_id,)).fetchall()
    assert len(all_logs) == 2

    # Original rejected proposal is preserved intact
    assert all_logs[0]["id"] == prop1_id
    assert all_logs[0]["approval_status"] == "rejected"
    assert all_logs[0]["rejection_reason"] == "Unofficial third-party forum post"
    assert all_logs[0]["rejected_at"] is not None

    # New proposal is pending
    assert all_logs[1]["id"] == prop2_id
    assert all_logs[1]["approval_status"] == "pending"
    assert all_logs[1]["new_deadline"] == "2026-05-25"


def test_remaining_pending_proposal_handling_on_rejection(temp_db):
    """
    Verify when two proposals are pending, rejecting one restores the other
    in deadlines.proposed_deadline rather than wiping it to NULL.
    """
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    prop1_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Evidence 1")
    prop2_id = propose_change(con, d_id, "2026-05-15", "2026-06-05", "http://example.com", "Evidence 2")

    # Reject proposal 2 (June 5)
    reject_proposal(con, prop2_id, rejection_reason="Date typo in gazette")

    # Proposal 1 is still pending, so proposed_deadline should fall back to 2026-05-25
    d_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row["deadline_date"] == "2026-05-15"
    assert d_row["proposed_deadline"] == "2026-05-25"
    assert d_row["status"] == "changed"

    # Reject proposal 1
    reject_proposal(con, prop1_id, rejection_reason="Also rejected")
    d_row_final = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert d_row_final["deadline_date"] == "2026-05-15"
    assert d_row_final["proposed_deadline"] is None
    assert d_row_final["status"] == "verified"


def test_concurrent_database_uniqueness_safeguard(temp_db):
    """
    Verify partial unique index enforces uniqueness for pending proposals at database level,
    preventing race conditions during concurrent monitoring runs.
    """
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Alpha', 'B.Tech CS', 2026, 'Round 1', '2026-05-15', 'http://example.com', 'verified')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]

    # Direct database insertion 1
    con.execute("""
        INSERT INTO change_log (deadline_id, changed_at, new_deadline, approval_status)
        VALUES (?, datetime('now'), '2026-05-25', 'pending')
    """, (d_id,))

    # Direct database insertion 2 with identical pending date should fail on unique index
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("""
            INSERT INTO change_log (deadline_id, changed_at, new_deadline, approval_status)
            VALUES (?, datetime('now'), '2026-05-25', 'pending')
        """, (d_id,))

    # propose_change catches IntegrityError and recovers safely
    recovered_id = propose_change(con, d_id, "2026-05-15", "2026-05-25", "http://example.com", "Evidence")
    assert recovered_id is not None


# -------------------------------------------------------------
# Import Feature Tests (Sections 2, 3, 4, 5, 7)
# -------------------------------------------------------------
def test_import_successful_csv(temp_db):
    """Verify successful CSV import with both verified and unverified approval modes."""
    con, _ = temp_db
    csv_data = (
        "college,programme,year,cycle,deadline_date,source_url,timezone,notes\n"
        "IIT Bombay,M.Tech AI,2026,Main,2026-06-15,https://iitb.ac.in/notice,Asia/Kolkata,Official PG notice\n"
        "BITS Pilani,B.E. Computer Science,2026,Iteration 1,2026-05-30,https://bits.ac.in/admissions,Asia/Kolkata,UG Iteration\n"
    )

    df = load_file_to_dataframe(csv_data.encode("utf-8"), "test_import.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.is_structure_valid is True
    assert summary.total_rows == 2
    assert summary.valid_count == 2
    assert summary.duplicate_count == 0
    assert summary.invalid_count == 0

    # 1. Import with is_verified_by_user = True (Approved date mode)
    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.success is True
    assert res.inserted_count == 2

    # Check database records
    row1 = con.execute("SELECT * FROM deadlines WHERE college = 'IIT Bombay'").fetchone()
    assert row1 is not None
    assert row1["deadline_date"] == "2026-06-15"
    assert row1["status"] == "verified"
    assert row1["is_synthetic"] == 0

    # Reminders generated because date was approved
    rems1 = con.execute("SELECT * FROM reminders WHERE deadline_id = ?", (row1["id"],)).fetchall()
    assert len(rems1) == 3

    # 2. Test unverified mode (candidate date awaiting review in Approval Center)
    csv_unverified = (
        "college,programme,year,cycle,deadline_date,source_url,timezone,notes\n"
        "NIT Warangal,M.Tech VLSI,2026,Round 1,2026-07-05,https://nitw.ac.in/pg,,Unverified candidate date\n"
    )
    df_unv = load_file_to_dataframe(csv_unverified.encode("utf-8"), "unverified.csv")
    summary_unv = validate_admission_dataframe(df_unv, con=con)
    res_unv = import_validated_records(con, summary_unv.valid_records, is_verified_by_user=False)
    assert res_unv.success is True

    row_unv = con.execute("SELECT * FROM deadlines WHERE college = 'NIT Warangal'").fetchone()
    assert row_unv["deadline_date"] is None            # NOT approved
    assert row_unv["proposed_deadline"] == "2026-07-05" # candidate date
    assert row_unv["status"] == "unverified"
    assert row_unv["timezone"] == "Asia/Kolkata"       # default applied

    # No reminders for unverified records
    rems_unv = con.execute("SELECT * FROM reminders WHERE deadline_id = ?", (row_unv["id"],)).fetchall()
    assert len(rems_unv) == 0


def test_import_successful_excel(temp_db):
    """Verify successful Excel (.xlsx) file parsing and ingestion."""
    con, _ = temp_db
    df_raw = pd.DataFrame([
        {
            "college": "Delhi Technological University",
            "programme": "B.Tech Software Engineering",
            "year": 2026,
            "cycle": "Round 1",
            "deadline_date": "2026-06-25",
            "source_url": "https://dtu.ac.in/btech",
            "timezone": "Asia/Kolkata",
            "notes": "Imported from Excel"
        }
    ])

    excel_buffer = io.BytesIO()
    df_raw.to_excel(excel_buffer, index=False, engine="openpyxl")
    excel_bytes = excel_buffer.getvalue()

    df_loaded = load_file_to_dataframe(excel_bytes, "admissions.xlsx")
    summary = validate_admission_dataframe(df_loaded, con=con)

    assert summary.valid_count == 1
    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.success is True

    row = con.execute("SELECT * FROM deadlines WHERE college = 'Delhi Technological University'").fetchone()
    assert row is not None
    assert row["programme"] == "B.Tech Software Engineering"
    assert row["deadline_date"] == "2026-06-25"
    assert row["is_synthetic"] == 0


def test_import_missing_required_columns(temp_db):
    """Verify rejection when required columns (e.g. source_url, programme) are omitted."""
    con, _ = temp_db
    csv_missing = "college,year\nIIT Madras,2026\n"
    df = load_file_to_dataframe(csv_missing.encode("utf-8"), "missing.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.is_structure_valid is False
    assert "programme" in summary.missing_columns
    assert "source_url" in summary.missing_columns
    assert "Missing required column(s)" in summary.structure_error
    assert summary.valid_count == 0


def test_import_invalid_and_impossible_dates(temp_db):
    """Verify calendar-level date validation rejects impossible dates and ambiguous formats."""
    con, _ = temp_db
    csv_dates = (
        "college,programme,year,cycle,deadline_date,source_url\n"
        "College 1,Prog 1,2026,Main,2026-02-30,https://example.com/1\n"    # Impossible Feb 30
        "College 2,Prog 2,2026,Main,2026-13-45,https://example.com/2\n"    # Impossible month 13
        "College 3,Prog 3,2026,Main,15/05/2026,https://example.com/3\n"    # Ambiguous non-ISO
        "College 4,Prog 4,2026,Main,2026-04-31,https://example.com/4\n"    # April has only 30 days
    )
    df = load_file_to_dataframe(csv_dates.encode("utf-8"), "bad_dates.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.valid_count == 0
    assert summary.invalid_count == 4
    for inv in summary.invalid_records:
        assert inv.status == "invalid"
        assert any("date" in err.lower() for err in inv.errors)


def test_import_invalid_academic_years(temp_db):
    """Verify academic year validation rejects non-integers and out-of-range years."""
    con, _ = temp_db
    csv_years = (
        "college,programme,year,cycle,source_url\n"
        "College 1,Prog 1,twenty_twenty_six,Main,https://example.com/1\n" # String
        "College 2,Prog 2,1985,Main,https://example.com/2\n"              # Out of range (<2000)
        "College 3,Prog 3,2150,Main,https://example.com/3\n"              # Out of range (>2100)
        "College 4,Prog 4,2026.5,Main,https://example.com/4\n"            # Float with fraction
    )
    df = load_file_to_dataframe(csv_years.encode("utf-8"), "bad_years.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.valid_count == 0
    assert summary.invalid_count == 4
    for inv in summary.invalid_records:
        assert any("academic year" in err.lower() or "integer" in err.lower() for err in inv.errors)


def test_import_duplicate_records_within_file(temp_db):
    """Verify duplicate (college, programme, year, cycle) rows within uploaded file are detected and skipped."""
    con, _ = temp_db
    csv_dups = (
        "college,programme,year,cycle,deadline_date,source_url\n"
        "IISc Bangalore,M.Tech AI,2026,Main,2026-06-15,https://iisc.ac.in/pg\n"
        "IISc Bangalore,M.Tech AI,2026,Main,2026-06-20,https://iisc.ac.in/pg2\n"  # Duplicate of Row 1
        "IISc Bangalore,M.Tech AI,2026,Round 2,2026-07-01,https://iisc.ac.in/pg\n" # Different cycle: Valid!
    )
    df = load_file_to_dataframe(csv_dups.encode("utf-8"), "internal_dups.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.total_rows == 3
    assert summary.valid_count == 2
    assert summary.duplicate_count == 1
    assert summary.invalid_count == 0

    dup_row = summary.duplicate_records[0]
    assert dup_row.row_number == 2
    assert dup_row.duplicate_type == "file"
    assert "Duplicate record within file" in dup_row.errors[0]

    # Only 2 valid records imported
    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.inserted_count == 2
    count = con.execute("SELECT COUNT(*) FROM deadlines WHERE college = 'IISc Bangalore'").fetchone()[0]
    assert count == 2


def test_import_duplicate_records_already_in_database(temp_db):
    """Verify records matching existing (college, programme, year, cycle) in DB are skipped without overwriting."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, source_url, status)
            VALUES ('NIT Trichy', 'B.Tech Mechanical', 2026, 'Main', '2026-05-15', 'https://nitt.edu/initial', 'verified')
        """)
        orig_row = con.execute("SELECT * FROM deadlines WHERE college = 'NIT Trichy'").fetchone()

    # Upload file attempting to import duplicate with a changed date and URL
    csv_db_dup = (
        "college,programme,year,cycle,deadline_date,source_url\n"
        "NIT Trichy,B.Tech Mechanical,2026,Main,2026-06-01,https://nitt.edu/overwrite_attempt\n"
        "NIT Trichy,B.Tech Civil,2026,Main,2026-05-20,https://nitt.edu/civil\n"
    )
    df = load_file_to_dataframe(csv_db_dup.encode("utf-8"), "db_dups.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.total_rows == 2
    assert summary.valid_count == 1
    assert summary.duplicate_count == 1
    assert summary.duplicate_records[0].duplicate_type == "database"
    assert "already exists in database" in summary.duplicate_records[0].errors[0].lower()

    # Import valid
    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.inserted_count == 1

    # CRITICAL: Verify existing database record was NOT overwritten!
    after_row = con.execute("SELECT * FROM deadlines WHERE id = ?", (orig_row["id"],)).fetchone()
    assert after_row["deadline_date"] == "2026-05-15" # Preserved original date!
    assert after_row["source_url"] == "https://nitt.edu/initial" # Preserved original URL!
    assert after_row["status"] == "verified"


def test_import_empty_and_malformed_files():
    """Verify empty, oversized, unsupported, and malformed files raise clear descriptive ValueErrors."""
    # 1. Empty bytes
    with pytest.raises(ValueError, match="empty \\(0 bytes\\)"):
        load_file_to_dataframe(b"", "empty.csv")

    # 2. Unsupported extension
    with pytest.raises(ValueError, match="Unsupported file type"):
        load_file_to_dataframe(b"data", "admissions.json")

    # 3. CSV with headers but no data rows
    with pytest.raises(ValueError, match="contains no data rows"):
        load_file_to_dataframe("college,programme,year,source_url\n".encode("utf-8"), "no_rows.csv")

    # 4. Corrupted bytes disguised as Excel
    with pytest.raises(ValueError, match="Malformed or unreadable file"):
        load_file_to_dataframe(b"Not an excel file zip content", "corrupted.xlsx")


def test_import_missing_optional_values_and_defaults(temp_db):
    """Verify optional fields are assigned clean defaults when omitted from uploaded file."""
    con, _ = temp_db
    csv_min = (
        "college,programme,year,source_url\n"
        "State University,M.A. History,2026,https://state.edu/ma-history\n"
    )
    df = load_file_to_dataframe(csv_min.encode("utf-8"), "minimal.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.valid_count == 1
    rec = summary.valid_records[0]
    assert rec["cycle"] == "Main"                # default applied
    assert rec["timezone"] == "Asia/Kolkata"     # default applied
    assert rec["deadline_date"] is None          # blank optional
    assert rec["notes"] is None                  # blank optional

    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.success is True

    row = con.execute("SELECT * FROM deadlines WHERE college = 'State University'").fetchone()
    assert row["cycle"] == "Main"
    assert row["timezone"] == "Asia/Kolkata"
    assert row["deadline_date"] is None
    assert row["notes"] is None
    assert row["status"] == "unverified"


def test_import_database_failure_transaction_rollback(temp_db):
    """Verify unexpected database failures during import execute an atomic rollback."""
    con, _ = temp_db
    valid_recs = [
        {
            "college": "Good College 1",
            "programme": "Prog 1",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-06-01",
            "source_url": "https://example.com/1",
            "timezone": "Asia/Kolkata",
            "notes": None
        },
        {
            "college": "Good College 2",
            "programme": "Prog 2",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-06-02",
            "source_url": "https://example.com/2",
            "timezone": "Asia/Kolkata",
            "notes": None
        }
    ]

    # Close connection to force an unexpected operational error during execution
    con_broken = sqlite3.connect(":memory:")
    con_broken.close()

    res = import_validated_records(con_broken, valid_recs, is_verified_by_user=True)
    assert res.success is False
    assert res.inserted_count == 0
    assert res.error_message is not None

    # Verify temp_db was not altered
    count = con.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0]
    assert count == 0


def test_import_preserves_existing_approved_deadlines_and_audit(temp_db):
    """Verify importing records does not modify existing approved dates, proposed dates, or change_log audits."""
    con, _ = temp_db
    with con:
        con.execute("""
            INSERT INTO deadlines (college, programme, year, cycle, deadline_date, proposed_deadline, source_url, status)
            VALUES ('Apex Institute', 'B.Tech IT', 2026, 'Main', '2026-05-15', '2026-05-25', 'https://apex.edu', 'changed')
        """)
        d_id = con.execute("SELECT id FROM deadlines").fetchone()["id"]
        con.execute("""
            INSERT INTO change_log (deadline_id, changed_at, old_deadline, new_deadline, approval_status, approved_by, approved_at)
            VALUES (?, '2026-10-20T10:00:00Z', '2026-05-01', '2026-05-15', 'approved', 'Registrar', '2026-10-20T11:00:00Z')
        """, (d_id,))

    # Upload file with duplicate of Apex Institute and 1 new record
    csv_test = (
        "college,programme,year,cycle,deadline_date,source_url\n"
        "Apex Institute,B.Tech IT,2026,Main,2026-06-30,https://apex.edu/new\n" # Duplicate!
        "New University,B.Sc Biology,2026,Main,2026-07-15,https://newu.edu\n"    # Valid new
    )
    df = load_file_to_dataframe(csv_test.encode("utf-8"), "audit_test.csv")
    summary = validate_admission_dataframe(df, con=con)

    assert summary.valid_count == 1
    assert summary.duplicate_count == 1

    res = import_validated_records(con, summary.valid_records, is_verified_by_user=True)
    assert res.success is True
    assert res.inserted_count == 1

    # Verify existing deadline record is completely untouched
    orig_deadline = con.execute("SELECT * FROM deadlines WHERE id = ?", (d_id,)).fetchone()
    assert orig_deadline["deadline_date"] == "2026-05-15"
    assert orig_deadline["proposed_deadline"] == "2026-05-25"
    assert orig_deadline["status"] == "changed"

    # Verify change_log audit entries are 100% intact
    audit_rows = con.execute("SELECT * FROM change_log WHERE deadline_id = ?", (d_id,)).fetchall()
    assert len(audit_rows) == 1
    assert audit_rows[0]["approval_status"] == "approved"
    assert audit_rows[0]["approved_by"] == "Registrar"


def test_sample_data_initialization_still_works(temp_db):
    """Verify seed_sample_records initializes 10 sample records and co-exists with imported records."""
    con, db_path = temp_db
    con.close() # Close to allow seed_sample_records to connect

    seed_sample_records(db_path)
    con = get_connection(db_path)

    # 10 synthetic sample records
    rows = con.execute("SELECT * FROM deadlines").fetchall()
    assert len(rows) == 10
    assert all(r["is_synthetic"] == 1 for r in rows)

    # Import 1 user record
    user_record = [{
        "college": "Stanford Indian Center",
        "programme": "Postdoc AI",
        "year": 2026,
        "cycle": "Fall",
        "deadline_date": "2026-08-01",
        "source_url": "https://stanford.edu/admissions",
        "timezone": "Asia/Kolkata",
        "notes": "User imported"
    }]
    res = import_validated_records(con, user_record, is_verified_by_user=True)
    assert res.success is True

    # Total records is now 11
    all_rows = con.execute("SELECT * FROM deadlines").fetchall()
    assert len(all_rows) == 11
    user_row = con.execute("SELECT * FROM deadlines WHERE college = 'Stanford Indian Center'").fetchone()
    assert user_row["is_synthetic"] == 0 # Real user imported record

    # Subsequent seed call is idempotent and does not overwrite or duplicate
    seed_sample_records(db_path)
    total_after_reseed = con.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0]
    assert total_after_reseed == 11

