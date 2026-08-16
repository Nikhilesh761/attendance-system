from pathlib import Path
import shutil
import py_compile
import sqlite3
import re

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app_web.py"
DB = ROOT / "db.py"
ATT_DB = ROOT / "attendance.db"

print("=" * 60)
print("BATCH 2 - SCHEDULED ATTENDANCE VERIFICATION")
print("=" * 60)

if not APP.exists():
    raise SystemExit("ERROR: app_web.py not found.")
if not DB.exists():
    raise SystemExit("ERROR: db.py not found.")
if not ATT_DB.exists():
    raise SystemExit("ERROR: attendance.db not found.")

# Backup code only. The database is never replaced.
shutil.copy2(APP, ROOT / "app_web_before_batch2_verify.py")
shutil.copy2(DB, ROOT / "db_before_batch2_verify.py")

app = APP.read_text(encoding="utf-8")
db = DB.read_text(encoding="utf-8")

checks = {
    "scheduled scanner": "get_period_status" in app and "SCHEDULE_CHECK_INTERVAL" in app,
    "manual session isolation": "create_manual_session" in app and "session_type=\"manual\"" in app,
    "scheduled session isolation": "session_type=\"scheduled\"" in app and "period:" in app,
    "scheduled duplicate prevention": "already_logged_in_period" in app,
    "manual duplicate prevention": "already_logged_in_session" in app,
    "scan finalization": "_finish_scan" in app,
    "period attendance helpers": "get_period_attendance" in app and "get_period_absentees" in app,
}

print("\nCode verification:")
failed = []
for name, ok in checks.items():
    print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    if not ok:
        failed.append(name)

# Verify the live DB has the required tables/columns without modifying it.
conn = sqlite3.connect(str(ATT_DB))
conn.row_factory = sqlite3.Row
tables = {
    r["name"]
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
}

print("\nDatabase verification:")
for table in ("students", "attendance", "class_periods"):
    ok = table in tables
    print(f"  [{'OK' if ok else 'FAIL'}] {table} table")
    if not ok:
        failed.append(f"database table: {table}")

if "class_periods" in tables:
    cols = {
        r["name"]
        for r in conn.execute("PRAGMA table_info(class_periods)").fetchall()
    }
    for col in ("id", "label", "start_time", "end_time", "scan_time",
                "scan_duration_sec", "enabled", "created_at"):
        ok = col in cols
        print(f"  [{'OK' if ok else 'FAIL'}] class_periods.{col}")
        if not ok:
            failed.append(f"class_periods column: {col}")

conn.close()

# Syntax validation.
print("\nPython syntax verification:")
try:
    py_compile.compile(str(APP), doraise=True)
    print("  [OK] app_web.py")
except Exception as e:
    print("  [FAIL] app_web.py")
    print(e)
    failed.append("app_web.py syntax")

try:
    py_compile.compile(str(DB), doraise=True)
    print("  [OK] db.py")
except Exception as e:
    print("  [FAIL] db.py")
    print(e)
    failed.append("db.py syntax")

print("\n" + "=" * 60)
if failed:
    print("VERIFICATION FAILED")
    print("No application files were modified by this verifier.")
    print("Problems:")
    for item in failed:
        print(" -", item)
else:
    print("BATCH 2 VERIFICATION PASSED")
    print("Scheduled scanning, manual/scheduled isolation, duplicate")
    print("prevention, scan finalization, and required DB structure are present.")
    print("attendance.db was NOT modified or recreated.")
print("=" * 60)
