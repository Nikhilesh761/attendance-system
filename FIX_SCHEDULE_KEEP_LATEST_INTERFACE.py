from pathlib import Path
import re, shutil, py_compile

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app_web.py'
DB = ROOT / 'db.py'

if not APP.exists() or not DB.exists():
    raise SystemExit('ERROR: app_web.py and db.py must be in the same folder as this script.')

shutil.copy2(APP, ROOT / 'app_web_before_schedule_patch.py')
shutil.copy2(DB, ROOT / 'db_before_schedule_patch.py')

app = APP.read_text(encoding='utf-8')
db = DB.read_text(encoding='utf-8')

SCHEDULE_BLOCK = r'''SCHEDULE_PAGE = BASE_STYLE + """
<style>
  .schedule-wrap { max-width: 980px; }
  .schedule-hero { border: 1px solid #222; background: linear-gradient(135deg,#0b0b0b,#151515); padding:26px; margin:20px 0 26px; border-radius:12px; }
  .schedule-hero h1 { margin:0 0 8px; font-size:30px; }
  .schedule-hero p { margin:0; color:#aaa; line-height:1.6; }
  .schedule-card { border:1px solid #252525; background:#0b0b0b; border-radius:12px; padding:22px; margin:16px 0; }
  .schedule-card h2 { margin:0 0 6px; }
  .muted { color:#888; font-size:13px; }
  .form-grid { display:grid; grid-template-columns:1.5fr 1fr 1fr 1fr 1fr; gap:14px; align-items:end; }
  .field label { display:block; color:#bbb; font-size:12px; text-transform:uppercase; letter-spacing:1px; margin-bottom:7px; }
  .field input { width:100%; box-sizing:border-box; padding:11px 12px; border:1px solid #333; border-radius:7px; background:#111; color:#fff; font-family:inherit; }
  .field small { display:block; color:#666; margin-top:5px; font-size:11px; }
  .primary-wide { margin-top:16px; width:100%; padding:13px 18px; border-radius:8px; font-weight:bold; }
  .period-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); gap:14px; margin-top:16px; }
  .period-card { border:1px solid #282828; background:#0d0d0d; border-radius:12px; padding:18px; }
  .period-top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
  .period-number { color:#ff0000; font-size:11px; text-transform:uppercase; letter-spacing:1.5px; }
  .period-name { font-size:18px; font-weight:bold; margin-top:4px; }
  .period-time { font-size:22px; margin:18px 0 8px; }
  .period-scan { color:#ccc; font-size:13px; }
  .period-actions { margin-top:18px; display:flex; gap:8px; }
  .period-actions form { margin:0; }
  .danger-outline { background:transparent; border:1px solid #660000; color:#ff6666; }
  .flash-error { border:1px solid #7a0000; background:#180000; color:#ff8c8c; border-radius:9px; padding:14px 16px; margin:14px 0; }
  .empty-state { border:1px dashed #333; border-radius:12px; padding:28px; text-align:center; color:#888; }
  @media (max-width:850px) { .form-grid { grid-template-columns:1fr 1fr; } }
  @media (max-width:560px) { .form-grid { grid-template-columns:1fr; } }
</style>
{{ nav|safe }}
<div class="schedule-wrap">
  <div class="schedule-hero">
    <h1>CLASS SCHEDULE</h1>
    <p>Set your college periods once. Attendance can automatically scan at the time you choose, while <b>TAKE ATTENDANCE NOW</b> remains available for manual demos.</p>
  </div>
  {% if error %}<div class="flash-error"><b>Could not save the period.</b><br>{{ error }}</div>{% endif %}
  <div class="schedule-card">
    <h2>Add Class Period</h2>
    <p class="muted">Example: a 09:00–09:50 class with attendance checked at 09:15.</p>
    <form method="post" action="/schedule/add" id="scheduleForm">
      <div class="form-grid">
        <div class="field"><label for="label">Period / Subject</label><input id="label" type="text" name="label" placeholder="e.g. Data Structures" required><small>Give the class a name.</small></div>
        <div class="field"><label for="start_time">Class starts</label><input id="start_time" type="time" name="start_time" required><small>Example: 09:00</small></div>
        <div class="field"><label for="end_time">Class ends</label><input id="end_time" type="time" name="end_time" required><small>Example: 09:50</small></div>
        <div class="field"><label for="scan_time">Automatic scan</label><input id="scan_time" type="time" name="scan_time" required><small>When recognition begins.</small></div>
        <div class="field"><label for="scan_duration_sec">Scan duration</label><input id="scan_duration_sec" type="number" name="scan_duration_sec" value="30" min="1" max="600" required><small>Seconds of recognition.</small></div>
      </div>
      <button type="submit" class="primary-wide">＋ ADD CLASS PERIOD</button>
    </form>
  </div>
  <div class="schedule-card">
    <h2>Today's Configured Periods</h2>
    <p class="muted">Each period has its own attendance session, so one class does not carry attendance into the next.</p>
    {% if periods %}
    <div class="period-grid">
      {% for p in periods %}
      <div class="period-card">
        <div class="period-top"><div><div class="period-number">Period {{ loop.index }}</div><div class="period-name">{{ p.label }}</div></div><div style="color:#666;font-size:12px;">ID {{ p.id }}</div></div>
        <div class="period-time">{{ p.start_time }} — {{ p.end_time }}</div>
        <div class="period-scan">📷 Automatic scan: <b>{{ p.scan_time }}</b></div>
        <div class="period-scan" style="margin-top:6px;">⏱ Scan duration: <b>{{ p.scan_duration_sec }} seconds</b></div>
        <div class="period-actions"><a href="/schedule/edit/{{ p.id }}"><button type="button">EDIT</button></a><form method="post" action="/schedule/delete/{{ p.id }}" onsubmit="return confirm('Delete this period? Historical attendance will NOT be deleted.');"><button type="submit" class="danger-outline">DELETE</button></form></div>
      </div>
      {% endfor %}
    </div>
    {% else %}<div class="empty-state"><div style="font-size:28px;margin-bottom:8px;">＋</div><b>No periods configured yet.</b><div style="margin-top:6px;">Add your first class above.</div></div>{% endif %}
  </div>
</div>
<script>
(function(){
  const form=document.getElementById('scheduleForm'), start=document.getElementById('start_time'), end=document.getElementById('end_time'), scan=document.getElementById('scan_time'), duration=document.getElementById('scan_duration_sec');
  form.addEventListener('submit',function(e){
    if(!start.value||!end.value||!scan.value)return;
    const mins=v=>v.split(':').reduce((h,m)=>h*60+Number(m),0);
    const s=mins(start.value), en=mins(end.value), sc=mins(scan.value), sec=Number(duration.value||0);
    if(en<=s){e.preventDefault();alert('Class end time must be after the start time.');return;}
    if(sc<s||sc>=en){e.preventDefault();alert('Automatic scan time must be inside the class period.');return;}
    if(sec<1||sec>600){e.preventDefault();alert('Scan duration must be between 1 and 600 seconds.');return;}
    if(sc*60+sec>en*60){e.preventDefault();alert('The scan duration extends past the end of the class.');}
  });
})();
</script>
"""

@app.route("/schedule")
@login_required(role="teacher")
def schedule_page():
    return render_template_string(SCHEDULE_PAGE, nav=get_nav(), periods=get_periods(), error=request.args.get("err"))
'''

