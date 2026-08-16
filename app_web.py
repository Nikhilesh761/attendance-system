"""
app_web.py — Flask-based dashboard + self-enrollment.

Replaces dashboard.py (Streamlit). Streamlit requires pandas internally, which
triggered a Device Guard block on some machines. Flask has no risky compiled
dependencies, so it installs cleanly everywhere and is simpler to share on a
college/friend network.

Run with:
    python app_web.py

Then share http://<your-local-ip>:5000 with anyone on the same network.
"""

from flask import Flask, request, render_template_string, redirect, url_for, send_file, Response, session
from datetime import datetime
from PIL import Image
from functools import wraps
import face_recognition
import numpy as np
import cv2
import io
import base64
import os
import threading
import time

from db import (
    init_db,
    get_all_students,
    get_all_students_admin,
    get_attendance_log,
    get_attendance_percentage,
    add_student,
    revoke_consent,
    delete_student,
    update_student,
    log_attendance,
    already_logged_today,
    get_periods,
    get_period,
    add_period,
    update_period,
    delete_period,
    get_period_status,
    get_period_attendance,
    get_period_absentees,
    already_logged_in_period,
    already_logged_in_session,
    create_manual_session,
    get_session_attendance,
    get_session_absentees,
)
from excel_export import generate_workbook, save_to_file as export_to_excel
import auth

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))  # set FLASK_SECRET_KEY for a stable key across restarts
init_db()
auth.init_users_table()

MATCH_TOLERANCE = 0.5

# Shared state for the live camera worker thread
camera_state = {
    "cap": None,
    "url": None,
    "latest_frame": None,
    "frame_version": 0,
    "running": False,
    "recent": [],
    "error": None,
    "scan_active": False,
    "current_period": None,
    "detected_ids": [],
    "last_report": None,
    "manual_request": None,
    "manual_session": None,
}
camera_lock = threading.Lock()


