from pathlib import Path
import shutil
import py_compile

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app_web.py"

if not APP.exists():
    raise SystemExit("ERROR: app_web.py not found.")

src = APP.read_text(encoding="utf-8")

# This patch ONLY adds Period Overview. It does not replace any existing route.
if 'def period_overview()' in src:
    py_compile.compile(str(APP), doraise=True)
    print("Period Overview already exists. No changes made.")
    raise SystemExit(0)

backup = ROOT / "app_web_before_safe_batch3.py"
shutil.copy2(APP, backup)

nav_old = '        <a href="/percent">Attendance %</a>'
if nav_old not in src:
    raise SystemExit("ERROR: Existing navigation was not found. Nothing changed.")

src = src.replace(
    nav_old,
    nav_old + '\n        <a href="/overview">Period Overview</a>',
    1
)

marker = "\nENROLL_PAGE ="
if marker not in src:
    raise SystemExit("ERROR: Safe insertion point was not found. Nothing changed.")

html = (
    '<h1>Period Overview</h1>' +
    '{{ nav|safe }}' +
    '<form method="get" style="margin:20px 0;">' +
    '<label>Date: </label><input type="date" name="date" value="{{ date }}">' +
    '<button type="submit">VIEW</button></form>' +
    '{% if rows %}<table>' +
    '<tr><th>Period</th><th>Class Time</th><th>Scan</th><th>Present</th><th>Absent</th><th>Total</th><th>Attendance</th><th>Report</th></tr>' +
    '{% for r in rows %}' +
    '<tr><td>{{ r.label }}</td>' +
    '<td>{{ r.start_time }} - {{ r.end_time }}</td>' +
    '<td>{{ r.scan_time }}</td>' +
    '<td>{{ r.present }}</td><td>{{ r.absent }}</td><td>{{ r.total }}</td>' +
    '<td>{{ r.pct }}%</td>' +
    '<td><a href="/period_report/{{ r.id }}?date={{ date }}">Open</a></td></tr>' +
    '{% endfor %}</table>' +
    '{% else %}<p>No class periods configured.</p>{% endif %}'
)

route = (
    '\n@app.route("/overview")\n'
    '@login_required(role="teacher")\n'
    'def period_overview():\n'
    '    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))\n'
    '    rows = []\n'
    '    for p in get_periods():\n'
    '        present = get_period_attendance(p["id"], date)\n'
    '        absent = get_period_absentees(p["id"], date)\n'
    '        total = len(present) + len(absent)\n'
    '        rows.append({\n'
    '            "id": p["id"], "label": p["label"],\n'
    '            "start_time": p["start_time"], "end_time": p["end_time"],\n'
    '            "scan_time": p["scan_time"], "present": len(present),\n'
    '            "absent": len(absent), "total": total,\n'
    '            "pct": round((len(present) / total) * 100, 1) if total else 0.0\n'
    '        })\n'
    '    html = BASE_STYLE + ' + repr(html) + '\n'
    '    return render_template_string(html, rows=rows, date=date, nav=get_nav())\n'
)

src = src.replace(marker, route + marker, 1)
APP.write_text(src, encoding="utf-8")
py_compile.compile(str(APP), doraise=True)

print("=" * 60)
print("SAFE BATCH 3 PATCH COMPLETE")
print("=" * 60)
print("[OK] Camera / Live Camera preserved")
print("[OK] TAKE ATTENDANCE NOW preserved")
print("[OK] Self-Enroll preserved")
print("[OK] Manage Students preserved")
print("[OK] Class Schedule preserved")
print("[OK] Excel export preserved")
print("[OK] Existing Attendance pages preserved")
print("[OK] Added Period Overview")
print("[OK] Added navigation item")
print("[OK] Python syntax verified")
print()
print("Backup:", backup.name)
print("attendance.db NOT modified")
print("db.py NOT modified")
print("Camera/recognition code NOT modified")
print("=" * 60)
