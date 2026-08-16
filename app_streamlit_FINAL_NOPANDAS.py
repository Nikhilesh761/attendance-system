"""
app_streamlit.py — complete Streamlit interface for the classroom attendance system.

This is the Streamlit UI layer only. It uses the current db.py API and keeps the
working attendance architecture intact:
- teacher/student authentication
- consent-based self enrollment
- historical attendance
- scheduled periods + period isolation
- manual "TAKE ATTENDANCE NOW" sessions
- present/absent reports
- live RTSP/IP camera scanning
- blink-based liveness check
- Excel export

Run:
    streamlit run app_streamlit.py
"""

import io
import threading
import time
from datetime import datetime, date

import cv2
import numpy as np
import streamlit as st
from PIL import Image
from openpyxl import Workbook
import face_recognition

import db
import auth

try:
    from excel_export import generate_workbook
except Exception:
    generate_workbook = None


# =====================================================================
# PAGE / THEME
# =====================================================================

st.set_page_config(
    page_title="Attendance Dashboard",
    page_icon="●",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'JetBrains Mono', monospace;
    }
    .stApp { background: #050505; color: #f5f5f5; }
    section[data-testid="stSidebar"] {
        background: #090909;
        border-right: 1px solid #252525;
    }
    .dot-header {
        display:flex; align-items:center; gap:12px;
        padding:4px 0 18px 0;
        border-bottom:1px solid #292929;
        margin-bottom:24px;
    }
    .dot-header .dot {
        width:10px; height:10px; border-radius:50%;
        background:#ff1e1e; box-shadow:0 0 8px #ff1e1e;
        flex:0 0 auto;
    }
    .dot-header h1 {
        margin:0; color:#f5f5f5;
        font-family:'Space Mono', monospace;
        font-size:23px; letter-spacing:2px; text-transform:uppercase;
    }
    .dot-header span { color:#777; font-size:11px; letter-spacing:1px; }

    div[data-testid="stMetric"] {
        background:#111;
        border:1px solid #2b2b2b;
        border-radius:3px;
        padding:14px 18px;
    }
    div[data-testid="stMetricLabel"] {
        color:#7d7d7d !important;
        font-size:10px !important;
        text-transform:uppercase;
        letter-spacing:1.5px;
    }
    div[data-testid="stMetricValue"] {
        color:#ff1e1e !important;
        font-family:'Space Mono', monospace;
    }

    div.stButton > button, div[data-testid="stFormSubmitButton"] button {
        border:1px solid #ff1e1e;
        background:#0b0b0b;
        color:#f5f5f5;
        border-radius:2px;
        text-transform:uppercase;
        letter-spacing:1px;
        font-size:11px;
    }
    div.stButton > button:hover, div[data-testid="stFormSubmitButton"] button:hover {
        background:#ff1e1e; color:#050505; border-color:#ff1e1e;
    }
    div[data-testid="stDataFrame"] {
        border:1px solid #292929;
        border-radius:3px;
    }
    hr { border-top:1px dotted #292929 !important; }
    .panel {
        border:1px solid #292929;
        background:#0b0b0b;
        padding:16px;
        border-radius:3px;
        margin-bottom:14px;
    }
    .period-card {
        border:1px solid #292929;
        background:#0b0b0b;
        padding:16px 18px;
        border-radius:3px;
        margin:8px 0;
    }
    .period-card .label {
        color:#ff1e1e;
        font-size:10px;
        letter-spacing:1.5px;
        text-transform:uppercase;
    }
    .period-card .title {
        font-family:'Space Mono', monospace;
        font-size:17px;
        margin:4px 0 10px 0;
    }
    .period-card .meta { color:#aaa; font-size:12px; line-height:1.8; }
    .big-percent {
        font-family:'Space Mono', monospace;
        font-size:48px;
        color:#ff1e1e;
        line-height:1;
    }
    .bar-bg { background:#202020; height:10px; margin-top:12px; }
    .bar-fill { background:#ff1e1e; height:10px; }
    .small-muted { color:#777; font-size:11px; }
    </style>
    """,
    unsafe_allow_html=True,
)


def dot_header(title: str, subtitle: str = ""):
    st.markdown(
        f"""
        <div class="dot-header">
            <div class="dot"></div>
            <div>
                <h1>{title}</h1>
                <span>{subtitle}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def safe_rerun():
    try:
        st.rerun()
    except Exception:
        pass


# =====================================================================
# DATABASE / AUTH
# =====================================================================

db.init_db()
auth.init_users_table()

if "user" not in st.session_state:
    dot_header("Attendance System", "Teacher or student access")
    left, right = st.columns([1, 1])
    with left:
        st.markdown('<div class="panel">', unsafe_allow_html=True)
        with st.form("login_form", clear_on_submit=False):
            st.subheader("LOGIN")
            username = st.text_input("Username / Student ID")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log In", use_container_width=True)
        st.markdown('</div>', unsafe_allow_html=True)
        if submitted:
            user = auth.verify_login(username.strip(), password)
            if user:
                st.session_state.user = user
                safe_rerun()
            else:
                st.error("Incorrect username or password.")
    with right:
        st.markdown(
            """
            <div class="panel">
              <div style="color:#ff1e1e;font-family:'Space Mono';letter-spacing:2px;">ACCESS</div>
              <p style="color:#aaa;font-size:12px;line-height:1.8;">
                Teachers receive the full attendance dashboard.<br>
                Students can access only their own attendance.<br><br>
                New students can use Self-Enroll after the teacher opens the system.
              </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.stop()

current_user = st.session_state.user


# =====================================================================
# SHARED CAMERA ENGINE
# =====================================================================

MATCH_TOLERANCE = 0.5
EAR_THRESHOLD = 0.21
BLINK_CONSEC_FRAMES = 2
LIVENESS_TIMEOUT_SEC = 8
SCHEDULED_INTERVAL = 1.0
MANUAL_INTERVAL = 0.6

if "camera_engine" not in st.session_state:
    st.session_state.camera_engine = {
        "running": False,
        "url": "",
        "error": None,
        "scan_active": False,
        "scan_mode": None,
        "current_period": None,
        "detected_ids": [],
        "recent": [],
        "last_report": None,
        "manual_request": None,
        "thread": None,
    }

CAMERA = st.session_state.camera_engine
CAMERA_LOCK = st.session_state.setdefault("camera_lock", threading.RLock())


def camera_state():
    return CAMERA


def eye_aspect_ratio(eye_points):
    eye_points = np.array(eye_points)
    a = np.linalg.norm(eye_points[1] - eye_points[5])
    b = np.linalg.norm(eye_points[2] - eye_points[4])
    c = np.linalg.norm(eye_points[0] - eye_points[3])
    return (a + b) / (2.0 * c) if c else 0.0


def liveness_check(cap, timeout=LIVENESS_TIMEOUT_SEC):
    """Basic blink liveness, matching recognize.py / auto_scanner.py."""
    start = time.time()
    consec_closed = 0
    while time.time() - start < timeout:
        ret, frame = cap.read()
        if not ret:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        landmarks_list = face_recognition.face_landmarks(rgb)
        if not landmarks_list:
            continue
        landmarks = landmarks_list[0]
        if "left_eye" not in landmarks or "right_eye" not in landmarks:
            continue
        left = eye_aspect_ratio(landmarks["left_eye"])
        right = eye_aspect_ratio(landmarks["right_eye"])
        avg = (left + right) / 2.0
        if avg < EAR_THRESHOLD:
            consec_closed += 1
        else:
            if consec_closed >= BLINK_CONSEC_FRAMES:
                return True
            consec_closed = 0
    return False


def recognize_frame(frame, students):
    if not students:
        return []
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    locations = face_recognition.face_locations(rgb)
    if not locations:
        return []
    encodings = face_recognition.face_encodings(rgb, locations)
    known_ids = [s[0] for s in students]
    known_names = [s[1] for s in students]
    known_encodings = [np.array(s[2]) for s in students]
    results = []
    for loc, encoding in zip(locations, encodings):
        distances = face_recognition.face_distance(known_encodings, encoding)
        if not len(distances):
            continue
        best = int(np.argmin(distances))
        if distances[best] <= MATCH_TOLERANCE:
            results.append({
                "id": known_ids[best],
                "name": known_names[best],
                "confidence": round(1 - float(distances[best]), 3),
                "location": loc,
            })
    return results


def reset_scan_state():
    with CAMERA_LOCK:
        CAMERA["scan_active"] = False
        CAMERA["scan_mode"] = None
        CAMERA["current_period"] = None
        CAMERA["detected_ids"] = []
        CAMERA["recent"] = []
        CAMERA["manual_request"] = None


def finish_report(mode, period, session_info, session_date):
    if mode == "scheduled":
        period_id = period["id"]
        present = db.get_period_attendance(period_id, session_date)
        absent = db.get_period_absentees(period_id, session_date)
        report = {
            "type": "scheduled",
            "date": session_date,
            "period": dict(period),
            "present": present,
            "absent": absent,
            "completed_at": datetime.now().strftime("%H:%M:%S"),
        }
    else:
        session_key = session_info["session_key"]
        present = db.get_session_attendance(session_key)
        absent = db.get_session_absentees(session_key)
        report = {
            "type": "manual",
            "date": session_info["session_date"],
            "session_key": session_key,
            "period": {
                "id": None,
                "label": session_info["label"],
                "start_time": session_info["started_at"],
                "end_time": "",
                "scan_time": session_info["started_at"],
                "scan_duration_sec": session_info["duration_sec"],
                "enabled": 1,
            },
            "present": present,
            "absent": absent,
            "completed_at": datetime.now().strftime("%H:%M:%S"),
        }
    with CAMERA_LOCK:
        CAMERA["last_report"] = report
        CAMERA["scan_active"] = False
        CAMERA["scan_mode"] = None
        CAMERA["current_period"] = None
        CAMERA["detected_ids"] = []
        CAMERA["recent"] = []
        CAMERA["manual_request"] = None
    return report


def camera_worker(url):
    cap = cv2.VideoCapture(url)
    try:
        # Best effort: keeps the RTSP reader from accumulating a large stale buffer.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not cap.isOpened():
            with CAMERA_LOCK:
                CAMERA["error"] = "Could not open the camera URL. Check RTSP/HTTP URL and network access."
                CAMERA["running"] = False
            return

        with CAMERA_LOCK:
            CAMERA["error"] = None
            CAMERA["running"] = True

        active_mode = None
        active_period = None
        active_session = None
        active_end = None
        active_key = None
        students_cache = []
        last_recognition = 0.0
        prompted = set()

        while True:
            with CAMERA_LOCK:
                if not CAMERA["running"]:
                    break
                manual_request = CAMERA.get("manual_request")

            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.1)
                continue

            with CAMERA_LOCK:
                CAMERA["latest_frame"] = frame.copy()

            now_dt = datetime.now()

            # Manual scan always wins over the timetable.
            if active_mode is None and manual_request:
                active_mode = "manual"
                active_session = dict(manual_request)
                active_end = time.time() + int(active_session["duration_sec"])
                prompted = set()
                students_cache = db.get_all_students()
                with CAMERA_LOCK:
                    CAMERA["manual_request"] = None
                    CAMERA["scan_active"] = True
                    CAMERA["scan_mode"] = "manual"
                    CAMERA["current_period"] = {
                        "id": None,
                        "label": active_session["label"],
                        "start_time": active_session["started_at"],
                        "end_time": "",
                        "scan_time": active_session["started_at"],
                        "scan_duration_sec": active_session["duration_sec"],
                        "enabled": 1,
                        "manual": True,
                    }
                    CAMERA["detected_ids"] = []
                    CAMERA["recent"] = []
                    CAMERA["last_report"] = None
                last_recognition = 0.0

            # Scheduled scan starts only in its configured scan window.
            if active_mode is None:
                status = db.get_period_status(now_dt)
                scan_period = status.get("scan")
                if scan_period:
                    active_mode = "scheduled"
                    active_period = dict(scan_period)
                    active_key = (now_dt.strftime("%Y-%m-%d"), scan_period["id"])
                    active_end = time.time() + int(scan_period["scan_duration_sec"])
                    prompted = set()
                    students_cache = db.get_all_students()
                    with CAMERA_LOCK:
                        CAMERA["scan_active"] = True
                        CAMERA["scan_mode"] = "scheduled"
                        CAMERA["current_period"] = dict(scan_period)
                        CAMERA["detected_ids"] = []
                        CAMERA["recent"] = []
                        CAMERA["last_report"] = None
                    last_recognition = 0.0

            # End active scan and generate its report.
            if active_mode is not None and active_end is not None and time.time() >= active_end:
                if active_mode == "scheduled":
                    report = finish_report(
                        "scheduled",
                        active_period,
                        None,
                        active_key[0],
                    )
                else:
                    report = finish_report(
                        "manual",
                        None,
                        active_session,
                        active_session["session_date"],
                    )
                active_mode = None
                active_period = None
                active_session = None
                active_end = None
                active_key = None
                students_cache = []
                prompted = set()
                last_recognition = 0.0
                continue

            # Recognition is deliberately throttled while the camera reader keeps
            # consuming frames continuously. This prevents stale RTSP buffers.
            if active_mode is not None and active_end is not None and time.time() < active_end:
                interval = MANUAL_INTERVAL if active_mode == "manual" else SCHEDULED_INTERVAL
                if time.time() - last_recognition >= interval:
                    last_recognition = time.time()
                    results = recognize_frame(frame, students_cache)
                    session_date = now_dt.strftime("%Y-%m-%d")
                    for result in results:
                        sid = result["id"]
                        name = result["name"]
                        confidence = result["confidence"]
                        if sid in prompted:
                            continue

                        if active_mode == "scheduled":
                            period_id = active_period["id"]
                            if db.already_logged_in_period(sid, period_id, session_date):
                                prompted.add(sid)
                                continue
                        else:
                            if db.already_logged_in_session(sid, active_session["session_key"]):
                                prompted.add(sid)
                                continue

                        prompted.add(sid)

                        # Liveness is retained from the working recognize.py pipeline.
                        live = liveness_check(cap)
                        if not live:
                            prompted.discard(sid)
                            continue

                        if active_mode == "scheduled":
                            db.log_attendance(
                                sid, name, confidence,
                                period_id=active_period["id"],
                                session_date=session_date,
                                period_label=active_period["label"],
                                session_key=f"{session_date}:period:{active_period['id']}",
                                session_type="scheduled",
                            )
                        else:
                            db.log_attendance(
                                sid, name, confidence,
                                period_id=None,
                                session_date=session_date,
                                period_label=active_session["label"],
                                session_key=active_session["session_key"],
                                session_type="manual",
                            )

                        with CAMERA_LOCK:
                            CAMERA["detected_ids"].append(sid)
                            CAMERA["detected_ids"] = list(dict.fromkeys(CAMERA["detected_ids"]))
                            CAMERA["recent"].insert(0, {
                                "name": name,
                                "id": sid,
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "confidence": confidence,
                            })
                            CAMERA["recent"] = CAMERA["recent"][:30]

            time.sleep(0.03)
    except Exception as exc:
        with CAMERA_LOCK:
            CAMERA["error"] = f"Camera worker error: {exc}"
    finally:
        cap.release()
        with CAMERA_LOCK:
            CAMERA["running"] = False
            CAMERA["scan_active"] = False
            CAMERA["scan_mode"] = None
            CAMERA["current_period"] = None
            CAMERA["manual_request"] = None
            CAMERA["latest_frame"] = None


def start_camera(url):
    with CAMERA_LOCK:
        if CAMERA["running"]:
            return False
        CAMERA["url"] = url
        CAMERA["error"] = None
        CAMERA["last_report"] = None
        CAMERA["running"] = True
    thread = threading.Thread(target=camera_worker, args=(url,), daemon=True, name="attendance-camera")
    CAMERA["thread"] = thread
    thread.start()
    return True


def stop_camera():
    with CAMERA_LOCK:
        CAMERA["running"] = False
        CAMERA["manual_request"] = None
        CAMERA["scan_active"] = False


def request_manual_scan(duration_sec):
    with CAMERA_LOCK:
        if not CAMERA["running"]:
            return False, "Start the camera first."
        if CAMERA["scan_active"] or CAMERA.get("manual_request"):
            return False, "A scan is already running. Wait for it to finish."
    session = db.create_manual_session()
    session["duration_sec"] = int(max(3, min(duration_sec, 120)))
    with CAMERA_LOCK:
        CAMERA["manual_request"] = session
        CAMERA["last_report"] = None
        CAMERA["detected_ids"] = []
        CAMERA["recent"] = []
    return True, session


# =====================================================================
# SIDEBAR NAVIGATION
# =====================================================================

st.sidebar.markdown(
    """
    <div style="padding:8px 0 18px 0;">
      <div style="font-family:'Space Mono';font-size:16px;letter-spacing:2px;color:#f5f5f5;">● ATTENDANCE</div>
      <div style="font-size:10px;color:#777;letter-spacing:1px;">FACIAL RECOGNITION SYSTEM</div>
    </div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.caption(f"Logged in as {current_user['username']} ({current_user['role']})")

if st.sidebar.button("LOG OUT", use_container_width=True):
    # Do not leave a camera thread running after the authenticated session ends.
    if current_user.get("role") == "teacher":
        stop_camera()
    del st.session_state["user"]
    safe_rerun()

st.sidebar.markdown("<hr>", unsafe_allow_html=True)

if current_user["role"] == "teacher":
    PAGES = [
        ("Today's Attendance", "◉  Today's Attendance"),
        ("Full Log", "☰  Full Log"),
        ("Attendance %", "▣  Attendance %"),
        ("Self-Enroll", "◈  Self-Enroll"),
        ("Manage Students", "⚙  Manage Students"),
        ("Live Camera Scan", "◎  Live Camera Scan"),
        ("Class Schedule", "▦  Class Schedule"),
        ("Period Reports", "◫  Period Reports"),
        ("Export", "⬇  Download Excel"),
    ]
else:
    PAGES = [("My Attendance", "◉  My Attendance")]

labels = [x[1] for x in PAGES]
selected = st.sidebar.radio("Navigate", labels, label_visibility="collapsed")
page = PAGES[labels.index(selected)][0]
st.sidebar.markdown("<hr>", unsafe_allow_html=True)
st.sidebar.caption("Local instance · single-WiFi pilot")


# =====================================================================
# STUDENT: MY ATTENDANCE
# =====================================================================

if page == "My Attendance":
    my_id = current_user["student_id"]
    dot_header("My Attendance", f"Student ID: {my_id}")
    rows = db.get_attendance_log(limit=5000)
    my_rows = [r for r in rows if r["student_id"] == my_id]
    all_sessions = {r.get("session_key") or r["timestamp"][:10] for r in rows}
    pct_rows = db.get_attendance_percentage(len(all_sessions)) if all_sessions else []
    pct = next((r["attendance_pct"] for r in pct_rows if r["student_id"] == my_id), 0)

    c1, c2, c3 = st.columns(3)
    c1.metric("Attendance %", f"{pct}%")
    c2.metric("Sessions Present", len({r.get('session_key') or r['timestamp'][:10] for r in my_rows}))
    c3.metric("Records", len(my_rows))

    if not my_rows:
        st.info("No attendance recorded yet.")
    else:
        st.dataframe(my_rows, use_container_width=True, hide_index=True)


# =====================================================================
# TEACHER: TODAY'S ATTENDANCE
# =====================================================================

elif page == "Today's Attendance":
    today_str = datetime.now().strftime("%Y-%m-%d")
    dot_header("Today's Attendance", f"Live status · {today_str}")

    all_rows = db.get_attendance_log(limit=5000)
    today_rows = [r for r in all_rows if (r.get("session_date") or r["timestamp"][:10]) == today_str]
    unique_present = {r["student_id"] for r in today_rows}
    students = db.get_all_students_admin()
    total = sum(1 for s in students if s["consent_given"])
    present = len(unique_present)
    absent = max(total - present, 0)
    pct = round((present / total) * 100, 1) if total else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Enrolled Students", total)
    c2.metric("Present Today", present)
    c3.metric("Not Yet Marked", absent)
    c4.metric("Attendance %", f"{pct}%")

    st.markdown("### Today's records")
    if today_rows:
        display_rows = []
        for r in today_rows:
            item = dict(r)
            item["time"] = item.get("timestamp", "").split("T")[-1][:8]
            cols = [c for c in ["name", "student_id", "time", "confidence", "period_label", "session_type"] if c in item]
            display_rows.append({c: item.get(c) for c in cols})
        st.dataframe(display_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No attendance logged yet today.")

    st.markdown("### Period breakdown")
    periods = db.get_periods()
    if periods:
        for p in periods:
            present_rows = db.get_period_attendance(p["id"], today_str)
            absent_rows = db.get_period_absentees(p["id"], today_str)
            period_total = len(present_rows) + len(absent_rows)
            period_pct = round((len(present_rows) / period_total) * 100, 1) if period_total else 0
            st.markdown(
                f"<div class='period-card'><div class='label'>PERIOD</div>"
                f"<div class='title'>{p['label']}</div>"
                f"<div class='meta'>{p['start_time']} — {p['end_time']} · scan {p['scan_time']} · "
                f"<b style='color:#ff1e1e'>{period_pct}%</b> · present {len(present_rows)} · absent {len(absent_rows)}</div></div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No class periods configured yet.")


# =====================================================================
# TEACHER: FULL LOG
# =====================================================================

elif page == "Full Log":
    dot_header("Full Attendance Log", "Complete historical attendance")
    rows = db.get_attendance_log(limit=5000)
    if not rows:
        st.info("No attendance records yet.")
    else:
        names = ["All"] + sorted({r.get("name") for r in rows if r.get("name")})
        selected_name = st.selectbox("Student", names)
        filtered_rows = rows if selected_name == "All" else [r for r in rows if r.get("name") == selected_name]

        types = ["All"] + sorted({r.get("session_type") for r in rows if r.get("session_type")})
        selected_type = st.selectbox("Session type", types)
        if selected_type != "All":
            filtered_rows = [r for r in filtered_rows if r.get("session_type") == selected_type]
        st.dataframe(filtered_rows, use_container_width=True, hide_index=True)


# =====================================================================
# TEACHER: ATTENDANCE PERCENTAGE
# =====================================================================

elif page == "Attendance %":
    dot_header("Attendance %", "Session-level attendance analytics")
    rows = db.get_attendance_log(limit=10000)
    session_keys = {r.get("session_key") or r["timestamp"][:10] for r in rows}
    total_sessions = len(session_keys)
    data = db.get_attendance_percentage(total_sessions) if total_sessions else []

    if not data:
        st.info("No attendance analytics available yet.")
    else:
        data = sorted(data, key=lambda r: float(r.get("attendance_pct", 0)), reverse=True)
        for r in data:
            pct = float(r["attendance_pct"])
            st.markdown(
                f"<div class='panel'>"
                f"<div style='display:flex;justify-content:space-between;align-items:end;'>"
                f"<div><div style='font-family:Space Mono;font-size:16px'>{r['name']}</div>"
                f"<div class='small-muted'>{r['student_id']}</div></div>"
                f"<div class='big-percent'>{pct:.1f}%</div></div>"
                f"<div class='bar-bg'><div class='bar-fill' style='width:{max(0,min(100,pct))}%'></div></div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        st.dataframe(data, use_container_width=True, hide_index=True)


# =====================================================================
# TEACHER: SELF-ENROLL
# =====================================================================

elif page == "Self-Enroll":
    dot_header("Self-Enroll", "Consent required before biometric data is stored")
    st.write("Create a student record, capture a face encoding, and create the student login in one step.")

    with st.form("enroll_form"):
        c1, c2 = st.columns(2)
        with c1:
            student_id = st.text_input("Student ID")
            name = st.text_input("Full name")
            password = st.text_input("Student password", type="password")
        with c2:
            confirm = st.text_input("Confirm password", type="password")
            consent = st.checkbox("I confirm that the student has consented to face data being stored for attendance recognition.")
        submitted = st.form_submit_button("Create Student + Face Enrollment", use_container_width=True)

    st.caption("You can also upload a clear single-face image instead of using the browser camera.")
    camera_photo = st.camera_input("Enrollment camera", disabled=not consent)
    uploaded_photo = st.file_uploader("Or upload a photo", type=["jpg", "jpeg", "png"], disabled=not consent)
    photo = camera_photo or uploaded_photo

    if submitted:
        if not student_id.strip() or not name.strip():
            st.error("Student ID and name are required.")
        elif not password or password != confirm:
            st.error("Passwords must be entered and match.")
        elif not consent:
            st.error("Consent is required before enrollment.")
        elif photo is None:
            st.error("Take a photo or upload one first.")
        else:
            try:
                image = Image.open(photo).convert("RGB")
                arr = np.array(image)
                locations = face_recognition.face_locations(arr)
                if len(locations) != 1:
                    st.error(f"Found {len(locations)} face(s). Enrollment requires exactly one clear face.")
                else:
                    encoding = face_recognition.face_encodings(arr, locations)[0]
                    db.add_student(student_id.strip(), name.strip(), encoding, True)
                    auth.add_user(
                        username=student_id.strip(),
                        password=password,
                        role="student",
                        student_id=student_id.strip(),
                    )
                    st.success(f"Enrolled {name.strip()} ({student_id.strip()}) and created the student login.")
            except ValueError as exc:
                st.error(str(exc))
            except Exception as exc:
                st.error(f"Enrollment failed: {exc}")


# =====================================================================
# TEACHER: MANAGE STUDENTS
# =====================================================================

elif page == "Manage Students":
    dot_header("Manage Students", "Edit, revoke consent, delete, and manage accounts")
    students = db.get_all_students_admin()
    if not students:
        st.info("No students enrolled yet.")
    else:
        st.dataframe(students, use_container_width=True, hide_index=True)
        options = [f"{s['student_id']} — {s['name']}" for s in students]
        selected = st.selectbox("Select student", options)
        s = students[options.index(selected)]

        st.markdown("### Edit student")
        c1, c2 = st.columns(2)
        with c1:
            new_id = st.text_input("Student ID", value=s["student_id"])
        with c2:
            new_name = st.text_input("Name", value=s["name"])

        a, b, c = st.columns(3)
        with a:
            if st.button("Save Changes", use_container_width=True):
                try:
                    db.update_student(s["student_id"], new_id.strip(), new_name.strip())
                    st.success("Student updated.")
                    safe_rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with b:
            if st.button("Revoke Consent", use_container_width=True):
                db.revoke_consent(s["student_id"])
                st.warning("Consent revoked. Historical attendance is kept; the student is removed from future recognition.")
                safe_rerun()
        with c:
            if st.button("Delete Student", type="primary", use_container_width=True):
                db.delete_student(s["student_id"])
                st.success("Student deleted. Historical attendance rows are retained by the database layer.")
                safe_rerun()

        st.markdown("### Student login")
        if hasattr(auth, "set_student_password"):
            new_password = st.text_input("Set / reset student password", type="password", key="reset_student_password")
            if st.button("Set Password", use_container_width=True):
                if not new_password:
                    st.error("Enter a new password.")
                else:
                    auth.set_student_password(s["student_id"], new_password)
                    st.success("Student password updated.")
        else:
            st.caption("Password reset is managed by the current auth.py implementation.")


# =====================================================================
# TEACHER: LIVE CAMERA / MANUAL SCAN
# =====================================================================

elif page == "Live Camera Scan":
    dot_header("Live Camera Scan", "RTSP/IP camera · scheduled + manual sessions")

    running = bool(CAMERA.get("running"))
    scan_active = bool(CAMERA.get("scan_active"))
    current_period = CAMERA.get("current_period")
    recent = CAMERA.get("recent", [])
    last_report = CAMERA.get("last_report")

    if not running:
        rtsp_url = st.text_input(
            "Camera stream URL",
            value=CAMERA.get("url", ""),
            placeholder="rtsp://user:password@192.168.1.50:554/stream1",
        )
        if st.button("Start Camera", use_container_width=True):
            if not rtsp_url.strip():
                st.error("Enter the RTSP/HTTP camera URL first.")
            else:
                start_camera(rtsp_url.strip())
                time.sleep(0.5)
                safe_rerun()
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Camera", "ONLINE")
        c2.metric("Scan", "ACTIVE" if scan_active else "READY")
        c3.metric("Detected", len(CAMERA.get("detected_ids", [])))

        if st.button("Stop Camera", use_container_width=True):
            stop_camera()
            safe_rerun()

        st.caption(f"Watching: {CAMERA.get('url', '')}")

        # A Streamlit fragment provides a responsive live display on modern
        # Streamlit versions while the recognition thread keeps consuming frames.
        fragment = getattr(st, "fragment", None)
        if fragment is not None:
            @fragment(run_every="0.5s")
            def live_panel():
                with CAMERA_LOCK:
                    frame = CAMERA.get("latest_frame")
                    active = CAMERA.get("scan_active")
                    period = CAMERA.get("current_period")
                    recent_now = list(CAMERA.get("recent", []))
                    report_now = CAMERA.get("last_report")
                    err = CAMERA.get("error")
                if err:
                    st.error(err)
                if frame is not None:
                    st.image(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), channels="RGB", use_container_width=True)
                else:
                    st.info("Waiting for the first camera frame…")
                if active and period:
                    st.markdown(f"**CURRENT SCAN:** {period.get('label', 'Scan')}  ·  {period.get('scan_duration_sec', '?')} sec")
                else:
                    status = db.get_period_status()
                    nxt = status.get("next")
                    if nxt:
                        st.caption(f"Next scheduled scan: {nxt['label']} at {nxt['scan_time']}")
                    else:
                        st.caption("Camera is ready. No active scheduled scan.")
                if recent_now:
                    st.dataframe(recent_now, use_container_width=True, hide_index=True)
                if report_now:
                    st.success(
                        f"Last report: {report_now['period']['label']} · "
                        f"present {len(report_now['present'])} · absent {len(report_now['absent'])}"
                    )
            live_panel()
        else:
            with CAMERA_LOCK:
                frame = CAMERA.get("latest_frame")
                err = CAMERA.get("error")
            if err:
                st.error(err)
            if frame is not None:
                st.image(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), channels="RGB", use_container_width=True)
            st.info("Your Streamlit version does not expose st.fragment(); use Refresh to update this view.")
            if st.button("Refresh Live View"):
                safe_rerun()

    st.markdown("---")
    st.markdown("### TAKE ATTENDANCE NOW")
    st.caption("Starts immediately, bypasses the timetable, and creates an isolated manual attendance session.")
    duration = st.number_input("Manual scan duration (seconds)", min_value=3, max_value=120, value=10, step=1)
    if st.button("TAKE ATTENDANCE NOW", disabled=not running, use_container_width=True):
        ok, result = request_manual_scan(int(duration))
        if ok:
            st.success(f"Manual scan started for {result['duration_sec']} seconds.")
        else:
            st.error(result)

    if last_report:
        st.markdown("### Latest report")
        p = last_report["period"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Present", len(last_report["present"]))
        c2.metric("Absent", len(last_report["absent"]))
        total = len(last_report["present"]) + len(last_report["absent"])
        c3.metric("Attendance %", f"{(len(last_report['present']) / total * 100):.1f}%" if total else "0%")
        st.write(f"**{p['label']}** · completed {last_report['completed_at']}")
        tab1, tab2 = st.tabs(["Present", "Absent"])
        with tab1:
            st.dataframe(last_report["present"], use_container_width=True, hide_index=True)
        with tab2:
            st.dataframe(last_report["absent"], use_container_width=True, hide_index=True)


# =====================================================================
# TEACHER: CLASS SCHEDULE
# =====================================================================

elif page == "Class Schedule":
    dot_header("Class Schedule", "Configure periods and automatic attendance scans")

    st.markdown("### ADD CLASS PERIOD")
    with st.form("add_period_form"):
        c1, c2 = st.columns(2)
        with c1:
            label = st.text_input("Period / Subject", placeholder="Data Structures")
            start_time = st.time_input("Class starts", value=datetime.strptime("09:00", "%H:%M").time())
            end_time = st.time_input("Class ends", value=datetime.strptime("09:50", "%H:%M").time())
        with c2:
            scan_time = st.time_input("Automatic scan", value=datetime.strptime("09:15", "%H:%M").time())
            duration = st.number_input("Scan duration (seconds)", min_value=1, max_value=600, value=30, step=1)
        add = st.form_submit_button("+ ADD CLASS PERIOD", use_container_width=True)

    if add:
        try:
            pid = db.add_period(
                label.strip(),
                start_time.strftime("%H:%M"),
                end_time.strftime("%H:%M"),
                scan_time.strftime("%H:%M"),
                int(duration),
            )
            st.success(f"Period added successfully (ID {pid}).")
            safe_rerun()
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"Could not add period: {exc}")

    st.markdown("### CONFIGURED PERIODS")
    periods = db.get_periods()
    if not periods:
        st.info("No class periods configured yet.")
    else:
        for p in periods:
            st.markdown(
                f"<div class='period-card'>"
                f"<div class='label'>PERIOD {p['id']}</div>"
                f"<div class='title'>{p['label']}</div>"
                f"<div class='meta'>{p['start_time']} — {p['end_time']}<br>"
                f"Automatic scan: {p['scan_time']} · Duration: {p['scan_duration_sec']} sec · "
                f"Status: {'ENABLED' if p['enabled'] else 'DISABLED'}</div></div>",
                unsafe_allow_html=True,
            )
            e1, e2, e3, e4 = st.columns([2, 1, 1, 1])
            with e1:
                with st.expander(f"Edit Period {p['id']}"):
                    with st.form(f"edit_period_{p['id']}"):
                        nl = st.text_input("Label", value=p["label"])
                        ns = st.text_input("Start HH:MM", value=p["start_time"])
                        ne = st.text_input("End HH:MM", value=p["end_time"])
                        nt = st.text_input("Scan HH:MM", value=p["scan_time"])
                        nd = st.number_input("Duration", min_value=1, max_value=600, value=int(p["scan_duration_sec"]))
                        en = st.checkbox("Enabled", value=bool(p["enabled"]))
                        save = st.form_submit_button("SAVE PERIOD")
                    if save:
                        try:
                            db.update_period(p["id"], nl.strip(), ns.strip(), ne.strip(), nt.strip(), int(nd), en)
                            st.success("Period updated.")
                            safe_rerun()
                        except ValueError as exc:
                            st.error(str(exc))
                        except Exception as exc:
                            st.error(f"Could not update period: {exc}")
            with e2:
                if st.button("Delete", key=f"delete_period_{p['id']}"):
                    db.delete_period(p["id"])
                    st.success("Period deleted. Historical attendance remains in the attendance table.")
                    safe_rerun()


# =====================================================================
# TEACHER: PERIOD REPORTS
# =====================================================================

elif page == "Period Reports":
    dot_header("Period Reports", "Present / absent breakdown by class session")
    periods = db.get_periods()
    if not periods:
        st.info("Create a class period first.")
    else:
        selected_period_label = st.selectbox("Class period", [f"{p['id']} — {p['label']}" for p in periods])
        selected_period = periods[[f"{p['id']} — {p['label']}" for p in periods].index(selected_period_label)]
        report_date = st.date_input("Report date", value=date.today())
        date_str = report_date.strftime("%Y-%m-%d")
        present = db.get_period_attendance(selected_period["id"], date_str)
        absent = db.get_period_absentees(selected_period["id"], date_str)
        total = len(present) + len(absent)
        pct = (len(present) / total * 100) if total else 0

        c1, c2, c3 = st.columns(3)
        c1.metric("Present", len(present))
        c2.metric("Absent", len(absent))
        c3.metric("Attendance %", f"{pct:.1f}%")
        st.write(f"**{selected_period['label']}** · {date_str} · scan {selected_period['scan_time']}")

        tab1, tab2 = st.tabs(["PRESENT", "ABSENT"])
        with tab1:
            if present:
                st.dataframe(present, use_container_width=True, hide_index=True)
            else:
                st.info("No students marked present for this period.")
        with tab2:
            if absent:
                st.dataframe(absent, use_container_width=True, hide_index=True)
            else:
                st.success("No absentees for this period.")


# =====================================================================
# TEACHER: EXPORT
# =====================================================================

elif page == "Export":
    dot_header("Download Excel", "Export attendance records")
    rows = db.get_attendance_log(limit=10000)
    if not rows:
        st.info("No attendance records to export yet.")
    else:
        # Build the workbook directly with openpyxl. Pandas is intentionally not
        # used because Windows Application Control on this machine blocks
        # pandas' compiled DLLs.
        buffer = io.BytesIO()
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Attendance"
        headers = list(rows[0].keys())
        sheet.append(headers)
        for row in rows:
            sheet.append([row.get(h) for h in headers])
        workbook.save(buffer)
        buffer.seek(0)
        st.download_button(
            "DOWNLOAD attendance.xlsx",
            data=buffer.getvalue(),
            file_name="attendance.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.dataframe(df, use_container_width=True, hide_index=True)