def login_required(role=None):
    """
    Use as @login_required() for any logged-in user, or @login_required(role="teacher")
    to restrict a route to teachers only. Students get redirected to their own
    attendance view if they try to reach a teacher-only route.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            user = session.get("user")
            if not user:
                return redirect(url_for("login_page"))
            if role is not None and user["role"] != role:
                return redirect(url_for("my_attendance"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


LOGIN_PAGE = """
<style>
  body { font-family: 'Courier New', monospace; max-width: 400px; margin: 80px auto; padding: 0 20px; background: #000; color: #fff; }
  h1 { font-size: 20px; letter-spacing: 2px; text-transform: uppercase; }
  input[type=text], input[type=password] { padding: 8px; width: 100%; margin: 6px 0 14px 0; background: #111; border: 1px solid #333; color: #fff; box-sizing: border-box; }
  input[type=submit] { background: #ff0000; color: #fff; border: none; padding: 10px 16px; cursor: pointer; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; width: 100%; }
  .flash.error { color: #ff5555; margin-bottom: 10px; }
  .hint { color: #888; font-size: 12px; margin-top: 16px; }
</style>
<h1>Attendance System Login</h1>
{% if error %}<p class="flash error">{{ error }}</p>{% endif %}
<form method="post">
  <label>Username</label>
  <input type="text" name="username" required>
  <label>Password</label>
  <input type="password" name="password" required>
  <input type="submit" value="Log in">
</form>
<p class="hint">Students: use your Student ID as your username (set during Self-Enroll).</p>
"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth.verify_login(username, password)
        if user:
            session["user"] = user
            if user["role"] == "teacher":
                return redirect(url_for("home"))
            return redirect(url_for("my_attendance"))
        error = "Incorrect username or password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login_page"))


def get_nav():
    user = session.get("user")
    if not user:
        return ""
    if user["role"] == "teacher":
        links = """
        <a href="/">Today's Attendance</a>
        <a href="/log">Full Log</a>
        <a href="/percent">Attendance %</a>
        <a href="/enroll">Self-Enroll</a>
        <a href="/manage">Manage Students</a>
        <a href="/live">Live Camera Scan</a>
        <a href="/live">TAKE ATTENDANCE NOW</a>
        <a href="/schedule">Class Schedule</a>
        <a href="/schedule">Period Breakdown</a>
        <a href="/reports">Attendance Reports</a>
        <a href="/export/excel">Download Excel</a>
        """
    else:
        links = '<a href="/my_attendance">My Attendance</a>'
    return f"""
    <nav>
      {links}
      <a href="/logout" style="float:right;">Log out ({user['username']})</a>
    </nav>
    <hr>
    """


@app.route("/my_attendance")
@login_required()
def my_attendance():
    user = session["user"]
    if user["role"] != "student":
        return redirect(url_for("home"))

    my_id = user["student_id"]
    log = get_attendance_log(limit=2000)
    my_rows = [r for r in log if r["student_id"] == my_id]

    html = BASE_STYLE + "<h1>My Attendance</h1>" + get_nav() + """
    {% if rows %}
    <table>
      <tr><th>Timestamp</th><th>Confidence</th></tr>
      {% for r in rows %}
      <tr><td>{{ r.timestamp }}</td><td>{{ r.confidence }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
      <p>No attendance recorded yet.</p>
    {% endif %}
    """
    return render_template_string(html, rows=my_rows)

BASE_STYLE = """
<style>
  body { font-family: 'Courier New', monospace; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #000; color: #fff; }
  h1 { font-size: 22px; letter-spacing: 2px; text-transform: uppercase; }
  h2 { font-size: 18px; margin-top: 30px; letter-spacing: 1px; }
  nav a { margin-right: 16px; text-decoration: none; color: #fff; font-weight: bold; border-bottom: 2px solid transparent; }
  nav a:hover { border-bottom: 2px solid #ff0000; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { border: 1px solid #333; padding: 8px 10px; text-align: left; font-size: 14px; color: #fff; }
  th { background: #111; text-transform: uppercase; letter-spacing: 1px; }
  .stats { display: flex; gap: 20px; margin: 16px 0; }
  .stat-box { border: 1px solid #333; border-radius: 0; padding: 12px 20px; background: #0a0a0a; }
  .stat-box .num { font-size: 26px; font-weight: bold; color: #ff0000; }
  .stat-box .label { font-size: 12px; color: #999; text-transform: uppercase; }
  button, input[type=submit] { background: #ff0000; color: #fff; border: none; padding: 10px 16px;
    border-radius: 0; cursor: pointer; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; }
  button.danger { background: #660000; }
  input[type=text] { padding: 8px; width: 250px; margin: 4px 0; background: #111; border: 1px solid #333; color: #fff; }
  .consent-box { background: #111; border: 1px solid #ff0000; padding: 14px; border-radius: 0; margin: 14px 0; color: #fff; }
  #video, #canvas { border-radius: 0; border: 1px solid #333; }
</style>
"""

# NAV is now built dynamically per-request by get_nav() (below), since the
# links shown depend on whether a teacher or student is logged in.


# Camera-recognition performance settings.
# The RTSP reader is kept separate from recognition so heavy face processing
# can never make the live video fall seconds behind the real classroom.
FACE_SCALE = 0.67
RECOGNITION_MODEL = "hog"
SCHEDULED_INTERVAL = 0.75
MANUAL_INTERVAL = 0.35
SCHEDULE_CHECK_INTERVAL = 0.50
MJPEG_INTERVAL = 0.08


def _prepare_students(students):
    """Prepare enrolled face data once per scan instead of rebuilding it per frame."""
    if not students:
        return [], [], np.empty((0, 128), dtype=np.float64)
    known_ids = [s[0] for s in students]
    known_names = [s[1] for s in students]
    known_encodings = np.asarray(
        [np.asarray(s[2], dtype=np.float64) for s in students]
    )
    return known_ids, known_names, known_encodings


def _recognize_frame(frame, prepared_students):
    """
    Recognize all faces in a frame.

    The frame is reduced to 67% before face detection/encoding. This keeps
    normal classroom faces large enough to recognize while reducing CPU work.
    """
    known_ids, known_names, known_encodings = prepared_students
    if len(known_ids) == 0:
        return []

    height, width = frame.shape[:2]
    if width > 0 and height > 0:
        small = cv2.resize(
            frame,
            (max(1, int(width * FACE_SCALE)), max(1, int(height * FACE_SCALE))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small = frame

    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    locations = face_recognition.face_locations(
        rgb, number_of_times_to_upsample=0, model=RECOGNITION_MODEL
    )
    if not locations:
        print("[recognize] no face detected in this frame")
        return []

    encodings = face_recognition.face_encodings(
        rgb, locations, num_jitters=1, model="small"
    )
    results = []
    for encoding in encodings:
        distances = face_recognition.face_distance(known_encodings, encoding)
        if len(distances) == 0:
            continue
        best_idx = int(np.argmin(distances))
        distance = float(distances[best_idx])
        print(f"[recognize] closest={known_names[best_idx]} distance={distance:.3f} "
              f"tolerance={MATCH_TOLERANCE} match={distance <= MATCH_TOLERANCE}")
        if distance <= MATCH_TOLERANCE:
            results.append({
                "id": known_ids[best_idx],
                "name": known_names[best_idx],
                "confidence": round(1.0 - distance, 3),
            })
    return results


def _finish_scan(report, should_export=False):
    if should_export:
        try:
            export_to_excel()
        except Exception:
            # Recognition/report completion must not fail because Excel export failed.
            pass
    with camera_lock:
        camera_state["last_report"] = report
        camera_state["scan_active"] = False
        camera_state["current_period"] = None
        camera_state["detected_ids"] = []
        camera_state["recent"] = []
        camera_state["manual_session"] = None


def camera_worker(url):
    """
    Keep the RTSP feed responsive while recognition runs independently.

    A dedicated reader thread continuously consumes the RTSP stream and keeps
    only the newest frame. Recognition works on that newest frame, preventing
    the camera buffer from accumulating stale frames while face_recognition
    is busy.
    """
    cap = cv2.VideoCapture(url)
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass

    if not cap.isOpened():
        with camera_lock:
            camera_state["error"] = (
                "Could not open that camera URL. Check the RTSP link and that "
                "this PC can reach the camera on the network."
            )
            camera_state["running"] = False
        return

    stop_reader = threading.Event()

    with camera_lock:
        camera_state["cap"] = cap
        camera_state["running"] = True
        camera_state["error"] = None
        camera_state["latest_frame"] = None
        camera_state["frame_version"] = 0
        camera_state["scan_active"] = False
        camera_state["current_period"] = None
        camera_state["detected_ids"] = []
        camera_state["last_report"] = None
        camera_state["manual_session"] = None

    def reader_loop():
        while not stop_reader.is_set():
            with camera_lock:
                if not camera_state["running"]:
                    break
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.05)
                continue
            with camera_lock:
                camera_state["latest_frame"] = frame
                camera_state["frame_version"] += 1

    reader = threading.Thread(
        target=reader_loop,
        name="attendance-rtsp-reader",
        daemon=True,
    )
    reader.start()

    active_mode = None
    active_key = None
    active_period = None
    active_session = None
    active_end = None
    prepared_students = ([], [], np.empty((0, 128), dtype=np.float64))
    logged_this_scan = set()
    last_recognition = 0.0
    last_processed_frame = -1
    last_schedule_check = 0.0
    pending_export = False

    try:
        while True:
            with camera_lock:
                if not camera_state["running"]:
                    break
                manual_request = camera_state.get("manual_request")
                frame = camera_state.get("latest_frame")
                frame_version = camera_state.get("frame_version", 0)

            now = time.time()
            now_dt = datetime.now()

            # Manual scans always take priority over the timetable.
            if active_mode is None and manual_request:
                session = manual_request
                with camera_lock:
                    camera_state["manual_request"] = None
                    camera_state["manual_session"] = dict(session)
                    camera_state["scan_active"] = True
                    camera_state["current_period"] = {
                        "id": None,
                        "label": session["label"],
                        "start_time": session["started_at"],
                        "end_time": "",
                        "scan_time": session["started_at"],
                        "scan_duration_sec": session["duration_sec"],
                        "enabled": 1,
                        "manual": True,
                    }
                    camera_state["detected_ids"] = []
                    camera_state["recent"] = []
                    camera_state["last_report"] = None

                active_mode = "manual"
                active_key = session["session_key"]
                active_session = session
                active_end = now + int(session["duration_sec"])
                prepared_students = _prepare_students(get_all_students())
                logged_this_scan = set()
                pending_export = False
                last_recognition = 0.0
                last_processed_frame = -1

            # Check the timetable twice per second, not on every video frame.
            if active_mode is None and now - last_schedule_check >= SCHEDULE_CHECK_INTERVAL:
                last_schedule_check = now
                status = get_period_status(now_dt)
                scan_period = status.get("scan")
                scan_key = (
                    (now_dt.strftime("%Y-%m-%d"), scan_period["id"])
                    if scan_period else None
                )

                if scan_period:
                    active_mode = "scheduled"
                    active_key = scan_key
                    active_period = dict(scan_period)
                    active_end = now + int(scan_period["scan_duration_sec"])
                    prepared_students = _prepare_students(get_all_students())
                    logged_this_scan = set()
                    pending_export = False
                    last_recognition = 0.0
                    last_processed_frame = -1
                    with camera_lock:
                        camera_state["scan_active"] = True
                        camera_state["current_period"] = dict(scan_period)
                        camera_state["detected_ids"] = []
                        camera_state["recent"] = []
                        camera_state["last_report"] = None

            # Finish the active scan and export ONCE, rather than exporting after
            # every single recognized student.
            if active_mode is not None and active_end is not None and now >= active_end:
                if active_mode == "scheduled":
                    session_date = active_key[0]
                    period_id = active_key[1]
                    period = get_period(period_id)
                    present = get_period_attendance(period_id, session_date) if period else []
                    absent = get_period_absentees(period_id, session_date) if period else []
                    report = {
                        "type": "scheduled",
                        "date": session_date,
                        "period": period,
                        "present": present,
                        "absent": absent,
                        "completed_at": datetime.now().strftime("%H:%M:%S"),
                    }
                else:
                    session_key = active_session["session_key"]
                    present = get_session_attendance(session_key)
                    absent = get_session_absentees(session_key)
                    report = {
                        "type": "manual",
                        "date": active_session["session_date"],
                        "session_key": session_key,
                        "period": {
                            "id": None,
                            "label": active_session["label"],
                            "start_time": active_session["started_at"],
                            "end_time": "",
                            "scan_time": active_session["started_at"],
                            "scan_duration_sec": active_session["duration_sec"],
                            "enabled": 1,
                        },
                        "present": present,
                        "absent": absent,
                        "completed_at": datetime.now().strftime("%H:%M:%S"),
                    }

                _finish_scan(report, should_export=pending_export)
                active_mode = None
                active_key = None
                active_period = None
                active_session = None
                active_end = None
                prepared_students = ([], [], np.empty((0, 128), dtype=np.float64))
                logged_this_scan = set()
                pending_export = False
                last_recognition = 0.0
                last_processed_frame = -1
                continue

            # Process only a fresh frame. The reader thread keeps consuming RTSP
            # frames even while this expensive operation is running.
            interval = MANUAL_INTERVAL if active_mode == "manual" else SCHEDULED_INTERVAL
            if (
                active_mode is not None
                and active_end is not None
                and now < active_end
                and frame is not None
                and frame_version != last_processed_frame
                and now - last_recognition >= interval
            ):
                last_processed_frame = frame_version
                last_recognition = now

                if prepared_students[0]:
                    results = _recognize_frame(frame, prepared_students)
                    session_date = now_dt.strftime("%Y-%m-%d")

                    for result in results:
                        sid = result["id"]
                        name = result["name"]
                        confidence = result["confidence"]

                        # One database write per student per scan.
                        if sid in logged_this_scan:
                            continue

                        if active_mode == "scheduled":
                            period_id = active_key[1]
                            if already_logged_in_period(sid, period_id, session_date):
                                logged_this_scan.add(sid)
                                continue
                            log_attendance(
                                sid, name, confidence,
                                period_id=period_id,
                                session_date=session_date,
                                period_label=active_period["label"],
                                session_key=f"{session_date}:period:{period_id}",
                                session_type="scheduled",
                            )
                        else:
                            session_key = active_session["session_key"]
                            if already_logged_in_session(sid, session_key):
                                logged_this_scan.add(sid)
                                continue
                            log_attendance(
                                sid, name, confidence,
                                period_id=None,
                                session_date=session_date,
                                period_label=active_session["label"],
                                session_key=session_key,
                                session_type="manual",
                            )

                        logged_this_scan.add(sid)
                        pending_export = True

                        with camera_lock:
                            camera_state["detected_ids"].append(sid)
                            camera_state["detected_ids"] = list(
                                dict.fromkeys(camera_state["detected_ids"])
                            )
                            camera_state["recent"].insert(
                                0,
                                {
                                    "name": name,
                                    "id": sid,
                                    "time": datetime.now().strftime("%H:%M:%S"),
                                },
                            )
                            camera_state["recent"] = camera_state["recent"][:30]

            time.sleep(0.01)
    finally:
        stop_reader.set()
        reader.join(timeout=1.5)
        try:
            cap.release()
        except Exception:
            pass

        with camera_lock:
            camera_state["cap"] = None
            camera_state["latest_frame"] = None
            camera_state["scan_active"] = False
            camera_state["current_period"] = None
            camera_state["detected_ids"] = []
            camera_state["manual_request"] = None
            camera_state["manual_session"] = None


LIVE_PAGE = BASE_STYLE + "{{ nav|safe }}" + """
<h1>Live Camera Scan</h1>
<p>The camera can stay connected continuously. Scheduled scans happen automatically, or you can take an immediate attendance report at any time.</p>

{% if error %}<div class="consent-box">{{ error }}</div>{% endif %}
{% if message %}<div class="consent-box">{{ message }}</div>{% endif %}

{% if not running %}
<form method="post" action="/start_camera">
  <input type="text" name="rtsp_url" placeholder="rtsp://user:pass@192.168.1.50:554/stream1"
         style="width:400px" value="{{ current_url or '' }}" required>
  <input type="submit" value="Start camera">
</form>
<p style="color:#999">After starting the camera, you can either wait for the timetable or use the manual scan below.</p>
{% else %}
<form method="post" action="/stop_camera">
  <p>Watching: <code>{{ current_url }}</code></p>
  <input type="submit" value="Stop camera">
</form>

<div class="stats">
  <div class="stat-box"><div class="num" id="scanState">{{ 'SCANNING' if scan_active else '0' }}</div><div class="label">Detector state</div></div>
  <div class="stat-box"><div class="num" id="periodName">{{ current_period.label if current_period else 'Ready' }}</div><div class="label">Current scan</div></div>
  <div class="stat-box"><div class="num" id="detectedCount">{{ detected_count }}</div><div class="label">Detected in scan</div></div>
</div>

<h2>Manual attendance — Scan now</h2>
<p>Use this for a demo or a class that is not on the timetable. It starts immediately and creates a separate attendance report. It does <b>not</b> wait for the scheduled time.</p>
<form method="post" action="/manual_scan" onsubmit="return startManualScan();">
  <label>Scan duration:
    <input type="number" name="duration_sec" id="manualDuration" value="10" min="3" max="120" style="width:80px"> seconds
  </label>
  <input type="submit" value="TAKE ATTENDANCE NOW" id="manualButton">
</form>

<div id="manualStatus" style="margin-top:12px;color:#aaa;"></div>

<h2>Live feed</h2>
<img src="/video_feed" width="640" style="border-radius:8px; border:1px solid #333;">

<h2>Current scan</h2>
<div id="periodInfo">Loading...</div>

<h2>Recently recognized in this scan</h2>
<ul id="recentList"></ul>

<h2>Latest report</h2>
<div id="latestReport">No report yet.</div>

<script>
let manualSubmitting = false;

function startManualScan() {
  if (manualSubmitting) return false;
  manualSubmitting = true;
  const btn = document.getElementById('manualButton');
  btn.disabled = true;
  btn.value = 'STARTING SCAN...';
  document.getElementById('manualStatus').innerText =
    'Manual scan requested. Recognition will start immediately.';
  return true;
}

async function pollState() {
  try {
    const resp = await fetch('/recent_json');
    const data = await resp.json();

    document.getElementById('scanState').innerText = data.scan_active ? 'SCANNING' : '0';
    document.getElementById('periodName').innerText =
      data.current_period ? data.current_period.label : 'Ready';
    document.getElementById('detectedCount').innerText = data.detected_count;

    const p = data.current_period;
    document.getElementById('periodInfo').innerHTML = p
      ? `<b>${p.label}</b> — ${p.manual ? 'MANUAL SCAN' : (p.start_time + ' to ' + p.end_time + ' — scan at ' + p.scan_time)}`
      : (data.next_period
          ? `<b>Next:</b> ${data.next_period.label} — ${data.next_period.start_time} to ${data.next_period.end_time} — scan at ${data.next_period.scan_time}`
          : 'Ready for a manual scan. No upcoming scheduled scan.');

    const list = document.getElementById('recentList');
    list.innerHTML = '';
    data.recent.forEach(r => {
      const li = document.createElement('li');
      li.innerText = r.time + ' — ' + r.name + ' (' + r.id + ')';
      list.appendChild(li);
    });

    if (data.last_report) {
      const r = data.last_report;
      const reportLink = r.type === 'manual'
        ? `/manual_report?session_key=${encodeURIComponent(r.session_key)}`
        : `/period_report/${r.period.id}?date=${encodeURIComponent(r.date)}`;
      document.getElementById('latestReport').innerHTML =
        `<b>${r.period.label}</b> — completed ${r.completed_at}<br>` +
        `Present: <b>${r.present.length}</b> &nbsp; | &nbsp; Absent: <b>${r.absent.length}</b>` +
        ` &nbsp; <a href="${reportLink}" style="color:#fff">Open full report</a>`;
    }
  } catch (e) {}
}
setInterval(pollState, 1000);
pollState();
</script>
{% endif %}
"""


@app.route("/live")
@login_required(role="teacher")
def live_page():
    with camera_lock:
        running = camera_state["running"]
        current_url = camera_state["url"]
        error = camera_state["error"]
    with camera_lock:
        scan_active = bool(camera_state["scan_active"])
        current_period = camera_state["current_period"]
        detected_count = len(camera_state["detected_ids"])
    return render_template_string(
        LIVE_PAGE,
        running=running,
        current_url=current_url,
        error=error,
        message=request.args.get("msg"),
        nav=get_nav(),
        scan_active=scan_active,
        current_period=current_period,
        detected_count=detected_count,
    )


@app.route("/start_camera", methods=["POST"])
@login_required(role="teacher")
def start_camera():
    rtsp_url = request.form.get("rtsp_url", "").strip()
    if not rtsp_url:
        return redirect(url_for("live_page"))
    with camera_lock:
        if camera_state["running"]:
            return redirect(url_for("live_page"))
        camera_state["url"] = rtsp_url
        camera_state["recent"] = []
        camera_state["error"] = None
    thread = threading.Thread(target=camera_worker, args=(rtsp_url,), daemon=True)
    thread.start()
    time.sleep(1.5)  # give it a moment to report an error if the URL is bad
    return redirect(url_for("live_page"))


@app.route("/manual_scan", methods=["POST"])
@login_required(role="teacher")
def manual_scan():
    duration_raw = request.form.get("duration_sec", "10").strip()
    try:
        duration = int(duration_raw)
    except ValueError:
        duration = 10
    duration = max(3, min(duration, 120))

    with camera_lock:
        if not camera_state["running"]:
            return redirect(url_for("live_page", msg="Start the camera first, then use Take Attendance Now."))
        if camera_state["scan_active"] or camera_state.get("manual_request"):
            return redirect(url_for("live_page", msg="A scan is already running. Wait for it to finish before starting another one."))

    session_info = create_manual_session()
    session_info["duration_sec"] = duration

    with camera_lock:
        camera_state["manual_request"] = session_info
        camera_state["last_report"] = None
        camera_state["detected_ids"] = []
        camera_state["recent"] = []

    return redirect(url_for("live_page", msg=f"Manual attendance scan started for {duration} seconds."))


@app.route("/stop_camera", methods=["POST"])
@login_required(role="teacher")
def stop_camera():
    with camera_lock:
        camera_state["running"] = False
    return redirect(url_for("live_page"))


@app.route("/recent_json")
@login_required(role="teacher")
def recent_json():
    status = get_period_status()
    with camera_lock:
        current = camera_state.get("current_period")
        last_report = camera_state.get("last_report")
        return {
            "recent": list(camera_state.get("recent", [])),
            "scan_active": bool(camera_state.get("scan_active", False)),
            "current_period": current,
            "detected_count": len(camera_state.get("detected_ids", [])),
            "last_report": last_report,
            "next_period": status.get("next"),
        }


def mjpeg_generator():
    while True:
        with camera_lock:
            frame = camera_state["latest_frame"]
        if frame is None:
            time.sleep(0.2)
            continue
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
        time.sleep(0.1)


@app.route("/video_feed")
@login_required(role="teacher")
def video_feed():
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/manual_report")
@login_required(role="teacher")
def manual_report():
    session_key = request.args.get("session_key", "").strip()
    if not session_key or not session_key.startswith("manual-"):
        return redirect(url_for("live_page", msg="Invalid manual scan report."))

    present = get_session_attendance(session_key)
    absent = get_session_absentees(session_key)

    # Pull session metadata from the first matching attendance row when available.
    log = get_attendance_log(limit=5000)
    rows = [r for r in log if r.get("session_key") == session_key]
    label = rows[0].get("period_label") if rows else "Manual Attendance Scan"
    date = rows[0].get("session_date") if rows else datetime.now().strftime("%Y-%m-%d")

    html = BASE_STYLE + "<h1>Manual Attendance Report</h1>" + get_nav() + """
    <h2>{{ label }}</h2>
    <p>Date: <b>{{ date }}</b></p>
    <div class="stats">
      <div class="stat-box"><div class="num">{{ present|length }}</div><div class="label">Present</div></div>
      <div class="stat-box"><div class="num">{{ absent|length }}</div><div class="label">Absent</div></div>
    </div>

    <h2>Present</h2>
    {% if present %}
    <table><tr><th>Name</th><th>ID</th><th>Time</th><th>Confidence</th></tr>
    {% for r in present %}
    <tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td>
    <td>{{ r.timestamp.split('T')[1][:8] if 'T' in r.timestamp else r.timestamp }}</td>
    <td>{{ r.confidence }}</td></tr>
    {% endfor %}</table>
    {% else %}<p>No students were recognized.</p>{% endif %}

    <h2>Absent</h2>
    {% if absent %}
    <table><tr><th>Name</th><th>ID</th></tr>
    {% for r in absent %}<tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td></tr>{% endfor %}
    </table>
    {% else %}<p>No absentees.</p>{% endif %}
    """
    return render_template_string(
        html,
        label=label,
        date=date,
        present=present,
        absent=absent,
    )


SCHEDULE_PAGE = BASE_STYLE + """
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

@app.route("/schedule/add", methods=["POST"])
@login_required(role="teacher")
def schedule_add():
    try:
        label = request.form.get("label", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()
        scan_time = request.form.get("scan_time", "").strip()
        duration_raw = request.form.get("scan_duration_sec", "30").strip()
        if not label:
            raise ValueError("Period / subject name is required.")
        if not start_time or not end_time or not scan_time:
            raise ValueError("Please fill in the class start, class end, and automatic scan time.")
        try:
            duration = int(duration_raw)
        except (TypeError, ValueError):
            raise ValueError("Scan duration must be a whole number of seconds.")
        if duration < 1 or duration > 600:
            raise ValueError("Scan duration must be between 1 and 600 seconds.")
        add_period(label, start_time, end_time, scan_time, duration)
        return redirect(url_for("schedule_page"))
    except ValueError as e:
        return redirect(url_for("schedule_page", err=str(e)))
    except Exception as e:
        return redirect(url_for("schedule_page", err=(str(e).strip() or "The schedule could not be saved.")))

@app.route("/schedule/edit/<int:period_id>", methods=["GET", "POST"])
@login_required(role="teacher")
def schedule_edit(period_id):
    period = get_period(period_id)
    if not period:
        return redirect(url_for("schedule_page", err="Period not found."))
    error = None
    if request.method == "POST":
        try:
            label=request.form.get("label", "").strip()
            start_time=request.form.get("start_time", "").strip()
            end_time=request.form.get("end_time", "").strip()
            scan_time=request.form.get("scan_time", "").strip()
            duration=int(request.form.get("scan_duration_sec", "30"))
            if not label: raise ValueError("Period / subject name is required.")
            update_period(period_id,label,start_time,end_time,scan_time,duration,enabled=True)
            return redirect(url_for("schedule_page"))
        except ValueError as e: error=str(e)
        except Exception as e: error=str(e).strip() or "The period could not be updated."
    edit_page=BASE_STYLE+"""
<style>
.edit-card{max-width:760px;margin:25px auto;background:#0b0b0b;border:1px solid #292929;border-radius:12px;padding:24px}.edit-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.edit-grid label{display:block;color:#bbb;font-size:12px;text-transform:uppercase;letter-spacing:1px;margin-bottom:7px}.edit-grid input{width:100%;box-sizing:border-box;padding:11px;background:#111;color:#fff;border:1px solid #333;border-radius:7px;font-family:inherit}.edit-actions{display:flex;gap:10px;margin-top:20px}.cancel{background:#222}.edit-error{background:#180000;border:1px solid #700;color:#ff9999;padding:12px;border-radius:8px;margin-bottom:16px}@media(max-width:600px){.edit-grid{grid-template-columns:1fr}}
</style>
{{ nav|safe }}
<div class="edit-card"><h1>EDIT CLASS PERIOD</h1><p style="color:#888;">Update the class time or automatic attendance scan.</p>{% if error %}<div class="edit-error">{{ error }}</div>{% endif %}<form method="post"><div class="edit-grid"><div><label>Period / Subject</label><input type="text" name="label" value="{{ p.label }}" required></div><div><label>Class starts</label><input type="time" name="start_time" value="{{ p.start_time }}" required></div><div><label>Class ends</label><input type="time" name="end_time" value="{{ p.end_time }}" required></div><div><label>Automatic scan</label><input type="time" name="scan_time" value="{{ p.scan_time }}" required></div><div><label>Scan duration (seconds)</label><input type="number" name="scan_duration_sec" value="{{ p.scan_duration_sec }}" min="1" max="600" required></div></div><div class="edit-actions"><button type="submit">SAVE CHANGES</button><a href="/schedule"><button type="button" class="cancel">CANCEL</button></a></div></form></div>
"""
    return render_template_string(edit_page,nav=get_nav(),p=period,error=error)

@app.route("/schedule/delete/<int:period_id>", methods=["POST"])
@login_required(role="teacher")
def schedule_delete(period_id):
    delete_period(period_id)
    return redirect(url_for("schedule_page"))

@app.route("/reports")
@login_required(role="teacher")
def reports_page():
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    periods = get_periods()
    html = BASE_STYLE + "<h1>Attendance Reports</h1>" + get_nav() + """
    <p>Select a class period to view its complete attendance report.</p>
    <form method="get" style="margin:18px 0;">
      <label>Date:
        <input type="date" name="date" value="{{ date }}">
      </label>
      <input type="submit" value="VIEW">
    </form>
    {% if periods %}
    <div class="stats">
      {% for p in periods %}
      <div class="stat-box">
        <div class="num">{{ p.label }}</div>
        <div class="label">{{ p.start_time }} – {{ p.end_time }}</div>
        <p style="margin-top:12px;">
          <a href="/period_report/{{ p.id }}?date={{ date }}">
            <button type="button">OPEN ATTENDANCE REPORT</button>
          </a>
        </p>
      </div>
      {% endfor %}
    </div>
    {% else %}
      <p>No class periods configured yet. Add them in Class Schedule.</p>
    {% endif %}
    """
    return render_template_string(html, periods=periods, date=date)

@app.route("/period_report/<int:period_id>")
@login_required(role="teacher")
def period_report(period_id):
    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    period = get_period(period_id)
    if not period:
        return redirect(url_for("schedule_page", err="Period not found."))
    present = get_period_attendance(period_id, date)
    absent = get_period_absentees(period_id, date)
    html = BASE_STYLE + "<h1>Period Attendance Report</h1>" + get_nav() + """
    <h2>{{ period.label }}</h2><p>{{ period.start_time }} – {{ period.end_time }} | Scan: {{ period.scan_time }} | Date: {{ date }}</p>
    <div class="stats"><div class="stat-box"><div class="num">{{ present|length }}</div><div class="label">Present</div></div><div class="stat-box"><div class="num">{{ absent|length }}</div><div class="label">Absent</div></div></div>
    <h2>Present</h2>
    {% if present %}<table><tr><th>Name</th><th>ID</th><th>Time</th><th>Confidence</th></tr>{% for r in present %}<tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td><td>{{ r.timestamp.split('T')[1][:8] if 'T' in r.timestamp else r.timestamp }}</td><td>{{ r.confidence }}</td></tr>{% endfor %}</table>{% else %}<p>No students were recognized.</p>{% endif %}
    <h2>Absent</h2>
    {% if absent %}<table><tr><th>Name</th><th>ID</th></tr>{% for r in absent %}<tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td></tr>{% endfor %}</table>{% else %}<p>No absentees.</p>{% endif %}
    """
    return render_template_string(html, period=period, date=date, present=present, absent=absent)

@app.route("/export/excel")
@login_required(role="teacher")
def export_excel():
    wb = generate_workbook()
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="attendance_export.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/")
@login_required(role="teacher")
def home():
    students = get_all_students()
    log = get_attendance_log(limit=500)
    today = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in log if r["timestamp"].startswith(today)]
    marked_ids = {r["student_id"] for r in today_rows}
    absent = [s for s in students if s[0] not in marked_ids]

    html = BASE_STYLE + "<h1>Attendance Dashboard</h1>" + get_nav() + """
    <div class="stats">
      <div class="stat-box"><div class="num">{{ total }}</div><div class="label">Enrolled students</div></div>
      <div class="stat-box"><div class="num">{{ present }}</div><div class="label">Present today</div></div>
      <div class="stat-box"><div class="num">{{ not_marked }}</div><div class="label">Not yet marked</div></div>
    </div>

    <h2>Today's attendance</h2>
    {% if today_rows %}
    <table>
      <tr><th>Name</th><th>ID</th><th>Time</th><th>Confidence</th></tr>
      {% for r in today_rows %}
      <tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td>
          <td>{{ r.timestamp.split('T')[1][:8] if 'T' in r.timestamp else r.timestamp }}</td>
          <td>{{ r.confidence }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
      <p>No attendance logged yet today.</p>
    {% endif %}

    {% if absent %}
    <h2>Not yet marked present</h2>
    <table>
      <tr><th>Name</th><th>ID</th></tr>
      {% for sid, name, _ in absent %}
      <tr><td>{{ name }}</td><td>{{ sid }}</td></tr>
      {% endfor %}
    </table>
    {% endif %}
    """
    return render_template_string(
        html, total=len(students), present=len(today_rows),
        not_marked=max(len(students) - len(today_rows), 0),
        today_rows=today_rows, absent=absent,
    )


@app.route("/log")
@login_required(role="teacher")
def full_log():
    log = get_attendance_log(limit=1000)
    html = BASE_STYLE + "<h1>Full Attendance Log</h1>" + get_nav() + """
    {% if log %}
    <table>
      <tr><th>Name</th><th>ID</th><th>Timestamp</th><th>Confidence</th></tr>
      {% for r in log %}
      <tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td><td>{{ r.timestamp }}</td><td>{{ r.confidence }}</td></tr>
      {% endfor %}
    </table>
    {% else %}
      <p>No attendance records yet.</p>
    {% endif %}
    """
    return render_template_string(html, log=log)


@app.route("/percent")
@login_required(role="teacher")
def percent_view():
    total_sessions = int(request.args.get("sessions", 1))
    threshold = int(request.args.get("threshold", 75))
    pct_data = get_attendance_percentage(total_sessions)
    pct_data.sort(key=lambda r: r["attendance_pct"])
    defaulters = [r for r in pct_data if r["attendance_pct"] < threshold]

    html = BASE_STYLE + "<h1>Attendance %</h1>" + get_nav() + """
    <form method="get">
      Total sessions held: <input type="text" name="sessions" value="{{ total_sessions }}" style="width:60px">
      Defaulter threshold %: <input type="text" name="threshold" value="{{ threshold }}" style="width:60px">
      <input type="submit" value="Update">
    </form>

    {% if pct_data %}
    <table>
      <tr><th>Name</th><th>ID</th><th>Attendance %</th></tr>
      {% for r in pct_data %}
      <tr><td>{{ r.name }}</td><td>{{ r.student_id }}</td><td>{{ r.attendance_pct }}%</td></tr>
      {% endfor %}
    </table>
    {% if defaulters %}
    <p style="color:#c0392b;"><b>{{ defaulters|length }} student(s) below {{ threshold }}%</b></p>
    {% endif %}
    {% else %}
      <p>No attendance data yet.</p>
    {% endif %}
    """
    return render_template_string(html, pct_data=pct_data, total_sessions=total_sessions,
                                   threshold=threshold, defaulters=defaulters)


ENROLL_PAGE = BASE_STYLE + "{{ nav|safe }}" + """
<h1>Self-Enroll</h1>
<p>Anyone with this link can enroll themselves here. Consent is required before the camera unlocks.</p>

{% if message %}
<div class="flash {{ 'error' if error else '' }}">{{ message }}</div>
{% endif %}

<div class="consent-box">
  <b>Consent</b><br>
  By checking this box, you agree that a face-derived numeric vector (not a stored photo)
  will be created from your webcam image and used only for attendance matching in this class.
  You can request deletion at any time — see Manage Students.
  <br><br>
  <label><input type="checkbox" id="consentBox"> I understand and consent to enrollment</label>
</div>

<div id="enrollForm" style="display:none;">
  <p>ID: <input type="text" id="studentId" placeholder="e.g. roll number — this is also your login username"></p>
  <p>Name: <input type="text" id="studentName" placeholder="Full name"></p>
  <p>Choose a password: <input type="password" id="studentPassword" placeholder="Password for logging in"></p>
  <p>Confirm password: <input type="password" id="studentPasswordConfirm" placeholder="Confirm password"></p>

  <video id="video" width="400" height="300" autoplay></video>
  <canvas id="canvas" width="400" height="300" style="display:none;"></canvas>
  <br>
  <button type="button" onclick="capture()">Capture photo</button>
  <span class="shot-status" id="shotStatus">0 / 3 photos captured</span>
  <br><br>
  <button type="button" onclick="submitEnrollment()">Submit enrollment</button>
</div>

<script>
const consentBox = document.getElementById('consentBox');
const enrollForm = document.getElementById('enrollForm');
const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
let shots = [];
let stream = null;

consentBox.addEventListener('change', async () => {
  if (consentBox.checked) {
    enrollForm.style.display = 'block';
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: true });
      video.srcObject = stream;
    } catch (e) {
      alert('Could not access camera: ' + e.message);
    }
  } else {
    enrollForm.style.display = 'none';
    if (stream) stream.getTracks().forEach(t => t.stop());
  }
});

function capture() {
  if (shots.length >= 3) { alert('Already have 3 photos. Submit or refresh to retake.'); return; }
  const ctx = canvas.getContext('2d');
  ctx.drawImage(video, 0, 0, 400, 300);
  shots.push(canvas.toDataURL('image/jpeg'));
  document.getElementById('shotStatus').innerText = shots.length + ' / 3 photos captured';
}

async function submitEnrollment() {
  const id = document.getElementById('studentId').value.trim();
  const name = document.getElementById('studentName').value.trim();
  const password = document.getElementById('studentPassword').value;
  const passwordConfirm = document.getElementById('studentPasswordConfirm').value;
  if (!id || !name) { alert('Enter ID and name first.'); return; }
  if (!password || password !== passwordConfirm) { alert('Passwords must be entered and match.'); return; }
  if (shots.length === 0) { alert('Capture at least one photo first.'); return; }

  const resp = await fetch('/enroll_submit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: id, name: name, password: password, photos: shots, consent: true })
  });
  const data = await resp.json();
  alert(data.message);
  if (data.success) { window.location.href = '/login'; }
}
</script>

<hr>
<h2>Or add a student manually (upload a photo instead of using the camera)</h2>
<p>Use this if someone doesn't have camera access right now, or you're enrolling on their behalf.</p>
<form method="post" action="/add_manual" enctype="multipart/form-data">
  <div class="consent-box">
    <label><input type="checkbox" name="consent" required> I confirm this person has given consent to be enrolled.</label>
  </div>
  <p>ID: <input type="text" name="id" required></p>
  <p>Name: <input type="text" name="name" required></p>
  <p>Photo (clear, single face, front-facing): <input type="file" name="photo" accept="image/*" required></p>
  <input type="submit" value="Add student">
</form>
"""


@app.route("/enroll")
def enroll_page():
    return render_template_string(ENROLL_PAGE, message=request.args.get("msg"),
                                   error=request.args.get("err") == "1", nav=get_nav())


@app.route("/add_manual", methods=["POST"])
@login_required(role="teacher")
def add_manual():
    student_id = request.form.get("id", "").strip()
    name = request.form.get("name", "").strip()
    consent = request.form.get("consent")
    file = request.files.get("photo")

    if not consent:
        return redirect(url_for("enroll_page", msg="Consent checkbox must be checked.", err="1"))
    if not student_id or not name or not file or file.filename == "":
        return redirect(url_for("enroll_page", msg="ID, name, and a photo are all required.", err="1"))

    try:
        img = Image.open(file.stream).convert("RGB")
        arr = np.array(img)
        locations = face_recognition.face_locations(arr)
        if len(locations) != 1:
            return redirect(url_for(
                "enroll_page",
                msg=f"Found {len(locations)} face(s) in the photo — need exactly 1. Try a clearer photo.",
                err="1",
            ))
        encoding = face_recognition.face_encodings(arr, locations)[0]
        add_student(student_id, name, encoding, consent_given=True)
        return redirect(url_for("manage"))
    except ValueError as e:
        return redirect(url_for("enroll_page", msg=str(e), err="1"))
    except Exception:
        return redirect(url_for("enroll_page", msg="Could not process that photo. Try a different one.", err="1"))


@app.route("/enroll_submit", methods=["POST"])
def enroll_submit():
    data = request.get_json()
    if not data or not data.get("consent"):
        return {"success": False, "message": "Consent not confirmed."}

    student_id = data.get("id", "").strip()
    name = data.get("name", "").strip()
    password = data.get("password", "")
    photos = data.get("photos", [])

    if not student_id or not name or not photos:
        return {"success": False, "message": "Missing ID, name, or photos."}
    if not password:
        return {"success": False, "message": "A password is required to create your login."}

    encodings = []
    for photo_data_url in photos:
        try:
            header, encoded = photo_data_url.split(",", 1)
            img_bytes = base64.b64decode(encoded)
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            arr = np.array(img)
            locations = face_recognition.face_locations(arr)
            if len(locations) != 1:
                continue
            enc = face_recognition.face_encodings(arr, locations)[0]
            encodings.append(enc)
        except Exception:
            continue

    if not encodings:
        return {"success": False, "message": "Could not detect a clear single face in any photo. Try again with better lighting."}

    avg_encoding = np.mean(encodings, axis=0)
    try:
        add_student(student_id, name, avg_encoding, consent_given=True)
        auth.add_user(username=student_id, password=password, role="student", student_id=student_id)
        return {"success": True, "message": f"Enrolled {name} ({student_id}) and created your login."}
    except ValueError as e:
        return {"success": False, "message": str(e)}


@app.route("/manage")
@login_required(role="teacher")
def manage():
    admin_rows = get_all_students_admin()
    html = BASE_STYLE + "<h1>Manage Students</h1>" + get_nav() + """
    {% if rows %}
    <table>
      <tr><th>ID</th><th>Name</th><th>Enrolled at</th><th>Consent</th><th>Action</th></tr>
      {% for r in rows %}
      <tr>
        <td>{{ r.student_id }}</td><td>{{ r.name }}</td><td>{{ r.enrolled_at }}</td>
        <td>{{ 'Yes' if r.consent_given else 'Revoked' }}</td>
        <td>
          <a href="/edit/{{ r.student_id }}"><button type="button">Edit</button></a>
          <form method="post" action="/revoke/{{ r.student_id }}" style="display:inline">
            <button type="submit">Revoke consent</button>
          </form>
          <form method="post" action="/delete/{{ r.student_id }}" style="display:inline">
            <button type="submit" class="danger">Delete</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
      <p>No students enrolled yet — use Self-Enroll.</p>
    {% endif %}
    """
    return render_template_string(html, rows=admin_rows)


@app.route("/edit/<student_id>", methods=["GET", "POST"])
@login_required(role="teacher")
def edit_student(student_id):
    rows = get_all_students_admin()
    student = next((r for r in rows if r["student_id"] == student_id), None)
    if not student:
        return redirect(url_for("manage"))

    error_msg = None
    if request.method == "POST":
        new_id = request.form.get("new_id", "").strip()
        new_name = request.form.get("new_name", "").strip()
        if not new_id or not new_name:
            error_msg = "ID and name can't be empty."
        else:
            try:
                update_student(student_id, new_id, new_name)
                return redirect(url_for("manage"))
            except ValueError as e:
                error_msg = str(e)

    html = BASE_STYLE + "<h1>Edit Student</h1>" + get_nav() + """
    {% if error %}<div class="flash error">{{ error }}</div>{% endif %}
    <form method="post">
      <p>ID: <input type="text" name="new_id" value="{{ s.student_id }}"></p>
      <p>Name: <input type="text" name="new_name" value="{{ s.name }}"></p>
      <input type="submit" value="Save changes">
      <a href="/manage"><button type="button">Cancel</button></a>
    </form>
    <p style="color:#888; font-size:13px;">Note: changing the ID here does not change their stored
    face encoding — they'll still be recognized the same way, just logged under the new ID/name
    going forward, and their existing attendance history is relabeled to match.</p>
    """
    return render_template_string(html, s=student, error=error_msg)


@app.route("/revoke/<student_id>", methods=["POST"])
@login_required(role="teacher")
def revoke(student_id):
    revoke_consent(student_id)
    return redirect(url_for("manage"))


@app.route("/delete/<student_id>", methods=["POST"])
@login_required(role="teacher")
def delete(student_id):
    delete_student(student_id)
    return redirect(url_for("manage"))


if __name__ == "__main__":
    print("Starting dashboard. On this device: http://localhost:5000")
    print("For others on the same WiFi: http://<your-local-ip>:5000  (run 'ipconfig' to find your IP)")
    app.run(host="0.0.0.0", port=5000, debug=False)
