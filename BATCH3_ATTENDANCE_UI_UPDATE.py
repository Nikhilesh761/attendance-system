from pathlib import Path
import re, shutil, py_compile

ROOT = Path(__file__).resolve().parent
APP = ROOT / 'app_web.py'
if not APP.exists():
    raise SystemExit('ERROR: app_web.py not found.')
shutil.copy2(APP, ROOT / 'app_web_before_batch3.py')
s = APP.read_text(encoding='utf-8')

if 'href="/overview"' not in s:
    s = s.replace('<a href="/percent">Attendance %</a>', '<a href="/percent">Attendance %</a>\n        <a href="/overview">Period Overview</a>', 1)

HOME = '''@app.route("/")
@login_required(role="teacher")
def home():
    students = get_all_students()
    log = get_attendance_log(limit=5000)
    today = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in log if (r.get("session_date") or r.get("timestamp", "")).startswith(today)]
    present_ids = {r["student_id"] for r in today_rows}
    total = len(students)
    present_count = len(present_ids)
    absent_count = max(total - present_count, 0)
    attendance_pct = round((present_count / total) * 100, 1) if total else 0.0
    period_rows = []
    for p in get_periods():
        present = get_period_attendance(p["id"], today)
        absent = get_period_absentees(p["id"], today)
        ptotal = len(present) + len(absent)
        period_rows.append({"id":p["id"],"label":p["label"],"start_time":p["start_time"],"end_time":p["end_time"],"scan_time":p["scan_time"],"present":len(present),"absent":len(absent),"total":ptotal,"pct":round((len(present)/ptotal)*100,1) if ptotal else 0.0})
    html = BASE_STYLE + r"""
<style>
.dash{max-width:1100px;margin:auto}.hero,.panel,.metric{border:1px solid #252525;background:#0b0b0b;border-radius:12px}.hero{background:linear-gradient(135deg,#0b0b0b,#151515);padding:26px;margin:20px 0}.hero h1{margin:0 0 7px;font-size:29px}.hero p,.muted{color:#888}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.metric{padding:20px}.value{font-size:30px;font-weight:bold}.primary .value,.pct{color:#ff0000}.label{font-size:10px;color:#777;text-transform:uppercase;letter-spacing:1px;margin-top:6px}.bar{height:7px;background:#222;border-radius:9px;overflow:hidden;margin-top:11px}.bar span{display:block;height:100%;background:#ff0000}.panel{padding:20px;margin-top:16px}.head{display:flex;justify-content:space-between;align-items:center}.head h2{margin:0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px;margin-top:14px}.card{border:1px solid #292929;border-radius:10px;padding:16px;background:#0d0d0d}.card h3{margin:0}.time{color:#888;font-size:12px;margin-top:5px}.mini{display:flex;gap:18px;margin-top:15px}.mini b{font-size:20px}.mini span{display:block;color:#777;font-size:9px;text-transform:uppercase}.action{display:inline-block;color:#fff;text-decoration:none;border:1px solid #444;border-radius:7px;padding:8px 10px;font-size:11px;margin-top:13px}.table-wrap{overflow:auto;margin-top:12px}table{min-width:700px}.empty{border:1px dashed #333;padding:24px;text-align:center;color:#777;border-radius:9px}@media(max-width:800px){.metrics{grid-template-columns:repeat(2,1fr)}}@media(max-width:500px){.metrics{grid-template-columns:1fr}}
</style>
<div class="dash"><div class="hero"><h1>TODAY'S ATTENDANCE</h1><p>Live attendance snapshot across today's configured classes.</p><div class="muted">{{ today }}</div></div>
<div class="metrics"><div class="metric primary"><div class="value">{{ attendance_pct }}%</div><div class="label">Attendance</div><div class="bar"><span style="width:{{ attendance_pct }}%"></span></div></div><div class="metric"><div class="value">{{ present_count }}</div><div class="label">Present</div></div><div class="metric"><div class="value">{{ absent_count }}</div><div class="label">Absent</div></div><div class="metric"><div class="value">{{ total }}</div><div class="label">Total Students</div></div></div>
<div class="panel"><div class="head"><div><h2>Period Breakdown</h2><div class="muted">Each period has its own attendance session.</div></div><a class="action" href="/overview">VIEW ALL</a></div>{% if period_rows %}<div class="cards">{% for p in period_rows %}<div class="card"><h3>{{ p.label }}</h3><div class="time">{{ p.start_time }} — {{ p.end_time }} · Scan {{ p.scan_time }}</div><div class="mini"><div><b>{{ p.present }}</b><span>Present</span></div><div><b>{{ p.absent }}</b><span>Absent</span></div><div><b>{{ p.total }}</b><span>Total</span></div></div><div class="pct">{{ p.pct }}%</div><div class="bar"><span style="width:{{ p.pct }}%"></span></div><a class="action" href="/period_report/{{ p.id }}?date={{ today }}">OPEN REPORT</a></div>{% endfor %}</div>{% else %}<div class="empty">No class periods configured.</div>{% endif %}</div>
<div class="panel"><div class="head"><div><h2>Latest Attendance</h2><div class="muted">Today's most recent recognition records.</div></div><a class="action" href="/log">FULL LOG</a></div>{% if today_rows %}<div class="table-wrap"><table><tr><th>Student</th><th>ID</th><th>Time</th><th>Period</th><th>Confidence</th></tr>{% for r in today_rows[:20] %}<tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td><td>{{ r.timestamp.split('T')[1][:8] if 'T' in r.timestamp else r.timestamp }}</td><td>{{ r.get('period_label') or 'Manual' }}</td><td>{{ r.confidence }}</td></tr>{% endfor %}</table></div>{% else %}<div class="empty">No attendance has been recorded today.</div>{% endif %}</div></div>
"""
    return render_template_string(html,today=today,total=total,present_count=present_count,absent_count=absent_count,attendance_pct=attendance_pct,period_rows=period_rows,today_rows=today_rows)
'''

