"""
auth.py — login layer for the attendance dashboard.

Separate users table from students: a "student" role account is linked to a
student_id (their face-recognition record), a "teacher" role account is not.
Passwords are hashed with a per-user salt (SHA-256) — adequate for a college
pilot; swap to bcrypt if this ever handles more sensitive credentials than a
classroom attendance login.
"""

import sqlite3
import hashlib
import secrets
from datetime import datetime
from contextlib import contextmanager

DB_PATH = "attendance.db"  # same file as db.py — one database for the whole app


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_users_table():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username       TEXT PRIMARY KEY,
                password_hash  TEXT NOT NULL,
                salt           TEXT NOT NULL,
                role           TEXT NOT NULL CHECK(role IN ('teacher', 'student')),
                student_id     TEXT,
                created_at     TEXT NOT NULL
            )
        """)


def _hash_password(password: str, salt: str = None):
    if salt is None:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return pw_hash, salt


def add_user(username: str, password: str, role: str, student_id: str = None) -> None:
    """
    role: 'teacher' or 'student'.
    student_id required for 'student' role — links the login to their
    face-recognition record so they only ever see their own attendance.
    """
    if role not in ("teacher", "student"):
        raise ValueError("role must be 'teacher' or 'student'")
    if role == "student" and not student_id:
        raise ValueError("student accounts require a linked student_id")

    pw_hash, salt = _hash_password(password)
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            raise ValueError(f"Username '{username}' is already taken.")
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role, student_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, pw_hash, salt, role, student_id, datetime.now().isoformat()),
        )


def verify_login(username: str, password: str):
    """Returns {'username', 'role', 'student_id'} on success, None on failure."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    pw_hash, _ = _hash_password(password, row["salt"])
    if pw_hash == row["password_hash"]:
        return {"username": row["username"], "role": row["role"], "student_id": row["student_id"]}
    return None


def change_password(username: str, new_password: str) -> None:
    pw_hash, salt = _hash_password(new_password)
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE username = ?",
            (pw_hash, salt, username),
        )


if __name__ == "__main__":
    init_users_table()
    print("Users table initialized.")
