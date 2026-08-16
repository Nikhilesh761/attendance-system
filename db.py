"""
db.py — dual-mode data layer for the attendance system.

Local dev (no DATABASE_URL set):
    Uses SQLite, exactly as before (attendance.db).

Cloud (DATABASE_URL set, e.g. on Streamlit Cloud or your local writer
pointed at Neon):
    Uses Postgres via psycopg2.

Same public API either way:
- Students / consent / face encodings
- Historical attendance
- Scheduled class periods
- Period-specific attendance
- Manual "Scan Now" attendance sessions
- Absent lists without deleting historical records

Set DATABASE_URL as an env var (locally via .env, on Streamlit Cloud via
st.secrets / Secrets manager) to switch to Postgres. Leave it unset to
keep using local SQLite (attendance.db) with zero behavior change.
"""

import os
import json
import uuid
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_PATH = "attendance.db"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3


# ---------------------------------------------------------------------------
# Connection layer
# ---------------------------------------------------------------------------

def _qmark_to_pg(sql: str) -> str:
    """Convert '?' placeholders to '%s' for psycopg2."""
    return sql.replace("?", "%s")


class _CursorAdapter:
    """Wraps a cursor so callers can keep using sqlite-style code:
    conn.execute(sql, params) -> cursor, and rows behave like dicts.
    """

    def __init__(self, conn, use_pg):
        self._conn = conn
        self._use_pg = use_pg

    def execute(self, sql, params=()):
        if self._use_pg:
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(_qmark_to_pg(sql), params)
            return cur
        else:
            return self._conn.execute(sql, params)


@contextmanager
def get_conn():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield _CursorAdapter(conn, use_pg=True)
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield _CursorAdapter(conn, use_pg=False)
            conn.commit()
        finally:
            conn.close()


def _row_get(row, key):
    """Works whether row is a sqlite3.Row or a psycopg2 RealDictRow."""
    return row[key]


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _column_exists(conn, table, column) -> bool:
    if USE_POSTGRES:
        cur = conn.execute(
            """SELECT 1 FROM information_schema.columns
               WHERE table_name=? AND column_name=?""",
            (table, column),
        )
        return cur.fetchone() is not None
    else:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return column in cols