# Add only missing schedule UI/GET route.
if 'SCHEDULE_PAGE =' not in app:
    guard = '\nif __name__ == "__main__":'
    if guard in app:
        app = app.replace(guard, '\n' + SCHEDULE_BLOCK + '\n' + guard, 1)
    else:
        app += '\n\n' + SCHEDULE_BLOCK + '\n'
elif '@app.route("/schedule")' not in app:
    route = '''\n@app.route("/schedule")\n@login_required(role="teacher")\ndef schedule_page():\n    return render_template_string(SCHEDULE_PAGE, nav=get_nav(), periods=get_periods(), error=request.args.get("err"))\n'''
    guard = '\nif __name__ == "__main__":'
    if guard in app:
        app = app.replace(guard, route + '\n' + guard, 1)
    else:
        app += '\n' + route + '\n'
APP.write_text(app, encoding='utf-8')

# Add a safe created_at migration and make add_period work with the existing DB.
if '"class_periods", "created_at"' not in db:
    marker = '        conn.execute("""\n            CREATE INDEX IF NOT EXISTS idx_attendance_session'
    if marker in db:
        db = db.replace(marker, '        _add_column_if_missing(conn, "class_periods", "created_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")\n\n' + marker, 1)

pattern = re.compile(r'def add_period\(label, start_time, end_time, scan_time, scan_duration_sec=60\):.*?(?=\n\ndef update_period\()', re.S)
replacement = '''def add_period(label, start_time, end_time, scan_time, scan_duration_sec=60):
    _validate_period_times(start_time, end_time, scan_time, scan_duration_sec)
    now = datetime.now().isoformat()
    with get_conn() as conn:
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
'''
db, count = pattern.subn(lambda m: replacement, db, count=1)
if count != 1:
    raise SystemExit('ERROR: add_period() was not found; files were backed up but not modified.')
DB.write_text(db, encoding='utf-8')

py_compile.compile(str(APP), doraise=True)
py_compile.compile(str(DB), doraise=True)
print('SUCCESS: latest interface preserved; schedule route/template and created_at handling patched.')
print('Backups: app_web_before_schedule_patch.py and db_before_schedule_patch.py')
print('attendance.db was NOT modified or recreated.')
