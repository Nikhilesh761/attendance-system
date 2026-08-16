"""
view_log.py — zero-dependency attendance viewer. Uses only sqlite3 (via db.py),
no pandas, no streamlit. Use this as a fallback demo if the dashboard won't
launch due to the pandas/Device Guard issue.

Usage:
    python view_log.py
"""

from datetime import datetime
from db import init_db, get_attendance_log, get_all_students

init_db()

students = get_all_students()
log = get_attendance_log(limit=200)
today = datetime.now().strftime("%Y-%m-%d")
today_rows = [r for r in log if r["timestamp"].startswith(today)]

print("=" * 60)
print("ATTENDANCE SYSTEM — LIVE LOG")
print("=" * 60)
print(f"Enrolled students: {len(students)}")
print(f"Present today: {len(today_rows)}")
print()

if today_rows:
    print(f"{'Name':<20}{'ID':<10}{'Time':<12}{'Confidence':<10}")
    print("-" * 52)
    for r in today_rows:
        time_str = r["timestamp"].split("T")[1][:8] if "T" in r["timestamp"] else r["timestamp"]
        print(f"{r['name']:<20}{r['student_id']:<10}{time_str:<12}{r['confidence']:<10}")
else:
    print("No attendance logged yet today.")

marked_ids = {r["student_id"] for r in today_rows}
absent = [s for s in students if s[0] not in marked_ids]
if absent:
    print()
    print("Not yet marked present:")
    for sid, name, _ in absent:
        print(f"  - {name} ({sid})")