def _add_column_if_missing(conn, table, column, definition_sqlite, definition_pg=None):
    if _column_exists(conn, table, column):
        return
    definition = definition_pg if (USE_POSTGRES and definition_pg) else definition_sqlite
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with get_conn() as conn:
        if USE_POSTGRES:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS students (
                    student_id       TEXT PRIMARY KEY,
                    name             TEXT NOT NULL,
                    encoding         TEXT NOT NULL,
                    enrolled_at      TEXT NOT NULL,
                    consent_given    INTEGER NOT NULL DEFAULT 0,
                    consent_at       TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attendance (
                    id            SERIAL PRIMARY KEY,
                    student_id    TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    timestamp     TEXT NOT NULL,
                    confidence    REAL,
                    FOREIGN KEY (student_id) REFERENCES students(student_id)
                )
            """)
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS students (
                    student_id       TEXT PRIMARY KEY,
                    name             TEXT NOT NULL,
                    encoding         TEXT NOT NULL,
                    enrolled_at      TEXT NOT NULL,
                    consent_given    INTEGER NOT NULL DEFAULT 0,
                    consent_at       TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS attendance (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id    TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    timestamp     TEXT NOT NULL,
                    confidence    REAL,
                    FOREIGN KEY (student_id) REFERENCES students(student_id)
                )
            """)

        # Safe migration for databases created by the original version.
        _add_column_if_missing(conn, "attendance", "period_id", "INTEGER")
        _add_column_if_missing(conn, "attendance", "session_date", "TEXT")
        _add_column_if_missing(conn, "attendance", "period_label", "TEXT")
        _add_column_if_missing(conn, "attendance", "session_key", "TEXT")
        _add_column_if_missing(conn, "attendance", "session_type", "TEXT DEFAULT 'scheduled'")

        if USE_POSTGRES:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS class_periods (
                    id                SERIAL PRIMARY KEY,
                    label             TEXT NOT NULL,
                    start_time        TEXT NOT NULL,
                    end_time          TEXT NOT NULL,
                    scan_time         TEXT NOT NULL,
                    scan_duration_sec INTEGER NOT NULL DEFAULT 60,
                    enabled           INTEGER NOT NULL DEFAULT 1
                )
            """)
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS class_periods (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    label             TEXT NOT NULL,
                    start_time        TEXT NOT NULL,
                    end_time          TEXT NOT NULL,
                    scan_time         TEXT NOT NULL,
                    scan_duration_sec INTEGER NOT NULL DEFAULT 60,
                    enabled           INTEGER NOT NULL DEFAULT 1
                )
            """)
        _add_column_if_missing(
            conn, "class_periods", "created_at",
            "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            definition_pg="TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        )

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_attendance_session
            ON attendance(session_key, student_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_attendance_period_date
            ON attendance(period_id, session_date, student_id)
        """)


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------

def add_student(student_id: str, name: str, encoding, consent_given: bool) -> None:
    if not consent_given:
        raise ValueError("Cannot enroll a student without recorded consent.")
    encoding_json = json.dumps(list(map(float, encoding)))
    now = datetime.now().isoformat()
    with get_conn() as conn:
        if USE_POSTGRES:
            conn.execute(
                """INSERT INTO students
                   (student_id, name, encoding, enrolled_at, consent_given, consent_at)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT (student_id) DO UPDATE SET
                       name = EXCLUDED.name,
                       encoding = EXCLUDED.encoding,
                       enrolled_at = EXCLUDED.enrolled_at,
                       consent_given = 1,
                       consent_at = EXCLUDED.consent_at""",
                (student_id, name, encoding_json, now, now),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO students "
                "(student_id, name, encoding, enrolled_at, consent_given, consent_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (student_id, name, encoding_json, now, now),
            )


def get_all_students():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT student_id, name, encoding FROM students WHERE consent_given = 1"
        ).fetchall()
    return [(r["student_id"], r["name"], json.loads(r["encoding"])) for r in rows]


def get_all_students_admin():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT student_id, name, enrolled_at, consent_given, consent_at FROM students"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_consent(student_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE students SET consent_given = 0, encoding = '' WHERE student_id = ?",
            (student_id,),
        )


def delete_student(student_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM students WHERE student_id = ?", (student_id,))


def update_student(old_id: str, new_id: str, new_name: str) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM students WHERE student_id = ?", (old_id,)
        ).fetchone()
        if not existing:
            raise ValueError(f"No student found with ID {old_id}")
        if new_id != old_id:
            clash = conn.execute(
                "SELECT 1 FROM students WHERE student_id = ?", (new_id,)
            ).fetchone()
            if clash:
                raise ValueError(f"Student ID {new_id} is already in use.")
        conn.execute(
            "UPDATE students SET student_id = ?, name = ? WHERE student_id = ?",
            (new_id, new_name, old_id),
        )
        conn.execute(
            "UPDATE attendance SET student_id = ?, name = ? WHERE student_id = ?",
            (new_id, new_name, old_id),
        )


# ---------------------------------------------------------------------------
# Class periods
# ---------------------------------------------------------------------------

def add_period(label, start_time, end_time, scan_time, scan_duration_sec=60):
    _validate_period_times(start_time, end_time, scan_time, scan_duration_sec)
    now = datetime.now().isoformat()
    with get_conn() as conn:
        if USE_POSTGRES:
            cur = conn.execute(
                """INSERT INTO class_periods
                   (label, start_time, end_time, scan_time, scan_duration_sec, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)
                   RETURNING id""",
                (label, start_time, end_time, scan_time, int(scan_duration_sec), now),
            )
            return cur.fetchone()["id"]
        else:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(class_periods)").fetchall()}
            if "created_at" in cols:
                cur = conn.execute(
                    """INSERT INTO class_periods
                       (label, start_time, end_time, scan_time, scan_duration_sec, enabled, created_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (label, start_time, end_time, scan_time, int(scan_duration_sec), now),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO class_periods
                       (label, start_time, end_time, scan_time, scan_duration_sec, enabled)
                       VALUES (?, ?, ?, ?, ?, 1)""",
                    (label, start_time, end_time, scan_time, int(scan_duration_sec)),
                )
            return cur.lastrowid


def update_period(period_id, label, start_time, end_time, scan_time, scan_duration_sec=60, enabled=True):
    _validate_period_times(start_time, end_time, scan_time, scan_duration_sec)
    with get_conn() as conn:
        conn.execute(
            """UPDATE class_periods
               SET label=?, start_time=?, end_time=?, scan_time=?,
                   scan_duration_sec=?, enabled=?
               WHERE id=?""",
            (label, start_time, end_time, scan_time,
             int(scan_duration_sec), 1 if enabled else 0, period_id),
        )


def delete_period(period_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM class_periods WHERE id=?", (period_id,))


def _validate_period_times(start_time, end_time, scan_time, duration):
    try:
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
        scan = datetime.strptime(scan_time, "%H:%M")
    except (TypeError, ValueError):
        raise ValueError("Times must use HH:MM format.")
    if end <= start:
        raise ValueError("End time must be after start time.")
    if scan < start or scan >= end:
        raise ValueError("Scan time must be inside the class period.")
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        raise ValueError("Scan duration must be a whole number of seconds.")
    if duration < 1 or duration > 600:
        raise ValueError("Scan duration must be between 1 and 600 seconds.")
    scan_end = scan + timedelta(seconds=duration)
    if scan_end > end:
        raise ValueError("Scan duration extends past the end of the class period.")


def get_periods(enabled_only=False):
    sql = "SELECT * FROM class_periods"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY start_time, id"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_period(period_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM class_periods WHERE id=?", (period_id,)).fetchone()
    return dict(row) if row else None


def _scan_window(period, date_obj):
    scan = datetime.combine(
        date_obj,
        datetime.strptime(period["scan_time"], "%H:%M").time()
    )
    end = scan + timedelta(seconds=int(period["scan_duration_sec"]))
    period_end = datetime.combine(
        date_obj,
        datetime.strptime(period["end_time"], "%H:%M").time()
    )
    if end > period_end:
        end = period_end
    return scan, end


def get_period_status(now=None):
    now = now or datetime.now()
    today = now.date()
    periods = get_periods(enabled_only=True)

    current = None
    scan = None
    next_period = None

    for p in periods:
        start = datetime.combine(today, datetime.strptime(p["start_time"], "%H:%M").time())
        end = datetime.combine(today, datetime.strptime(p["end_time"], "%H:%M").time())
        scan_start, scan_end = _scan_window(p, today)

        if start <= now < end:
            current = p
        if scan_start <= now < scan_end:
            scan = p
        if scan_start > now and next_period is None:
            next_period = p

    return {"current": current, "scan": scan, "next": next_period}


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------

def log_attendance(
    student_id: str,
    name: str,
    confidence: float,
    period_id=None,
    session_date=None,
    period_label=None,
    session_key=None,
    session_type="scheduled",
):
    timestamp = datetime.now().isoformat()
    if session_date is None:
        session_date = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO attendance
               (student_id, name, timestamp, confidence,
                period_id, session_date, period_label, session_key, session_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_id, name, timestamp, confidence, period_id,
             session_date, period_label, session_key, session_type),
        )


def already_logged_in_period(student_id, period_id, session_date):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM attendance
               WHERE student_id=? AND period_id=? AND session_date=?
               LIMIT 1""",
            (student_id, period_id, session_date),
        ).fetchone()
    return row is not None


def already_logged_in_session(student_id, session_key):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM attendance
               WHERE student_id=? AND session_key=? LIMIT 1""",
            (student_id, session_key),
        ).fetchone()
    return row is not None


def create_manual_session(label=None):
    now = datetime.now()
    session_key = "manual-" + now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    return {
        "session_key": session_key,
        "session_date": now.strftime("%Y-%m-%d"),
        "label": label or f"Manual Scan {now.strftime('%H:%M:%S')}",
        "started_at": now.strftime("%H:%M:%S"),
    }


def get_period_attendance(period_id, session_date):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT student_id, name, MIN(timestamp) AS timestamp,
                      MAX(confidence) AS confidence
               FROM attendance
               WHERE period_id=? AND session_date=?
               GROUP BY student_id, name
               ORDER BY name""",
            (period_id, session_date),
        ).fetchall()
    return [dict(r) for r in rows]