LOG = '''@app.route("/log")
@login_required(role="teacher")
def full_log():
    date_filter=request.args.get("date","").strip(); student_filter=request.args.get("student","").strip().lower(); period_filter=request.args.get("period","").strip().lower()
    log=get_attendance_log(limit=5000); rows=[]
    for r in log:
        d=r.get("session_date") or r.get("timestamp","")[:10]; p=r.get("period_label") or ("Manual Attendance" if r.get("session_type")=="manual" else "—")
        if date_filter and d!=date_filter: continue
        if student_filter and student_filter not in f"{r.get('name','')} {r.get('student_id','')}".lower(): continue
        if period_filter and period_filter not in p.lower(): continue
        rows.append({**r,"display_date":d,"display_period":p})
    html=BASE_STYLE+r"""
<style>.log{max-width:1150px;margin:auto}.hero,.filters,.tablebox{border:1px solid #252525;background:#0b0b0b;border-radius:12px}.hero{padding:25px;margin:20px 0}.hero h1{margin:0 0 6px}.hero p,.count{color:#888}.filters{padding:15px;display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:9px}.filters input,.filters select{width:100%;box-sizing:border-box;padding:10px;background:#111;color:#fff;border:1px solid #333;border-radius:7px}.tablebox{overflow:auto;margin-top:15px}table{min-width:850px;margin:0}.badge{font-size:9px;border:1px solid #444;padding:3px 6px;border-radius:4px;text-transform:uppercase}@media(max-width:700px){.filters{grid-template-columns:1fr}}</style>
<div class="log"><div class="hero"><h1>FULL ATTENDANCE LOG</h1><p>Date, time, student, period, session type and recognition confidence.</p></div><form class="filters" method="get"><input type="date" name="date" value="{{ date_filter }}"><input name="student" placeholder="Search student or ID" value="{{ student_filter }}"><select name="period"><option value="">All periods</option>{% for p in periods %}<option value="{{ p.label|lower }}" {% if period_filter==p.label|lower %}selected{% endif %}>{{ p.label }}</option>{% endfor %}<option value="manual" {% if period_filter=='manual' %}selected{% endif %}>Manual Attendance</option></select><button>FILTER</button></form><div class="count">{{ rows|length }} record(s) shown</div>{% if rows %}<div class="tablebox"><table><tr><th>Date</th><th>Time</th><th>Student</th><th>ID</th><th>Period</th><th>Type</th><th>Confidence</th></tr>{% for r in rows %}<tr><td>{{ r.display_date }}</td><td>{{ r.timestamp.split('T')[1][:8] if 'T' in r.timestamp else r.timestamp }}</td><td>{{ r.name }}</td><td>{{ r.student_id }}</td><td>{{ r.display_period }}</td><td><span class="badge">{{ r.get('session_type') or 'attendance' }}</span></td><td>{{ r.confidence }}</td></tr>{% endfor %}</table></div>{% else %}<div class="empty">No records match the selected filters.</div>{% endif %}</div>
"""
    return render_template_string(html,rows=rows,periods=get_periods(),date_filter=date_filter,student_filter=student_filter,period_filter=period_filter)
'''

