"""
workflow.py - Core deadline comparison, change proposal, and approval workflow.

Implements:
- Section 2.3 (Issue 3): Compare real date objects with to_date(), never raw strings.
- Section 2.4 (Issue 4): propose_change() creates a pending change_log row, never directly updates deadline_date.
- Section 2.6 (Issue 6): Atomic approval transaction setting approved_by/approved_at, and rejection with rejection_reason.
"""

from datetime import date, datetime, timezone
from typing import Optional, Dict, Any, Tuple, List
import sqlite3

def to_date(value: Any) -> Optional[date]:
    """
    Fix 2.3 (Issue 3): Safely convert string, date, or None to a datetime.date object.
    Supports ISO 'YYYY-MM-DD' strings and datetime/date objects.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    val_str = str(value).strip()
    if not val_str:
        return None
    try:
        return date.fromisoformat(val_str)
    except ValueError:
        # Fallback parsing if string contains timestamp or other delimiter
        try:
            return datetime.fromisoformat(val_str).date()
        except Exception:
            try:
                from extractor import parse_date_to_iso
                iso_str = parse_date_to_iso(val_str)
                if iso_str:
                    return date.fromisoformat(iso_str)
            except Exception:
                pass
            return None


def compare_deadlines(
    stored_deadline: Any,
    observed_deadline: Any
) -> Tuple[str, Optional[date], Optional[date]]:
    """
    Fix 2.3 & Day 3 Rule: Pure function that returns status and never writes to DB.
    
    Status can be:
    - 'unchanged': stored and observed dates exist and are strictly equal.
    - 'changed': observed date exists and differs from stored date.
    - 'could_not_verify': observed date is None or invalid.
    
    Returns: (status, old_date, new_date)
    """
    old_d = to_date(stored_deadline)
    new_d = to_date(observed_deadline)

    if new_d is None:
        return "could_not_verify", old_d, None

    if old_d is None:
        return "changed", None, new_d

    if old_d == new_d:
        return "unchanged", old_d, new_d
    else:
        return "changed", old_d, new_d


def get_pending_proposal(
    con: sqlite3.Connection,
    deadline_id: int,
    candidate_date: Optional[Any] = None
) -> Optional[sqlite3.Row]:
    """
    Retrieve an active pending proposal for a deadline.
    If candidate_date is provided, checks for an equivalent pending proposal matching that date.
    Otherwise returns the most recent pending proposal for the deadline.
    """
    if candidate_date is not None:
        c_date = to_date(candidate_date)
        c_iso = c_date.isoformat() if c_date else str(candidate_date).strip()
        return con.execute("""
            SELECT * FROM change_log
            WHERE deadline_id = ? AND approval_status = 'pending' AND new_deadline = ?
            ORDER BY changed_at DESC LIMIT 1
        """, (deadline_id, c_iso)).fetchone()
    else:
        return con.execute("""
            SELECT * FROM change_log
            WHERE deadline_id = ? AND approval_status = 'pending'
            ORDER BY changed_at DESC LIMIT 1
        """, (deadline_id,)).fetchone()


def get_pending_proposals(
    con: sqlite3.Connection,
    deadline_id: Optional[int] = None
) -> List[sqlite3.Row]:
    """Retrieve all active pending proposals, optionally filtered by deadline_id."""
    if deadline_id is not None:
        return con.execute("""
            SELECT * FROM change_log
            WHERE deadline_id = ? AND approval_status = 'pending'
            ORDER BY changed_at DESC
        """, (deadline_id,)).fetchall()
    return con.execute("""
        SELECT * FROM change_log
        WHERE approval_status = 'pending'
        ORDER BY changed_at DESC
    """).fetchall()


def propose_change(
    con: sqlite3.Connection,
    deadline_id: int,
    old_date: Optional[str],
    new_date: str,
    source_url: str,
    evidence: str,
    reason: str = "Automated portal check detected modified deadline"
) -> int:
    """
    Writes a PENDING proposal to change_log if an equivalent pending proposal does not already exist.
    Never touches deadlines.deadline_date! Only sets proposed_deadline and status='changed'.

    Prevents duplicate pending proposals for the same candidate date while preserving audit history.
    Handles conflicting pending candidate dates safely without silently replacing existing proposals.
    """
    utc_now = datetime.now(timezone.utc).isoformat()
    old_iso = to_date(old_date).isoformat() if to_date(old_date) else None
    new_iso = to_date(new_date).isoformat() if to_date(new_date) else str(new_date).strip()

    # 1. Prevent duplicate pending proposals: check if equivalent proposal is already pending
    existing_pending = get_pending_proposal(con, deadline_id, candidate_date=new_iso)
    if existing_pending is not None:
        # Equivalent proposal already pending. Do not insert another row into change_log.
        # Refresh last_checked and updated_at on deadlines table to record check
        with con:
            con.execute("""
                UPDATE deadlines
                SET proposed_deadline = ?,
                    status = 'changed',
                    last_checked = ?,
                    updated_at = ?
                WHERE id = ?
            """, (new_iso, utc_now, utc_now, deadline_id))
        return existing_pending["id"]

    # 2. Check if other pending proposals exist for different candidate dates
    other_pending = con.execute("""
        SELECT id, new_deadline FROM change_log
        WHERE deadline_id = ? AND approval_status = 'pending' AND new_deadline != ?
        ORDER BY changed_at DESC
    """, (deadline_id, new_iso)).fetchall()

    proposal_reason = reason
    if other_pending:
        conflict_details = ", ".join(f"#{r['id']} ({r['new_deadline']})" for r in other_pending)
        if "conflict" not in proposal_reason.lower():
            proposal_reason = f"{proposal_reason} (Note: concurrent pending proposal {conflict_details} exists)"

    with con:
        # 3. Insert proposal into change_log with uniqueness safeguard
        try:
            cur = con.execute("""
                INSERT INTO change_log (
                    deadline_id, changed_at, old_deadline, new_deadline,
                    reason, source_url, evidence, approval_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (deadline_id, utc_now, old_iso, new_iso, proposal_reason, source_url, evidence))
            proposal_id = cur.lastrowid
        except sqlite3.IntegrityError:
            # Concurrent monitoring attempt inserted an equivalent pending proposal
            race_pending = get_pending_proposal(con, deadline_id, candidate_date=new_iso)
            if race_pending is not None:
                proposal_id = race_pending["id"]
            else:
                raise

        # 4. Update deadline candidate date and status only (NOT deadline_date)
        con.execute("""
            UPDATE deadlines
            SET proposed_deadline = ?,
                status = 'changed',
                last_checked = ?,
                updated_at = ?
            WHERE id = ?
        """, (new_iso, utc_now, utc_now, deadline_id))

    return proposal_id