def get_period_absentees(period_id, session_date):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.student_id, s.name
               FROM students s
               WHERE s.consent_given=1
                 AND NOT EXISTS (
                     SELECT 1 FROM attendance a
                     WHERE a.student_id=s.student_id
                       AND a.period_id=?
                       AND a.session_date=?
                 )
               ORDER BY s.name""",
            (period_id, session_date),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_attendance(session_key):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT student_id, name, MIN(timestamp) AS timestamp,
                      MAX(confidence) AS confidence
               FROM attendance
               WHERE session_key=?
               GROUP BY student_id, name
               ORDER BY name""",
            (session_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_absentees(session_key):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT s.student_id, s.name
               FROM students s
               WHERE s.consent_given=1
                 AND NOT EXISTS (
                     SELECT 1 FROM attendance a
                     WHERE a.student_id=s.student_id
                       AND a.session_key=?
                 )
               ORDER BY s.name""",
            (session_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def already_logged_today(student_id: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM attendance WHERE student_id = ? AND timestamp LIKE ? LIMIT 1",
            (student_id, f"{today}%"),
        ).fetchone()
    return row is not None


def get_attendance_log(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT student_id, name, timestamp, confidence,
                      period_id, session_date, period_label, session_key, session_type
               FROM attendance
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_attendance_percentage(total_sessions: int):
    if USE_POSTGRES:
        session_expr = "COALESCE(session_key, timestamp::date::text)"
    else:
        session_expr = "COALESCE(session_key, date(timestamp))"
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT student_id, name,
                   COUNT(DISTINCT {session_expr}) AS sessions_present
            FROM attendance
            GROUP BY student_id, name
        """).fetchall()
    result = []
    for r in rows:
        pct = (r["sessions_present"] / total_sessions * 100) if total_sessions else 0
        result.append({
            "student_id": r["student_id"],
            "name": r["name"],
            "attendance_pct": round(pct, 1),
        })
    return result


if __name__ == "__main__":
    init_db()
    if USE_POSTGRES:
        print("Database initialized on Postgres (Neon) via DATABASE_URL")
    else:
        print(f"Database initialized at {DB_PATH}")
