"""
reminders.py - Calendar reminder generation, stable UID management, and exports.

Implements:
- Section 2.2 (Issue 2): Proper newline formatting in event descriptions.
- Section 2.5 (Issue 5): Stable reminder UIDs that persist across regenerations.
- Section 2.6 (Issue 11): Timezone-aware date calculations with ZoneInfo.
- Section 8.4: CSV export separating candidate and approved dates.
- RFC 5545 iCalendar (.ics) export with stable UIDs and alarms.
"""

import os
from uuid import uuid4
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any
import sqlite3
import pandas as pd
from icalendar import Calendar, Event, Alarm
from workflow import to_date

REMINDER_INTERVALS = [30, 7, 1]


def get_or_create_uid(con: sqlite3.Connection, deadline_id: int, days_before: int) -> str:
    """
    Fix 2.5 (Issue 5): Generates UID once, then reuses it.
    Prevents duplicate calendar events across multiple runs.
    """
    row = con.execute(
        "SELECT calendar_uid FROM reminders WHERE deadline_id=? AND reminder_type=?",
        (deadline_id, f"{days_before}_day")
    ).fetchone()

    if row and row["calendar_uid"]:
        return row["calendar_uid"]

    return f"{uuid4()}@admission-tracker"


def generate_reminders_for_deadline(
    con: sqlite3.Connection,
    deadline_id: int,
    reference_date: Optional[date] = None
) -> List[Dict[str, Any]]:
    """
    Generate or update reminders (30, 7, and 1 day before) for approved deadline.
    Fix 11: Uses programme's timezone for comparison.
    """
    d_row = con.execute("""
        SELECT id, college, programme, year, deadline_date, timezone
        FROM deadlines
        WHERE id = ?
    """, (deadline_id,)).fetchone()

    if not d_row or not d_row["deadline_date"]:
        return []

    target_date = to_date(d_row["deadline_date"])
    tz_str = d_row["timezone"] or "Asia/Kolkata"
    
    try:
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")

    today = reference_date or datetime.now(tz).date()
    utc_now = datetime.now(timezone.utc).isoformat()
    generated = []

    with con:
        for days in REMINDER_INTERVALS:
            reminder_dt = target_date - timedelta(days=days)
            rem_type = f"{days}_day"
            
            # Fetch existing or create stable UID (Fix 2.5)
            stable_uid = get_or_create_uid(con, deadline_id, days)

            con.execute("""
                INSERT INTO reminders (
                    deadline_id, reminder_type, reminder_date,
                    calendar_uid, needs_update, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(deadline_id, reminder_type) DO UPDATE SET
                    reminder_date = excluded.reminder_date,
                    calendar_uid = excluded.calendar_uid,
                    needs_update = 0,
                    updated_at = excluded.updated_at
            """, (
                deadline_id, rem_type, reminder_dt.isoformat(),
                stable_uid, utc_now, utc_now
            ))

            generated.append({
                "deadline_id": deadline_id,
                "reminder_type": rem_type,
                "reminder_date": reminder_dt.isoformat(),
                "calendar_uid": stable_uid,
                "is_upcoming": reminder_dt >= today
            })

    return generated


def sync_all_active_reminders(con: sqlite3.Connection) -> int:
    """Generate or update reminders for all approved deadlines."""
    rows = con.execute("SELECT id FROM deadlines WHERE deadline_date IS NOT NULL").fetchall()
    count = 0
    for r in rows:
        generate_reminders_for_deadline(con, r["id"])
        count += 1
    return count


def build_icalendar(con: sqlite3.Connection, deadline_id: Optional[int] = None) -> bytes:
    """
    Generate an iCalendar (.ics) byte payload for deadlines and reminders.
    Fix 2.2: Proper newline in description.
    Fix 2.5: Reuses stable UIDs.
    """
    cal = Calendar()
    cal.add("prodid", "-//Admission Deadline Tracker//admission-tracker.local//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    query = """
        SELECT d.id, d.college, d.programme, d.year, d.cycle, d.deadline_date,
               d.source_url, r.reminder_type, r.reminder_date, r.calendar_uid
        FROM deadlines d
        JOIN reminders r ON d.id = r.deadline_id
        WHERE d.deadline_date IS NOT NULL
    """
    params = []
    if deadline_id:
        query += " AND d.id = ?"
        params.append(deadline_id)

    rows = con.execute(query, params).fetchall()

    for row in rows:
        event = Event()
        event.add("uid", row["calendar_uid"])
        
        days_num = row["reminder_type"].split("_")[0]
        event.add("summary", f"[Deadline Reminder - {days_num}d] {row['college']} - {row['programme']}")
        
        rem_date = to_date(row["reminder_date"])
        event.add("dtstart", rem_date)
        event.add("dtend", rem_date + timedelta(days=1))
        
        # Fix 2.2: Description formatted with real newlines
        description = (
            f"College: {row['college']}\n"
            f"Programme: {row['programme']} ({row['year']}, {row['cycle']})\n"
            f"Final Deadline: {row['deadline_date']}\n"
            f"Notice Source: {row['source_url']}\n"
            f"Verify the official admission notice."
        )
        event.add("description", description)
        
        # Add alarm notification (9:00 AM on reminder date)
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"Admission Deadline approaching for {row['college']}")
        alarm.add("trigger", timedelta(hours=9))
        event.add_component(alarm)

        cal.add_component(event)

    return cal.to_ical()


def export_csv_dataframe(con: sqlite3.Connection) -> pd.DataFrame:
    """
    Step 8.4: Export CSV with candidate and approved dates in separate columns.
    """
    query = """
        SELECT 
            college,
            programme,
            year,
            cycle,
            deadline_date AS approved_deadline,
            proposed_deadline AS candidate_deadline,
            status,
            last_checked,
            source_url,
            notes,
            is_synthetic
        FROM deadlines
        ORDER BY year DESC, college ASC, programme ASC
    """
    df = pd.read_sql_query(query, con)
    return df
