# 🎓 Admission Deadline Tracker

> **Audited, Human-in-the-Loop University Admission Deadline Monitoring & Reconciliation System**  
> *Production architecture compliant with enterprise data integrity standards.*

---

## 🌟 Executive Summary

The **Admission Deadline Tracker** solves a critical real-world problem: university admission portals regularly update, extend, or amend deadlines via corrigenda notices, PDF circulars, and portal banners. Unmonitored portals cause missed deadlines, while naive web-scrapers silently overwrite verified data, emit false alarms due to dynamic website timestamps, or produce duplicate calendar alerts.

This production-grade system enforces strict data integrity:
- **One Record per `(college, programme, year, cycle)`**: Deterministic scoping across all admissions cycles.
- **Strict Separation of Candidate vs. Approved Dates**: Automated checks **never** mutate `deadline_date`. When a corrigendum is detected, a pending proposal is generated in `change_log`.
- **Human-in-the-Loop Approval Workflow**: Status transitions to `verified` only upon recorded administrative review (`approved_by`, `approved_at`).
- **Stable Calendar Sync (RFC 5545 iCalendar)**: Reusable deterministic UIDs prevent duplicate calendar events across multiple synchronization cycles.
- **Section-Only SHA-256 Hashing**: Ignores website banners, ads, and page-generation timestamps.
- **Audited Evidence Trail**: Every check attempt (success, change, or HTTP error) writes a persistent `check_history` row with UTC timestamps and evidence excerpts.

---

## 🛠️ Core Production Safeguards & Architectural Fixes

| # | Priority | Component | Problem Solved | Production Implementation |
| :-: | :---: | :--- | :--- | :--- |
| **1** | **P1** | `extractor.py` | Regex pattern escaping errors causing missed dates | Single-backslash raw regex supporting `DD Month YYYY`, `Month DD, YYYY`, `YYYY-MM-DD`, and `DD/MM/YYYY`. |
| **2** | **P1** | `extractor.py` / `reminders.py` | Malformed line breaks in PDF text and calendar descriptions | Proper `"\n".join()` for PyMuPDF page text and standard RFC 5545 newline formatting. |
| **3** | **P1** | `workflow.py` | String-vs-date comparison causing false change triggers | `to_date()` converter parsing ISO strings to `datetime.date` objects before comparison. |
| **4** | **P1** | `workflow.py` | Unchecked automatic date overwriting | `propose_change()` writes pending proposals to `change_log` without altering active approved dates. |
| **5** | **P2** | `reminders.py` | Duplicate calendar alerts caused by random UID regeneration | Stable UIDs stored in `reminders.calendar_uid` and preserved across regenerations. |
| **6** | **P2** | `database.py` | Missing audit fields for administrative accountability | Schema includes `approved_by`, `approved_at`, `rejected_at`, `rejection_reason`, and candidate `proposed_deadline`. |
| **7** | **P2** | `extractor.py` | False change triggers caused by website ads, timestamps, or banners | Content hashing restricted exclusively to the normalized matched notice passage containing candidate dates. |
| **8** | **P2** | `extractor.py` / `monitor.py` | Incomplete check records omitting successful check evidence | `log_check_history()` logs every attempt (success, change, or error) with HTTP code, timestamp, and matched passage. |
| **9** | **P2** | `workflow.py` / `app.py` | Status changes lacking human approval gating | Interactive Approval Center enforcing atomic transaction approval before date updates. |
| **10** | **P3** | `monitor.py` | Portal hammering from unthrottled polling loops | Configurable 5s rate-limiting delay and domain circuit breaker skipping hosts after 2 consecutive failures. |
| **11** | **P3** | `reminders.py` | Local machine timezone bias affecting deadline dates | Full IANA timezone support via `ZoneInfo` (`Asia/Kolkata` default; Windows supported via `tzdata`). |
| **12** | **P3** | `tests/` | Lack of automated regression tests | Comprehensive 15-test pytest test suite with 100% pass rate. |

---

## 📐 Architecture & Entity Relationship