PERCENT = '''@app.route("/percent")
@login_required(role="teacher")
def percent_view():
    try: total_sessions=max(1,int(request.args.get("sessions",1)))
    except ValueError: total_sessions=1
    try: threshold=min(100,max(0,int(request.args.get("threshold",75))))
    except ValueError: threshold=75
    pct_data=get_attendance_percentage(total_sessions); pct_data.sort(key=lambda r:r["attendance_pct"]); defaulters=[r for r in pct_data if r["attendance_pct"]<threshold]; avg=round(sum(r["attendance_pct"] for r in pct_data)/len(pct_data),1) if pct_data else 0
    html=BASE_STYLE+r"""
<style>.pct{max-width:1050px;margin:auto}.hero,.controls,.student{border:1px solid #252525;background:#0b0b0b;border-radius:12px}.hero{padding:25px;margin:20px 0}.hero h1{margin:0 0 6px}.hero p,.muted{color:#888}.controls{padding:15px;display:grid;grid-template-columns:1fr 1fr auto;gap:10px}.controls input{width:100%;box-sizing:border-box;padding:10px;background:#111;color:#fff;border:1px solid #333;border-radius:7px}.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:15px}.summary>div{padding:18px;border:1px solid #252525;background:#0b0b0b;border-radius:10px}.big{font-size:30px;font-weight:bold;color:#ff0000}.student{padding:16px;margin-top:10px}.studenthead{display:flex;justify-content:space-between}.bar{height:8px;background:#222;border-radius:9px;overflow:hidden;margin-top:10px}.bar span{display:block;height:100%;background:#ff0000}.low .score{color:#ff5555}.warning{margin-top:15px;border:1px solid #600;background:#170000;color:#f99;padding:12px;border-radius:8px}@media(max-width:650px){.controls,.summary{grid-template-columns:1fr}}</style>
<div class="pct"><div class="hero"><h1>ATTENDANCE PERCENTAGE</h1><p>Student attendance performance across the selected number of sessions.</p></div><form class="controls" method="get"><div><label class="muted">SESSIONS HELD</label><input type="number" min="1" name="sessions" value="{{ total_sessions }}"></div><div><label class="muted">DEFAULTER THRESHOLD %</label><input type="number" min="0" max="100" name="threshold" value="{{ threshold }}"></div><button>UPDATE</button></form><div class="summary"><div><div class="big">{{ avg }}%</div><span class="muted">Class Average</span></div><div><div class="big">{{ pct_data|length }}</div><span class="muted">Students</span></div><div><div class="big">{{ defaulters|length }}</div><span class="muted">Below {{ threshold }}%</span></div></div>{% if defaulters %}<div class="warning"><b>{{ defaulters|length }} student(s)</b> are below the {{ threshold }}% threshold.</div>{% endif %}{% for r in pct_data %}<div class="student {% if r.attendance_pct<threshold %}low{% endif %}"><div class="studenthead"><div><b>{{ r.name }}</b><div class="muted">{{ r.student_id }}</div></div><b class="score">{{ r.attendance_pct }}%</b></div><div class="bar"><span style="width:{{ [r.attendance_pct,100]|min }}%"></span></div></div>{% else %}<div class="empty">No attendance data yet.</div>{% endfor %}</div>
"""
    return render_template_string(html,pct_data=pct_data,total_sessions=total_sessions,threshold=threshold,defaulters=defaulters,avg=avg)
'''

