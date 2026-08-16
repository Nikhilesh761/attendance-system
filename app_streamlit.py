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

import html
import io
import json
import base64
import subprocess
import sys
import threading
import time
import socket
import os
import multiprocessing as mp
import queue as pyqueue
from pathlib import Path
from datetime import datetime, date

import cv2
import numpy as np
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image
from openpyxl import Workbook
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
    .table-title { color:#aaa; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin:10px 0 6px; }
    .native-table-wrap { width:100%; overflow-x:auto; border:1px solid #292929; border-radius:3px; margin:8px 0 18px; }
    .native-table { width:100%; min-width:720px; border-collapse:collapse; font-size:11px; background:#0b0b0b; }
    .native-table th { text-align:left; color:#ff1e1e; background:#111; border-bottom:1px solid #333; padding:9px 10px; white-space:nowrap; text-transform:uppercase; letter-spacing:.7px; }
    .native-table td { color:#ddd; border-bottom:1px solid #202020; padding:8px 10px; vertical-align:top; white-space:nowrap; }
    .native-table tr:last-child td { border-bottom:0; }
    .native-table tbody tr:hover td { background:#151515; }
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



def render_table(rows, title=None, max_rows=5000):
    """Render attendance records without Streamlit dataframe APIs.

    Streamlit dataframe rendering can import pandas internally. This machine has
    Windows Application Control blocking pandas native DLLs, so tables are
    rendered as escaped HTML instead.
    """
    if not rows:
        if title:
            st.markdown(f"**{html.escape(str(title))}**")
        st.info("No records to display.")
        return
    rows = list(rows)[:max_rows]
    if not isinstance(rows[0], dict):
        rows = [{"Value": r} for r in rows]
    columns, seen = [], set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, float):
            return f"{value:.3f}" if value != int(value) else str(int(value))
        return html.escape(str(value)).replace("\n", "<br>")
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell(row.get(c))}</td>" for c in columns) + "</tr>"
        for row in rows
    )
    heading = f"<div class='table-title'>{html.escape(str(title))}</div>" if title else ""
    st.markdown(
        heading + "<div class='native-table-wrap'><table class='native-table'><thead><tr>"
        + head + "</tr></thead><tbody>" + body + "</tbody></table></div>",
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
# STANDALONE CAMERA SERVICE CLIENT
# =====================================================================

CAMERA_PORT = 8777
CAMERA_BASE = f"http://127.0.0.1:{CAMERA_PORT}"

class _CameraServiceClient:
    """Streamlit talks to a completely separate camera service process.

    This is intentionally NOT a thread/multiprocessing target inside this
    Streamlit file. Windows spawn can re-import Streamlit and destabilize the
    server. The supervisor + service are ordinary subprocesses instead.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.supervisor = None
        self.service_path = str(Path(__file__).with_name("camera_service.py"))

    def _request(self, method, path, payload=None, timeout=1.5):
        import urllib.request
        import urllib.error
        data = None
        headers = {"Cache-Control": "no-cache"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(CAMERA_BASE + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def status(self):
        try:
            return self._request("GET", "/status", timeout=0.7)
        except Exception:
            return {"running": False, "error": "Camera service is restarting...", "detected": [], "recent": []}

    def ensure(self):
        with self.lock:
            try:
                self._request("GET", "/status", timeout=0.4)
                return True
            except Exception:
                pass
            if self.supervisor is None or self.supervisor.poll() is not None:
                if not os.path.exists(self.service_path):
                    return False
                self.supervisor = subprocess.Popen(
                    [sys.executable, self.service_path],
                    cwd=str(Path(__file__).parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                try:
                    self._request("GET", "/status", timeout=0.4)
                    return True
                except Exception:
                    time.sleep(0.08)
            return False

    def start(self, url):
        if not self.ensure():
            return False
        try:
            return bool(self._request("POST", "/start", {"url": url}, timeout=2.0).get("ok"))
        except Exception:
            return False

    def stop(self):
        try:
            self._request("POST", "/stop", {}, timeout=1.0)
        except Exception:
            pass

    def manual(self, seconds):
        if not self.ensure():
            return False, "Camera service is unavailable."
        try:
            r = self._request("POST", "/manual", {"seconds": int(seconds)}, timeout=1.5)
            return bool(r.get("ok")), r.get("result") or r.get("error", "Unable to start scan")
        except Exception as exc:
            return False, f"Camera service unavailable: {exc}"

    def encode(self, jpeg_bytes):
        if not self.ensure():
            raise RuntimeError("Camera service is unavailable.")
        payload = {"jpeg": base64.b64encode(jpeg_bytes).decode("ascii")}
        r = self._request("POST", "/encode", payload, timeout=10.0)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "Face encoding failed"))
        return r.get("result") or {}

@st.cache_resource(show_spinner=False)
def get_camera_service():
    return _CameraServiceClient()

CAMERA_SERVICE = get_camera_service()

# Keep a lightweight status snapshot for the rest of the dashboard. No
# Streamlit fragment/run_every is used because this Windows environment has a
# blocked pandas native DLL; the live camera itself is updated continuously by
# the browser-native MJPEG stream.
CAMERA = CAMERA_SERVICE.status()


def start_camera(url):
    return CAMERA_SERVICE.start(url)


def stop_camera():
    CAMERA_SERVICE.stop()


def request_manual_scan(duration_sec):
    return CAMERA_SERVICE.manual(duration_sec)

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
        render_table(my_rows)
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
        render_table(display_rows)
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
        render_table(filtered_rows)
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
        render_table(data)
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
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=90)
                result = CAMERA_SERVICE.encode(buf.getvalue())
                count = int((result or {}).get("count", 0)) if isinstance(result, dict) else 0
                encoding = (result or {}).get("encoding") if isinstance(result, dict) else None
                if count != 1 or not encoding:
                    st.error(f"Found {count} face(s). Enrollment requires exactly one clear face.")
                else:
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
        render_table(students)
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
    dot_header("Live Camera Scan", "SMOOTH LIVE FEED · ISOLATED CAMERA SERVICE · RECOGNITION IN BACKGROUND")

    CAMERA = CAMERA_SERVICE.status()
    running = bool(CAMERA.get("running"))
    url = CAMERA.get("url", "")

    if not running:
        if CAMERA.get("error"):
            st.info(CAMERA.get("error"))
        rtsp_url = st.text_input(
            "Camera stream URL",
            value=url,
            placeholder="http://192.168.1.113:8080/h264_pcm.sdp",
        )
        if st.button("START CAMERA", use_container_width=True):
            if not rtsp_url.strip():
                st.error("Enter the camera stream URL first.")
            elif start_camera(rtsp_url.strip()):
                time.sleep(0.4)
                safe_rerun()
            else:
                st.error("Could not start the isolated camera service. Check the terminal for the service process.")
    else:
        stream_url = f"{CAMERA_BASE}/mjpeg"
        status_url = f"{CAMERA_BASE}/status"
        # One browser component owns the live feed. It never gets replaced by
        # Streamlit reruns, and it polls the service directly for status.
        components.html(
            f"""
            <style>
              *{{box-sizing:border-box}} body{{margin:0;background:#050505;color:#eee;font-family:monospace}}
              .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px}}
              .card{{background:#111;border:1px solid #2b2b2b;border-radius:3px;padding:16px 18px}}
              .label{{font-size:12px;color:#ddd;text-transform:uppercase}}
              .value{{font-size:38px;color:#ff1e1e;margin-top:8px;letter-spacing:1px}}
              .feed{{width:100%;min-height:420px;background:#000;border:1px solid #292929;border-radius:4px;display:flex;align-items:center;justify-content:center;overflow:hidden}}
              img{{display:block;width:100%;height:auto;max-height:720px;object-fit:contain;background:#000}}
              .bar{{color:#777;font-size:11px;margin:8px 0 12px}}
              .live{{color:#ff1e1e}}
              @media(max-width:800px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
            </style>
            <div class="grid">
              <div class="card"><div class="label">Camera</div><div class="value" id="cam">ONLINE</div></div>
              <div class="card"><div class="label">Live FPS</div><div class="value" id="fps">0</div></div>
              <div class="card"><div class="label">Scan</div><div class="value" id="scan">READY</div></div>
              <div class="card"><div class="label">Detected</div><div class="value" id="det">0</div></div>
            </div>
            <div class="bar"><span class="live">●</span> LIVE FEED · isolated service · recognition runs outside Streamlit</div>
            <div class="feed"><img id="feed" src="{stream_url}?t={time.time()}" alt="Live camera feed"></div>
            <div class="bar" id="status">Watching: {html.escape(url)}</div>
            <div class="bar" id="recognition">LIVE RECOGNITION · scanning for enrolled faces…</div>
            <script>
              const img=document.getElementById('feed');
              const base='{stream_url}';
              let retry=0;
              img.onerror=()=>{{ retry++; setTimeout(()=>{{img.src=base+'?retry='+Date.now()}}, Math.min(1500,300*retry)); }};
              img.onload=()=>{{retry=0}};
              async function poll(){{
                try{{
                  const r=await fetch('{status_url}?t='+Date.now(),{{cache:'no-store'}});
                  const s=await r.json();
                  document.getElementById('cam').textContent=s.running?'ONLINE':'RECONNECTING';
                  document.getElementById('fps').textContent=Math.round(s.fps||0);
                  document.getElementById('scan').textContent=s.scan_active?'ACTIVE':'READY';
                  document.getElementById('det').textContent=(s.detected||[]).length;
                  document.getElementById('status').textContent='Watching: '+(s.url||'');
                  const names=(s.detected||[]).map(x=>x.name+' ('+Number(x.confidence||0).toFixed(3)+')');
                  document.getElementById('recognition').textContent=names.length?'LIVE RECOGNITION · '+names.join(', '):'LIVE RECOGNITION · scanning for enrolled faces…';
                }}catch(e){{document.getElementById('cam').textContent='RECONNECTING'}}
              }}
              poll(); setInterval(poll,500);
            </script>
            """,
            height=790,
            scrolling=False,
        )

        if CAMERA.get("error"):
            st.caption(CAMERA.get("error"))

        st.markdown("### TAKE ATTENDANCE NOW")
        st.caption("Starts immediately and creates a separate attendance session.")
        duration = st.number_input("Manual scan duration (seconds)", min_value=3, max_value=120, value=10, step=1)
        if st.button("TAKE ATTENDANCE NOW", use_container_width=True):
            ok, result = request_manual_scan(int(duration))
            if ok:
                # Do NOT rerun Streamlit here. The camera service runs the
                # attendance session independently, and the browser status panel
                # polls /status continuously. A rerun here made the scan appear
                # to last only one UI refresh.
                st.session_state["attendance_scan_started_at"] = time.time()
                st.session_state["attendance_scan_duration"] = int(result["duration_sec"])
                st.success(
                    f"Attendance scan ACTIVE — scanning for {result['duration_sec']} seconds."
                )
            else:
                st.error(result)

        started_at = st.session_state.get("attendance_scan_started_at")
        scan_duration = st.session_state.get("attendance_scan_duration", 0)
        if started_at:
            elapsed = time.time() - started_at
            if elapsed < scan_duration:
                st.info(
                    f"Attendance scan is running… {max(0, scan_duration - int(elapsed))} seconds remaining. "
                    "Keep your face visible in the camera."
                )

        report = CAMERA.get("last_report")
        if report:
            st.markdown("### Latest report")
            p = report.get("period", {})
            c1,c2,c3=st.columns(3)
            c1.metric("Present",len(report.get("present",[])))
            c2.metric("Absent",len(report.get("absent",[])))
            total=len(report.get("present",[]))+len(report.get("absent",[]))
            c3.metric("Attendance %",f"{len(report.get('present',[]))/total*100:.1f}%" if total else "0%")
            st.write(f"**{p.get('label','Scan')}** · completed {report.get('completed_at','')}")

        if st.button("STOP CAMERA", use_container_width=True):
            stop_camera()
            safe_rerun()


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
                render_table(present)
            else:
                st.info("No students marked present for this period.")
        with tab2:
            if absent:
                render_table(absent)
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
        render_table(rows)