"""
monitor.py - Automated monitoring engine with rate-limiting and resilient failure handling.

Implements:
- Section 2.6 (Issue 8): Record check_history row on EVERY attempt (success or failure).
- Section 2.6 (Issue 10): 5-second delay between requests and skip host after 2 consecutive failures.
- Section 5 & Checkpoint 5: Failures record could_not_verify, preserve previous approved date,
  preserve reminders, and log friendly error descriptions without raw stack traces.
"""

import time
import urllib.parse
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import sqlite3

from database import get_connection
from extractor import fetch_notice, find_deadline_candidates, hash_notice_section, log_check_history
from workflow import compare_deadlines, propose_change, to_date
from reminders import generate_reminders_for_deadline


def extract_host(url: str) -> str:
    """Extract domain or host from URL for per-domain rate limiting."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc:
            return parsed.netloc.lower()
        return "local_fixture"
    except Exception:
        return "unknown_host"


def check_deadline_record(con: sqlite3.Connection, record: sqlite3.Row) -> Dict[str, Any]:
    """
    Execute a single check cycle for a deadline record.
    Preserves audit trail and never mutates approved deadline_date without human approval.
    """
    deadline_id = record["id"]
    college = record["college"]
    programme = record["programme"]
    year = record["year"]
    source_url = record["source_url"]
    stored_deadline = record["deadline_date"]
    utc_now = datetime.now(timezone.utc).isoformat()

    # 1. Fetch notice content
    text, http_status, error_msg = fetch_notice(source_url)

    # 2. Failure Handling (Checkpoint 5)
    if error_msg or text is None:
        friendly_error = error_msg or f"Failed to retrieve portal content (HTTP {http_status})"
        
        # Log to check_history (Issue 8)
        log_check_history(
            con=con,
            deadline_id=deadline_id,
            status="could_not_verify",
            http_status=http_status,
            evidence=friendly_error,
            content_hash=record["content_hash"],
            source_url=source_url
        )

        # Update status to could_not_verify; keep previous deadline_date untouched
        with con:
            con.execute("""
                UPDATE deadlines
                SET status = 'could_not_verify',
                    last_checked = ?,
                    updated_at = ?
                WHERE id = ?
            """, (utc_now, utc_now, deadline_id))

        return {
            "deadline_id": deadline_id,
            "status": "could_not_verify",
            "http_status": http_status,
            "error": friendly_error,
            "deadline_date": stored_deadline
        }

    # 3. Extract candidate deadlines matching programme & year specificity (TC3)
    candidates = find_deadline_candidates(text, programme=programme, year=year)

    if not candidates:
        friendly_msg = "Notice retrieved successfully, but no matching deadline keywords found for this programme/year."
        log_check_history(
            con=con,
            deadline_id=deadline_id,
            status="could_not_verify",
            http_status=http_status,
            evidence=friendly_msg,
            content_hash=record["content_hash"],
            source_url=source_url
        )
        with con:
            con.execute("""
                UPDATE deadlines
                SET status = 'could_not_verify',
                    last_checked = ?,
                    updated_at = ?
                WHERE id = ?
            """, (utc_now, utc_now, deadline_id))

        return {
            "deadline_id": deadline_id,
            "status": "could_not_verify",
            "http_status": http_status,
            "error": friendly_msg,
            "deadline_date": stored_deadline
        }

    # Select primary candidate
    primary = candidates[0]
    candidate_date = primary["iso_date"]
    evidence = primary["evidence"]
    notice_hash = primary["section_hash"]

    # 4. Compare observed date vs stored approved date
    comp_status, old_d, new_d = compare_deadlines(stored_deadline, candidate_date)

    if comp_status == "unchanged":
        # TC1: Date confirmed unchanged
        log_check_history(
            con=con,
            deadline_id=deadline_id,
            status="verified",
            http_status=http_status,
            evidence=evidence,
            content_hash=notice_hash,
            source_url=source_url
        )
        with con:
            con.execute("""
                UPDATE deadlines
                SET status = 'verified',
                    content_hash = ?,
                    last_checked = ?,
                    updated_at = ?
                WHERE id = ?
            """, (notice_hash, utc_now, utc_now, deadline_id))

        # Ensure reminders are initialized if missing
        generate_reminders_for_deadline(con, deadline_id)

        return {
            "deadline_id": deadline_id,
            "status": "verified",
            "http_status": http_status,
            "deadline_date": stored_deadline,
            "evidence": evidence
        }

    elif comp_status == "changed":
        # TC2: New date observed! Propose change, NEVER update deadline_date directly!
        proposal_id = propose_change(
            con=con,
            deadline_id=deadline_id,
            old_date=stored_deadline,
            new_date=candidate_date,
            source_url=source_url,
            evidence=evidence,
            reason=f"Detected updated deadline for {programme} in official notice"
        )

        log_check_history(
            con=con,
            deadline_id=deadline_id,
            status="changed",
            http_status=http_status,
            evidence=evidence,
            content_hash=notice_hash,
            source_url=source_url
        )

        return {
            "deadline_id": deadline_id,
            "status": "changed",
            "proposal_id": proposal_id,
            "old_deadline": stored_deadline,
            "proposed_deadline": candidate_date,
            "evidence": evidence
        }

    else:
        # Fallback could_not_verify
        log_check_history(
            con=con,
            deadline_id=deadline_id,
            status="could_not_verify",
            http_status=http_status,
            evidence=evidence,
            content_hash=notice_hash,
            source_url=source_url
        )
        return {
            "deadline_id": deadline_id,
            "status": "could_not_verify",
            "http_status": http_status,
            "error": "Could not verify candidate deadline"
        }


def check_single_deadline_by_id(con: sqlite3.Connection, deadline_id: int) -> Dict[str, Any]:
    """Helper to check a specific deadline by ID."""
    row = con.execute("SELECT * FROM deadlines WHERE id = ?", (deadline_id,)).fetchone()
    if not row:
        raise ValueError(f"Deadline ID {deadline_id} not found.")
    return check_deadline_record(con, row)


def run_monitoring_cycle(
    db_path: Optional[str] = None,
    delay_seconds: float = 5.0,
    max_host_failures: int = 2
) -> List[Dict[str, Any]]:
    """
    Main monitoring batch loop with polite rate limiting and host failure skipping.
    Fix 10: time.sleep(5) between requests and skip host after 2 failures.
    """
    con = get_connection(db_path)
    rows = con.execute("SELECT * FROM deadlines ORDER BY id ASC").fetchall()

    results = []
    host_failure_counts: Dict[str, int] = {}

    for record in rows:
        host = extract_host(record["source_url"])

        # Check per-host failure limit (Issue 10)
        if host_failure_counts.get(host, 0) >= max_host_failures:
            utc_now = datetime.now(timezone.utc).isoformat()
            skip_msg = f"Skipping check for host '{host}': exceeded {max_host_failures} consecutive failures."
            log_check_history(
                con=con,
                deadline_id=record["id"],
                status="could_not_verify",
                http_status=0,
                evidence=skip_msg,
                content_hash=record["content_hash"],
                source_url=record["source_url"]
            )
            results.append({
                "deadline_id": record["id"],
                "status": "skipped",
                "reason": skip_msg
            })
            continue

        # Execute check
        res = check_deadline_record(con, record)
        results.append(res)

        # Track failure count per host
        if res.get("status") in ["could_not_verify", "error"] and res.get("http_status", 200) != 200:
            host_failure_counts[host] = host_failure_counts.get(host, 0) + 1
        elif res.get("status") in ["verified", "changed"]:
            host_failure_counts[host] = 0

        # Rate-limiting delay between requests (Issue 10)
        if delay_seconds > 0 and record != rows[-1]:
            time.sleep(delay_seconds)

    con.close()
    return results


if __name__ == "__main__":
    print("Running Admission Deadline Tracker monitoring cycle...")
    outcomes = run_monitoring_cycle(delay_seconds=1.0)
    print(f"Completed checks for {len(outcomes)} records.")
    for o in outcomes:
        print(f" - ID {o.get('deadline_id')}: {o.get('status')} (date: {o.get('deadline_date') or o.get('proposed_deadline')})")