def approve_proposal(
    con: sqlite3.Connection,
    proposal_id: int,
    approved_by: str = "Admin Reviewer"
) -> Dict[str, Any]:
    """
    Day 4 / Step 7.2: Atomic approval transaction.
    1. Verify proposal is still pending.
    2. Update deadline_date to new_deadline.
    3. Update or clear proposed_deadline and set status accordingly.
    4. Record approved_by and approved_at in change_log.
    5. Mark reminders as needs_update.
    """
    utc_now = datetime.now(timezone.utc).isoformat()
    
    with con:
        # Check proposal status
        row = con.execute("""
            SELECT id, deadline_id, old_deadline, new_deadline, approval_status
            FROM change_log
            WHERE id = ?
        """, (proposal_id,)).fetchone()

        if not row:
            raise ValueError(f"Proposal with ID {proposal_id} not found.")

        if row["approval_status"] != "pending":
            raise ValueError(f"Proposal {proposal_id} is already '{row['approval_status']}'. Cannot approve again.")

        deadline_id = row["deadline_id"]
        new_deadline = row["new_deadline"]

        # Check if other pending proposals remain for this deadline
        remaining = con.execute("""
            SELECT new_deadline FROM change_log
            WHERE deadline_id = ? AND approval_status = 'pending' AND id != ?
            ORDER BY changed_at DESC
        """, (deadline_id, proposal_id)).fetchone()

        # 1. Update deadlines table
        if remaining:
            con.execute("""
                UPDATE deadlines
                SET deadline_date = ?,
                    proposed_deadline = ?,
                    status = 'changed',
                    updated_at = ?
                WHERE id = ?
            """, (new_deadline, remaining["new_deadline"], utc_now, deadline_id))
        else:
            con.execute("""
                UPDATE deadlines
                SET deadline_date = ?,
                    proposed_deadline = NULL,
                    status = 'verified',
                    updated_at = ?
                WHERE id = ?
            """, (new_deadline, utc_now, deadline_id))

        # 2. Update change_log with audit fields (Issue 6)
        con.execute("""
            UPDATE change_log
            SET approval_status = 'approved',
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
        """, (approved_by, utc_now, proposal_id))

        # 3. Mark existing reminders as needing update
        con.execute("""
            UPDATE reminders
            SET needs_update = 1,
                updated_at = ?
            WHERE deadline_id = ?
        """, (utc_now, deadline_id))

    return {
        "success": True,
        "proposal_id": proposal_id,
        "deadline_id": deadline_id,
        "approved_deadline": new_deadline,
        "approved_by": approved_by,
        "approved_at": utc_now
    }


def reject_proposal(
    con: sqlite3.Connection,
    proposal_id: int,
    rejection_reason: str = "Rejected by reviewer after verification"
) -> Dict[str, Any]:
    """
    Day 4 / Step 7.3: Reject candidate proposal.
    Records rejected_at and rejection_reason. Keeps original deadline_date and reminders untouched.
    """
    utc_now = datetime.now(timezone.utc).isoformat()

    with con:
        row = con.execute("""
            SELECT id, deadline_id, approval_status
            FROM change_log
            WHERE id = ?
        """, (proposal_id,)).fetchone()

        if not row:
            raise ValueError(f"Proposal with ID {proposal_id} not found.")

        if row["approval_status"] != "pending":
            raise ValueError(f"Proposal {proposal_id} is already '{row['approval_status']}'. Cannot reject.")

        deadline_id = row["deadline_id"]

        # 1. Update change_log audit fields
        con.execute("""
            UPDATE change_log
            SET approval_status = 'rejected',
                rejected_at = ?,
                rejection_reason = ?
            WHERE id = ?
        """, (utc_now, rejection_reason, proposal_id))

        # 2. Check if other pending proposals remain for this deadline
        remaining = con.execute("""
            SELECT new_deadline FROM change_log
            WHERE deadline_id = ? AND approval_status = 'pending' AND id != ?
            ORDER BY changed_at DESC
        """, (deadline_id, proposal_id)).fetchone()

        if remaining:
            con.execute("""
                UPDATE deadlines
                SET proposed_deadline = ?,
                    status = 'changed',
                    updated_at = ?
                WHERE id = ?
            """, (remaining["new_deadline"], utc_now, deadline_id))
        else:
            con.execute("""
                UPDATE deadlines
                SET proposed_deadline = NULL,
                    status = 'verified',
                    updated_at = ?
                WHERE id = ?
            """, (utc_now, deadline_id))

    return {
        "success": True,
        "proposal_id": proposal_id,
        "deadline_id": deadline_id,
        "rejection_reason": rejection_reason,
        "rejected_at": utc_now
    }
