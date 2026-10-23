"""
database.py - Database schema and persistence for Admission Deadline Tracker.

Includes:
- Deadlines table with separation of approved vs candidate dates.
- Change log table with full approval and audit fields (approved_by, approved_at, rejected_at, rejection_reason).
- Check history table logging all checks (success, change, could_not_verify, error).
- Reminders table with stable calendar UIDs.
"""

import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.db")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Get SQLite database connection with Row factory enabled."""
    path = db_path or DEFAULT_DB_PATH
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database tables with complete schema and audit fields."""
    con = get_connection(db_path)
    with con:
        # 1. Deadlines table
        con.execute("""
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                college TEXT NOT NULL,
                programme TEXT NOT NULL,
                year INTEGER NOT NULL,
                cycle TEXT NOT NULL DEFAULT 'Main',
                deadline_date TEXT,               -- Approved official date (ISO YYYY-MM-DD)
                proposed_deadline TEXT,           -- Candidate date awaiting approval (Issue 6)
                source_url TEXT NOT NULL,
                content_hash TEXT,                -- Hash of extracted notice passage (Issue 7)
                last_checked TEXT,                -- UTC ISO timestamp
                status TEXT NOT NULL DEFAULT 'unverified', -- 'verified', 'changed', 'could_not_verify', 'unverified'
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata', -- ZoneInfo string (Issue 11)
                notes TEXT,
                is_synthetic INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(college, programme, year, cycle)
            );
        """)

        # 2. Change Log table with complete audit columns (Issue 6)
        con.execute("""
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deadline_id INTEGER NOT NULL REFERENCES deadlines(id) ON DELETE CASCADE,
                changed_at TEXT NOT NULL,          -- UTC ISO timestamp
                old_deadline TEXT,
                new_deadline TEXT NOT NULL,
                reason TEXT,
                source_url TEXT,
                evidence TEXT,
                approval_status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
                approved_by TEXT,                 -- Step 7 audit field
                approved_at TEXT,                 -- Step 7 audit field
                rejected_at TEXT,                 -- Step 7 audit field
                rejection_reason TEXT             -- Step 7 audit field
            );
        """)

        # 3. Check History table logging every attempt (Issue 8)
        con.execute("""
            CREATE TABLE IF NOT EXISTS check_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deadline_id INTEGER NOT NULL REFERENCES deadlines(id) ON DELETE CASCADE,
                checked_at TEXT NOT NULL,          -- UTC ISO timestamp
                status TEXT NOT NULL,              -- 'verified', 'changed', 'could_not_verify', 'error'
                http_status INTEGER,               -- e.g. 200, 403, 404
                evidence TEXT,                     -- Matched passage or error message
                content_hash TEXT,
                source_url TEXT
            );
        """)

        # 4. Reminders table with stable UIDs (Issue 5)
        con.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deadline_id INTEGER NOT NULL REFERENCES deadlines(id) ON DELETE CASCADE,
                reminder_type TEXT NOT NULL,       -- '30_day', '7_day', '1_day'
                reminder_date TEXT NOT NULL,       -- ISO YYYY-MM-DD
                calendar_uid TEXT NOT NULL,        -- Stable UID (never regenerated)
                needs_update INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(deadline_id, reminder_type)
            );
        """)

        # 5. Clean up duplicate pending proposals if any exist, and create partial unique index
        # Ensures concurrency safety and uniqueness for equivalent pending proposals
        con.execute("""
            DELETE FROM change_log
            WHERE approval_status = 'pending'
              AND id NOT IN (
                  SELECT MIN(id)
                  FROM change_log
                  WHERE approval_status = 'pending'
                  GROUP BY deadline_id, new_deadline
              );
        """)
        con.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_change_log_pending_unique
            ON change_log (deadline_id, new_deadline)
            WHERE approval_status = 'pending';
        """)
    con.close()


def seed_sample_records(db_path: Optional[str] = None) -> None:
    """
    Seed initial sample records including synthetic College A, College B, and real portals.
    All synthetic records are clearly labeled as synthetic per guide rules.
    """
    con = get_connection(db_path)
    fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

    samples = [
        # Sample 1: NIT Alpha (B.Tech - 2026)
        {
            "college": "National Institute of Technology (NIT) Alpha",
            "programme": "B.Tech Computer Science",
            "year": 2026,
            "cycle": "Round 1",
            "deadline_date": "2026-05-15",
            "proposed_deadline": None,
            "source_url": os.path.join(fixtures_dir, "college_a_initial.html"),
            "content_hash": "c8a3f81e69b2d39281a8b093f1f33342",
            "last_checked": "2026-10-20T10:00:00Z",
            "status": "verified",
            "timezone": "Asia/Kolkata",
            "notes": "Undergraduate computer science admissions - Round 1",
            "is_synthetic": 1
        },
        # Sample 2: Apex Technical University (B.Tech Mechanical)
        {
            "college": "Apex Technical University",
            "programme": "B.Tech Mechanical Engineering",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-06-10",
            "proposed_deadline": None,
            "source_url": os.path.join(fixtures_dir, "college_b_btech.html"),
            "content_hash": "a1b2c3d4e5f6789012345678abcdef01",
            "last_checked": "2026-10-21T09:30:00Z",
            "status": "verified",
            "timezone": "Asia/Kolkata",
            "notes": "Faculty of Engineering mechanical admissions",
            "is_synthetic": 1
        },
        # Sample 3: Apex Technical University (B.Sc Data Science)
        {
            "college": "Apex Technical University",
            "programme": "B.Sc Data Science",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-06-20",
            "proposed_deadline": None,
            "source_url": os.path.join(fixtures_dir, "college_b_bsc.html"),
            "content_hash": "b2c3d4e5f6a1789012345678abcdef02",
            "last_checked": "2026-10-21T09:35:00Z",
            "status": "verified",
            "timezone": "Asia/Kolkata",
            "notes": "Faculty of Science undergraduate admissions",
            "is_synthetic": 1
        },
        # Sample 4: Metro State Polytechnic (Diploma)
        {
            "college": "Metro State Polytechnic",
            "programme": "Diploma in AI & Robotics",
            "year": 2026,
            "cycle": "Cycle 1",
            "deadline_date": "2026-07-01",
            "proposed_deadline": None,
            "source_url": os.path.join(fixtures_dir, "simulated_403.html"),
            "content_hash": "f9e8d7c6b5a432109876543210abcdef",
            "last_checked": "2026-10-18T14:15:00Z",
            "status": "verified",
            "timezone": "Asia/Kolkata",
            "notes": "Polytechnic diploma admissions portal",
            "is_synthetic": 1
        },
        # Sample 5: Institute of Chemical Technology (PDF Circular)
        {
            "college": "Institute of Chemical Technology",
            "programme": "M.Tech Chemical Engineering",
            "year": 2026,
            "cycle": "Autumn",
            "deadline_date": "2026-06-30",
            "proposed_deadline": None,
            "source_url": os.path.join(fixtures_dir, "college_d_notice.pdf"),
            "content_hash": "d4e5f6a1b2c3789012345678abcdef03",
            "last_checked": "2026-10-22T11:00:00Z",
            "status": "verified",
            "timezone": "Asia/Kolkata",
            "notes": "Postgraduate chemical engineering circular",
            "is_synthetic": 1
        },
        # Additional 5 records to fulfill "app starts and shows 10 sample rows" (Day 1 Done condition)
        {
            "college": "Global Business Academy",
            "programme": "MBA Executive",
            "year": 2026,
            "cycle": "Round 2",
            "deadline_date": "2026-08-15",
            "proposed_deadline": None,
            "source_url": "https://example.edu/admissions/mba-2026",
            "content_hash": None,
            "last_checked": None,
            "status": "unverified",
            "timezone": "Asia/Kolkata",
            "notes": "Synthetic sample record",
            "is_synthetic": 1
        },
        {
            "college": "Central Law University",
            "programme": "LL.M Constitutional Law",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-05-30",
            "proposed_deadline": None,
            "source_url": "https://example.edu/law/llm-deadlines",
            "content_hash": None,
            "last_checked": None,
            "status": "unverified",
            "timezone": "Asia/Kolkata",
            "notes": "Synthetic sample record",
            "is_synthetic": 1
        },
        {
            "college": "Premier Institute of Medical Sciences",
            "programme": "M.Sc Biotechnology",
            "year": 2026,
            "cycle": "Phase 1",
            "deadline_date": "2026-06-05",
            "proposed_deadline": None,
            "source_url": "https://example.edu/med/biotech-notice",
            "content_hash": None,
            "last_checked": None,
            "status": "unverified",
            "timezone": "Asia/Kolkata",
            "notes": "Synthetic sample record",
            "is_synthetic": 1
        },
        {
            "college": "National Design Institute",
            "programme": "M.Des Interaction Design",
            "year": 2026,
            "cycle": "General",
            "deadline_date": "2026-07-15",
            "proposed_deadline": None,
            "source_url": "https://example.edu/design/admissions",
            "content_hash": None,
            "last_checked": None,
            "status": "unverified",
            "timezone": "Asia/Kolkata",
            "notes": "Synthetic sample record",
            "is_synthetic": 1
        },
        {
            "college": "State School of Architecture",
            "programme": "B.Arch Urban Planning",
            "year": 2026,
            "cycle": "Main",
            "deadline_date": "2026-06-25",
            "proposed_deadline": None,
            "source_url": "https://example.edu/arch/barch-2026",
            "content_hash": None,
            "last_checked": None,
            "status": "unverified",
            "timezone": "Asia/Kolkata",
            "notes": "Synthetic sample record",
            "is_synthetic": 1
        }
    ]

    with con:
        for s in samples:
            con.execute("""
                INSERT OR IGNORE INTO deadlines (
                    college, programme, year, cycle, deadline_date,
                    proposed_deadline, source_url, content_hash,
                    last_checked, status, timezone, notes, is_synthetic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s["college"], s["programme"], s["year"], s["cycle"],
                s["deadline_date"], s["proposed_deadline"], s["source_url"],
                s["content_hash"], s["last_checked"], s["status"],
                s["timezone"], s["notes"], s["is_synthetic"]
            ))

    con.close()


if __name__ == "__main__":
    init_db()
    seed_sample_records()
    print("Database initialized and 10 sample records seeded successfully.")