```
+---------------------------------------------------------------------------------------------------+
|                                            deadlines                                              |
+---------------------------------------------------------------------------------------------------+
| id | college | programme | year | cycle | deadline_date | proposed_deadline | source_url | status  |
+---------------------------------------------------------------------------------------------------+
                               | 1:N                                     | 1:N
                               v                                         v
+-------------------------------------------------------+  +----------------------------------------+
|                      change_log                       |  |              check_history             |
+-------------------------------------------------------+  +----------------------------------------+
| id | deadline_id | old_deadline | new_deadline       |  | id | deadline_id | checked_at (UTC)   |
| reason | source_url | evidence                       |  | status | http_status | evidence        |
| approval_status ('pending' / 'approved' / 'rejected') |  | content_hash | source_url              |
| approved_by | approved_at | rejected_at | rej_reason  |  +----------------------------------------+
+-------------------------------------------------------+
                               | 1:N
                               v
+---------------------------------------------------------------------------------------------------+
|                                            reminders                                              |
+---------------------------------------------------------------------------------------------------+
| id | deadline_id | reminder_type ('30_day', '7_day', '1_day') | reminder_date | calendar_uid      |
+---------------------------------------------------------------------------------------------------+
```

---

## 🖥️ Application Features & Modules

1. **📊 Master Tracker**:
   - Central repository managing all university programmes across cycles.
   - Search by programme or university, filter by status and academic year.
   - Separate visualization of **Approved Date (Active)** versus **Candidate Date (Unapproved)**.
   - On-demand "Check Notice Now" and "Sync Reminders" actions.
   - Rapid registration form for onboarding new admission portals.

2. **⚡ Approval Center**:
   - Dedicated administrative queue for reviewing detected corrigenda.
   - Side-by-side date diff visualization with day-difference calculation.
   - Highlighted evidence excerpt from the official university notice.
   - Single-transaction atomic approval updating `deadline_date`, recording approver identity and timestamp, and refreshing calendar alerts.
   - Rejection workflow recording mandatory rejection reasons without affecting current deadlines.

3. **📅 Calendar & Alerts Hub**:
   - Automated scheduling for 30-day, 7-day, and 1-day deadline alerts.
   - Stable UID tracking verifying zero duplicate event generation.
   - RFC 5545 iCalendar (`.ics`) file download for Google Calendar, Outlook, and Apple Calendar.
   - Full CSV audit report export separating candidate and approved dates.

4. **📜 Full Audit Trail**:
   - Chronological check history with UTC ISO timestamps, HTTP response codes, SHA-256 notice hashes, and matched text evidence.
   - Historical change log documenting all proposals, approvals, and rejections.

5. **🔍 Document & Notice Inspector**:
   - Interactive document parsing testing module supporting live web portals, local HTML notices, and official PDF circulars via PyMuPDF.
   - Real-time display of extracted text, regex matches, candidate scoring, and passage hashes.

6. **🌐 Monitoring & Portal Diagnostics**:
   - Monitored host registry tracking domain health, failure counters, and circuit breaker status.
   - Background polling sweep engine with adjustable rate-limiting delay.

---

## 🚀 Quick Start Guide

### 1. Installation
```bash
cd "c:\Users\user\Deadline Tracker"
python -m pip install -r requirements.txt
```

### 2. Initialize Database & Seed Records
```bash
python generate_fixtures.py
python database.py
```

### 3. Run Automated Tests
```bash
pytest -v
```
*Output: 15 passed in ~6 seconds.*

### 4. Launch the Executive Dashboard
```bash
streamlit run app.py
```
Access the application in your browser at `http://localhost:8501`.

---

## 📁 Repository Structure

```
c:\Users\user\Deadline Tracker\
├── app.py                   # Executive Streamlit dashboard and portal diagnostics
├── database.py              # SQLite schema, audit fields, and initialization
├── extractor.py             # Notice fetcher, regex parser, section hash, and check logger
├── workflow.py              # Pure compare_deadlines(), propose_change(), and atomic approval logic
├── reminders.py             # Stable UID manager, iCalendar (.ics) generator, and CSV exporter
├── monitor.py               # Rate-limited monitoring loop with host failure circuit breaker
├── generate_fixtures.py     # Deterministic sample HTML notices and PyMuPDF PDF circular
├── requirements.txt         # Complete dependency manifest
├── tracker.db               # SQLite database file
├── fixtures/                # Sample admission portals and circulars
└── tests/
    └── test_tracker.py      # Comprehensive 15-test pytest test suite
```