def replace_route(src, route, block):
    pattern=re.compile(r'@app\.route\("'+re.escape(route)+r'"[^\n]*\)\n@login_required\(role="teacher"\)\ndef [A-Za-z_]\w*\(.*?(?=\n@app\.route|\n[A-Z][A-Z0-9_]* = |\nif __name__ == "__main__":)',re.S)
    out,n=pattern.subn(block.rstrip(),src,count=1)
    if n!=1: raise SystemExit('ERROR: Could not locate route '+route)
    return out

s=replace_route(s,'/',HOME); s=replace_route(s,'/log',LOG); s=replace_route(s,'/percent',PERCENT)

if 'def period_overview()' not in s:
    OVERVIEW='''@app.route("/overview")
@login_required(role="teacher")
def period_overview():
    date=request.args.get("date",datetime.now().strftime("%Y-%m-%d")); rows=[]
    for p in get_periods():
        present=get_period_attendance(p["id"],date); absent=get_period_absentees(p["id"],date); total=len(present)+len(absent)
        rows.append({"id":p["id"],"label":p["label"],"start_time":p["start_time"],"end_time":p["end_time"],"scan_time":p["scan_time"],"present":len(present),"absent":len(absent),"total":total,"pct":round((len(present)/total)*100,1) if total else 0})
    html=BASE_STYLE+r"""
<style>.ov{max-width:1100px;margin:auto}.hero,.date,.card{border:1px solid #252525;background:#0b0b0b;border-radius:12px}.hero{padding:25px;margin:20px 0}.hero h1{margin:0 0 6px}.hero p,.muted{color:#888}.date{padding:14px;display:flex;gap:9px;align-items:end}.date input{padding:10px;background:#111;color:#fff;border:1px solid #333;border-radius:7px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:13px;margin-top:15px}.card{padding:17px}.time{color:#888;font-size:12px}.stats{display:flex;gap:20px;margin-top:15px}.stats b{font-size:21px}.pct{color:#ff0000;font-size:24px;font-weight:bold;margin-top:13px}.bar{height:8px;background:#222;border-radius:9px;overflow:hidden;margin-top:7px}.bar span{display:block;height:100%;background:#ff0000}.action{display:inline-block;margin-top:13px;color:#fff;text-decoration:none;border:1px solid #444;border-radius:7px;padding:8px 10px;font-size:11px}</style>
<div class="ov"><div class="hero"><h1>PERIOD OVERVIEW</h1><p>Attendance performance for every configured class period.</p></div><form class="date"><div><label class="muted">DATE</label><input type="date" name="date" value="{{ date }}"></div><button>VIEW</button></form>{% if rows %}<div class="cards">{% for r in rows %}<div class="card"><h2>{{ r.label }}</h2><div class="time">{{ r.start_time }} — {{ r.end_time }} · Scan {{ r.scan_time }}</div><div class="stats"><div><b>{{ r.present }}</b><div class="muted">PRESENT</div></div><div><b>{{ r.absent }}</b><div class="muted">ABSENT</div></div><div><b>{{ r.total }}</b><div class="muted">TOTAL</div></div></div><div class="pct">{{ r.pct }}%</div><div class="bar"><span style="width:{{ r.pct }}%"></span></div><a class="action" href="/period_report/{{ r.id }}?date={{ date }}">OPEN REPORT</a></div>{% endfor %}</div>{% else %}<div class="empty">No class periods configured.</div>{% endif %}</div>
"""
    return render_template_string(html,rows=rows,date=date)
'''
    marker='\nENROLL_PAGE ='
    if marker not in s: raise SystemExit('ERROR: ENROLL_PAGE marker not found.')
    s=s.replace(marker,'\n'+OVERVIEW+marker,1)

APP.write_text(s,encoding='utf-8')
py_compile.compile(str(APP),doraise=True)
print('='*60); print('BATCH 3 COMPLETE'); print('[OK] Today\'s Attendance redesign'); print('[OK] Full Attendance Log + filters'); print('[OK] Attendance Percentage redesign'); print('[OK] Period Overview'); print('[OK] Navigation updated'); print('[OK] app_web.py syntax check'); print('Backup: app_web_before_batch3.py'); print('attendance.db was NOT modified.'); print('db.py was NOT modified.'); print('Camera/recognition code was NOT replaced.'); print('='*60)
