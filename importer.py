"""
importer.py - Data ingestion, schema validation, and safe SQLite importing for Admission Deadline Tracker.

Features:
- Multi-format ingestion: CSV and Excel (.xlsx).
- Header normalization: trim whitespace, lowercase conversion.
- Strict calendar date validation (rejects impossible dates like 2026-02-30 and ambiguous formats).
- Academic year bounds & type validation.
- IANA timezone validation.
- In-file duplicate detection and SQLite database duplicate key checks (college, programme, year, cycle).
- Human-in-the-loop verification enforcement: uploaded deadline_date is only treated as verified/approved
  when explicitly confirmed by the user; otherwise stored as candidate proposed_deadline with status='unverified'.
- Transactional atomicity: batch insert rolls back on database failure.
- Reminder generation only for approved dates.
"""

import io
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import sqlite3
import pandas as pd

from reminders import generate_reminders_for_deadline

# File upload security limits
MAX_UPLOAD_SIZE_MB = 10
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024
SUPPORTED_EXTENSIONS = {".csv", ".xlsx"}

REQUIRED_COLUMNS = ["college", "programme", "year", "source_url"]
OPTIONAL_COLUMNS = ["cycle", "deadline_date", "timezone", "notes"]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS


@dataclass
class ValidationRowResult:
    row_number: int  # 1-indexed for display (excluding header)
    data: Dict[str, Any]
    status: str      # 'valid', 'duplicate', 'invalid'
    errors: List[str] = field(default_factory=list)
    duplicate_type: Optional[str] = None  # 'file' or 'database'


@dataclass
class ValidationSummary:
    total_rows: int = 0
    valid_count: int = 0
    duplicate_count: int = 0
    invalid_count: int = 0
    is_structure_valid: bool = True
    structure_error: Optional[str] = None
    missing_columns: List[str] = field(default_factory=list)
    results: List[ValidationRowResult] = field(default_factory=list)
    valid_records: List[Dict[str, Any]] = field(default_factory=list)
    duplicate_records: List[ValidationRowResult] = field(default_factory=list)
    invalid_records: List[ValidationRowResult] = field(default_factory=list)


@dataclass
class ImportResult:
    success: bool
    inserted_count: int = 0
    skipped_count: int = 0
    rejected_count: int = 0
    inserted_ids: List[int] = field(default_factory=list)
    error_message: Optional[str] = None


