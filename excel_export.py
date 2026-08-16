"""
excel_export.py — writes the current attendance log + student roster to an
.xlsx file using openpyxl. Deliberately NOT using pandas here — pandas is
what triggered the Device Guard block on some machines. openpyxl has no
compiled native extensions, so it installs and runs cleanly everywhere.
"""

from openpyxl import Workbook
from openpyxl.styles import Font
from db import get_attendance_log, get_all_students_admin

EXPORT_PATH = "attendance_export.xlsx"


def generate_workbook():
    wb = Workbook()

    ws1 = wb.active
    ws1.title = "Attendance Log"
    headers = ["Student ID", "Name", "Timestamp", "Confidence"]
    ws1.append(headers)
    for cell in ws1[1]:
        cell.font = Font(bold=True)
    for r in get_attendance_log(limit=100000):
        ws1.append([r["student_id"], r["name"], r["timestamp"], r["confidence"]])
    for col, width in zip("ABCD", [14, 22, 24, 12]):
        ws1.column_dimensions[col].width = width

    ws2 = wb.create_sheet("Students")
    headers2 = ["Student ID", "Name", "Enrolled At", "Consent", "Consent At"]
    ws2.append(headers2)
    for cell in ws2[1]:
        cell.font = Font(bold=True)
    for r in get_all_students_admin():
        ws2.append([
            r["student_id"], r["name"], r["enrolled_at"],
            "Yes" if r["consent_given"] else "Revoked", r["consent_at"],
        ])
    for col, width in zip("ABCDE", [14, 22, 24, 12, 24]):
        ws2.column_dimensions[col].width = width

    return wb


def save_to_file(path: str = EXPORT_PATH) -> str:
    """Regenerates the export file from current DB state. Safe to call often —
    overwrites the previous version each time, always reflects the latest data."""
    wb = generate_workbook()
    wb.save(path)
    return path


if __name__ == "__main__":
    p = save_to_file()
    print(f"Exported to {p}")