def normalize_column_names(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize dataframe columns by trimming whitespace and converting to lowercase."""
    clean_cols = {}
    for col in df.columns:
        norm = str(col).strip().lower()
        clean_cols[col] = norm
    return df.rename(columns=clean_cols)


def validate_date_value(val: Any) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate date value using real calendar-date rules.
    Rejects impossible dates (e.g. 2026-02-30) and ambiguous non-ISO formats.
    Returns: (is_valid, iso_date_str, error_message)
    """
    if val is None or pd.isna(val):
        return True, None, None

    # Handle pandas Timestamp or Python date/datetime
    if isinstance(val, (datetime, date, pd.Timestamp)):
        try:
            d = val.date() if hasattr(val, "date") else val
            return True, d.isoformat(), None
        except Exception as e:
            return False, None, f"Invalid date object: {e}"

    val_str = str(val).strip()
    if not val_str:
        return True, None, None

    # Enforce strict YYYY-MM-DD pattern to prevent ambiguous formats (DD/MM/YYYY vs MM/DD/YYYY)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", val_str):
        return False, None, f"Date '{val_str}' must be in strict YYYY-MM-DD format. Ambiguous formats are rejected."

    # Validate against actual calendar (checks month lengths and leap years)
    try:
        parsed_dt = datetime.strptime(val_str, "%Y-%m-%d")
        return True, parsed_dt.date().isoformat(), None
    except ValueError as e:
        return False, None, f"Impossible or invalid calendar date '{val_str}': {e}"


def validate_year_value(val: Any) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Validate academic year.
    Must be an integer between 2000 and 2100.
    Returns: (is_valid, year_int, error_message)
    """
    if val is None or pd.isna(val):
        return False, None, "Academic year is required."

    try:
        if isinstance(val, float):
            if not val.is_integer():
                return False, None, f"Academic year must be a whole integer, received {val}."
            y = int(val)
        elif isinstance(val, int):
            y = val
        else:
            val_str = str(val).strip()
            if "." in val_str:
                f_val = float(val_str)
                if not f_val.is_integer():
                    return False, None, f"Academic year must be a whole integer, received '{val_str}'."
                y = int(f_val)
            else:
                y = int(val_str)
    except (ValueError, TypeError):
        return False, None, f"Invalid academic year '{val}'. Must be a valid integer."

    if y < 2000 or y > 2100:
        return False, None, f"Academic year {y} is outside the allowed range (2000-2100)."

    return True, y, None


def validate_timezone_value(val: Any) -> Tuple[bool, str, Optional[str]]:
    """
    Validate IANA timezone string. Defaults to 'Asia/Kolkata' if omitted or blank.
    Returns: (is_valid, tz_str, error_message)
    """
    if val is None or pd.isna(val):
        return True, "Asia/Kolkata", None
    val_str = str(val).strip()
    if not val_str:
        return True, "Asia/Kolkata", None

    try:
        ZoneInfo(val_str)
        return True, val_str, None
    except (ZoneInfoNotFoundError, ValueError, Exception):
        return False, val_str, f"Invalid IANA timezone '{val_str}'."


def validate_url_value(val: Any) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate official notice source URL.
    Returns: (is_valid, clean_url, error_message)
    """
    if val is None or pd.isna(val):
        return False, None, "source_url is required."
    val_str = str(val).strip()
    if not val_str:
        return False, None, "source_url cannot be empty."

    parsed = urllib.parse.urlparse(val_str)
    if parsed.scheme in ("http", "https", "file") and (parsed.netloc or parsed.path):
        return True, val_str, None

    # Also accept local fixture paths or relative files
    if os.path.exists(val_str) or "/" in val_str or "\\" in val_str:
        return True, val_str, None

    return False, None, f"Invalid source_url '{val_str}'. Must be a valid URL (http/https/file) or file path."


def load_file_to_dataframe(
    file_or_bytes: Union[bytes, io.BytesIO, io.StringIO, str, Any],
    filename: str,
    max_size_bytes: int = MAX_UPLOAD_SIZE_BYTES
) -> pd.DataFrame:
    """
    Safely load a CSV or Excel file into a pandas DataFrame.
    Validates file extension, size limit, and catches malformed/empty content.
    """
    _, ext = os.path.splitext(filename.lower())
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '{ext}'. Only .csv and .xlsx files are supported.")

    # Check size if bytes or file object
    content_bytes = None
    if isinstance(file_or_bytes, bytes):
        content_bytes = file_or_bytes
    elif hasattr(file_or_bytes, "getvalue"):
        content_bytes = file_or_bytes.getvalue()
    elif isinstance(file_or_bytes, str) and os.path.exists(file_or_bytes):
        content_bytes = open(file_or_bytes, "rb").read()

    if content_bytes is not None:
        if len(content_bytes) == 0:
            raise ValueError("Uploaded file is empty (0 bytes).")
        if len(content_bytes) > max_size_bytes:
            mb_size = len(content_bytes) / (1024 * 1024)
            raise ValueError(f"File size ({mb_size:.2f} MB) exceeds maximum allowed size of {max_size_bytes / (1024 * 1024):.0f} MB.")
        stream = io.BytesIO(content_bytes)
    else:
        stream = file_or_bytes

    try:
        if ext == ".csv":
            df = pd.read_csv(stream)
        elif ext == ".xlsx":
            df = pd.read_excel(stream, engine="openpyxl")
        else:
            raise ValueError(f"Unsupported extension: {ext}")
    except Exception as e:
        raise ValueError(f"Malformed or unreadable file '{filename}': {e}")

    if df.empty:
        raise ValueError("Uploaded file contains no data rows.")

    return normalize_column_names(df)


def validate_admission_dataframe(
    df: pd.DataFrame,
    con: Optional[sqlite3.Connection] = None
) -> ValidationSummary:
    """
    Comprehensive row-by-row validation of an admission dataframe.
    Checks:
    - Missing required columns
    - Required field non-emptiness
    - Academic year correctness
    - Strict calendar date correctness
    - Timezone validity
    - In-file duplicate keys
    - In-database duplicate keys (if connection provided)
    """
    summary = ValidationSummary(total_rows=len(df))

    # 1. Structure Check: required columns
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        summary.is_structure_valid = False
        summary.missing_columns = missing_cols
        summary.structure_error = f"Missing required column(s): {', '.join(missing_cols)}"
        summary.invalid_count = len(df)
        return summary

    seen_file_keys = set()

    for idx, row in df.iterrows():
        row_num = idx + 1
        row_errors = []
        raw_dict = row.to_dict()

        # College
        raw_college = row.get("college")
        clean_college = str(raw_college).strip() if raw_college is not None and not pd.isna(raw_college) else ""
        if not clean_college:
            row_errors.append("College name is required and cannot be blank.")

        # Programme
        raw_programme = row.get("programme")
        clean_programme = str(raw_programme).strip() if raw_programme is not None and not pd.isna(raw_programme) else ""
        if not clean_programme:
            row_errors.append("Programme name is required and cannot be blank.")

        # Year
        year_valid, clean_year, year_err = validate_year_value(row.get("year"))
        if not year_valid:
            row_errors.append(year_err)

        # Cycle (default: 'Main')
        raw_cycle = row.get("cycle")
        clean_cycle = "Main"
        if raw_cycle is not None and not pd.isna(raw_cycle):
            val_c = str(raw_cycle).strip()
            if val_c:
                clean_cycle = val_c

        # Source URL
        url_valid, clean_url, url_err = validate_url_value(row.get("source_url"))
        if not url_valid:
            row_errors.append(url_err)

        # Deadline Date
        date_valid, clean_date, date_err = validate_date_value(row.get("deadline_date"))
        if not date_valid:
            row_errors.append(date_err)

        # Timezone
        tz_valid, clean_tz, tz_err = validate_timezone_value(row.get("timezone"))
        if not tz_valid:
            row_errors.append(tz_err)

        # Notes
        raw_notes = row.get("notes")
        clean_notes = str(raw_notes).strip() if raw_notes is not None and not pd.isna(raw_notes) else None

        cleaned_record = {
            "college": clean_college,
            "programme": clean_programme,
            "year": clean_year,
            "cycle": clean_cycle,
            "deadline_date": clean_date,
            "source_url": clean_url,
            "timezone": clean_tz,
            "notes": clean_notes
        }

        # If format/value errors exist, classify as invalid
        if row_errors:
            res = ValidationRowResult(
                row_number=row_num,
                data=raw_dict,
                status="invalid",
                errors=row_errors
            )
            summary.invalid_records.append(res)
            summary.results.append(res)
            continue

        # Check in-file duplicate key: (college, programme, year, cycle)
        file_key = (
            clean_college.lower(),
            clean_programme.lower(),
            clean_year,
            clean_cycle.lower()
        )

        if file_key in seen_file_keys:
            dup_err = f"Duplicate record within file: '{clean_college}' / '{clean_programme}' ({clean_year}, {clean_cycle}) appears earlier in this file."
            res = ValidationRowResult(
                row_number=row_num,
                data=cleaned_record,
                status="duplicate",
                errors=[dup_err],
                duplicate_type="file"
            )
            summary.duplicate_records.append(res)
            summary.results.append(res)
            continue

        seen_file_keys.add(file_key)

        # Check database duplicate key: (college, programme, year, cycle)
        if con is not None:
            existing = con.execute("""
                SELECT id, deadline_date, status FROM deadlines
                WHERE LOWER(TRIM(college)) = LOWER(?)
                  AND LOWER(TRIM(programme)) = LOWER(?)
                  AND year = ?
                  AND LOWER(TRIM(cycle)) = LOWER(?)
            """, (clean_college, clean_programme, clean_year, clean_cycle)).fetchone()

            if existing is not None:
                dup_db_err = (
                    f"Duplicate record: Already exists in database as record #{existing['id']} "
                    f"(Current status: {existing['status']}, deadline: {existing['deadline_date'] or 'None'}). "
                    f"Skipping to prevent overwriting existing record."
                )
                res = ValidationRowResult(
                    row_number=row_num,
                    data=cleaned_record,
                    status="duplicate",
                    errors=[dup_db_err],
                    duplicate_type="database"
                )
                summary.duplicate_records.append(res)
                summary.results.append(res)
                continue

        # If we reached here, the record is fully valid!
        res = ValidationRowResult(
            row_number=row_num,
            data=cleaned_record,
            status="valid"
        )
        summary.valid_records.append(cleaned_record)
        summary.results.append(res)

    summary.valid_count = len(summary.valid_records)
    summary.duplicate_count = len(summary.duplicate_records)
    summary.invalid_count = len(summary.invalid_records)

    return summary


def import_validated_records(
    con: sqlite3.Connection,
    valid_records: List[Dict[str, Any]],
    is_verified_by_user: bool = False
) -> ImportResult:
    """
    Insert valid records into the existing deadlines table inside an atomic transaction.
    
    Data integrity rules:
    - If is_verified_by_user is True:
        deadline_date = record['deadline_date']
        status = 'verified' if deadline_date else 'unverified'
        generate_reminders_for_deadline() called if deadline_date is present.
    - If is_verified_by_user is False:
        deadline_date = None (unapproved)
        proposed_deadline = record['deadline_date'] (candidate date for human review)
        status = 'unverified'
        NO reminders generated.
    - is_synthetic = 0 (imported records are explicitly labeled as real).
    - If any database failure occurs, rolls back completely.
    """
    if not valid_records:
        return ImportResult(success=True, inserted_count=0)

    utc_now = datetime.now(timezone.utc).isoformat()
    inserted_ids = []

    try:
        with con:
            for rec in valid_records:
                candidate_date = rec.get("deadline_date")

                if is_verified_by_user:
                    approved_date = candidate_date
                    proposed_date = None
                    record_status = "verified" if approved_date else "unverified"
                else:
                    approved_date = None
                    proposed_date = candidate_date
                    record_status = "unverified"

                cur = con.execute("""
                    INSERT INTO deadlines (
                        college, programme, year, cycle, deadline_date,
                        proposed_deadline, source_url, content_hash,
                        last_checked, status, timezone, notes, is_synthetic,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 0, ?, ?)
                """, (
                    rec["college"],
                    rec["programme"],
                    rec["year"],
                    rec["cycle"],
                    approved_date,
                    proposed_date,
                    rec["source_url"],
                    utc_now,
                    record_status,
                    rec["timezone"],
                    rec["notes"],
                    utc_now,
                    utc_now
                ))
                new_id = cur.lastrowid
                inserted_ids.append(new_id)

                # Generate reminders only for records with approved dates
                if is_verified_by_user and approved_date:
                    generate_reminders_for_deadline(con, new_id)

        return ImportResult(
            success=True,
            inserted_count=len(inserted_ids),
            inserted_ids=inserted_ids
        )

    except Exception as e:
        # Atomic rollback occurs automatically via 'with con:' on exception
        return ImportResult(
            success=False,
            inserted_count=0,
            error_message=str(e)
        )
