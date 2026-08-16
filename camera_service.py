"""
camera_service.py — single-process camera + face recognition + attendance service.

Purpose:
- One file only. No recognition_worker.py.
- Streamlit talks to this service on 127.0.0.1:8777.
- Supports:
    GET  /status
    GET  /mjpeg
    POST /start
    POST /stop
    POST /manual
    POST /encode
- Camera transport tries OpenCV FFmpeg, OpenCV default, then external ffmpeg.
- Face recognition happens in this same process.
- Recognized students are written directly to db.py attendance.
- Liveness is intentionally not required for the "Take Attendance Now" flow.
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import cv2
import numpy as np
import db

try:
    import face_recognition
except Exception as exc:
    face_recognition = None
    FACE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    FACE_IMPORT_ERROR = None


HOST = "127.0.0.1"
PORT = int(os.environ.get("ATTENDANCE_CAMERA_PORT", "8777"))
DEFAULT_CAMERA_URL = os.environ.get(
    "ATTENDANCE_CAMERA_URL",
    "rtsp://192.168.1.113:8080/h264_pcm.sdp",
)

# Recognition settings
MATCH_TOLERANCE = 0.60
RECOGNITION_INTERVAL = 0.45
MAX_RECOGNITION_WIDTH = 960

# Display
DISPLAY_WIDTH = 960
JPEG_QUALITY = 70
DETECTION_HOLD_SEC = 2.5

# Camera
RECONNECT_DELAY = 0.8
FRAME_READ_TIMEOUT = 4.0


class ExternalFFmpegReader:
    """Optional RTSP fallback using the ffmpeg executable."""

    def __init__(self, url: str):
        self.url = url
        self.proc: subprocess.Popen | None = None
        self.latest: np.ndarray | None = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = False

        self._start()

    def _start(self) -> None:
        for transport in ("udp", "tcp"):
            try:
                cmd = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-rtsp_transport", transport,
                    "-rw_timeout", "8000000",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-analyzeduration", "1000000",
                    "-probesize", "1000000",
                    "-i", self.url,
                    "-an",
                    "-f", "mjpeg",
                    "-q:v", "5",
                    "pipe:1",
                ]

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    bufsize=0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

                # Drain stderr so ffmpeg cannot block on a full Windows pipe.
                threading.Thread(
                    target=self._drain_stderr,
                    args=(proc,),
                    daemon=True,
                    name="ffmpeg-stderr-drain",
                ).start()

                self.proc = proc
                self.latest = None
                self.stop_event.clear()

                self.thread = threading.Thread(
                    target=self._reader,
                    daemon=True,
                    name="ffmpeg-frame-reader",
                )
                self.thread.start()

                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    with self.lock:
                        if self.latest is not None:
                            self.started = True
                            return
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)

                self.release()
            except (FileNotFoundError, OSError):
                self.release()
                return
            except Exception:
                self.release()

    @staticmethod
    def _drain_stderr(proc: subprocess.Popen) -> None:
        try:
            while proc.poll() is None and proc.stderr is not None:
                proc.stderr.readline()
        except Exception:
            pass

    def _reader(self) -> None:
        buf = bytearray()
        proc = self.proc
        if proc is None or proc.stdout is None:
            return

        try:
            while not self.stop_event.is_set() and proc.poll() is None:
                chunk = proc.stdout.read(32768)
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        if len(buf) > 2 * 1024 * 1024:
                            del buf[:-2]
                        break

                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break

                    jpg = bytes(buf[start:end + 2])
                    del buf[:end + 2]

                    arr = np.frombuffer(jpg, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None and frame.size:
                        with self.lock:
                            self.latest = frame
        except Exception:
            pass

    def is_opened(self) -> bool:
        return bool(
            self.started
            and self.proc is not None
            and self.proc.poll() is None
        )

    def read(self):
        with self.lock:
            if self.latest is None:
                return False, None
            return True, self.latest.copy()

    def release(self) -> None:
        self.stop_event.set()
        proc = self.proc
        self.proc = None
        self.started = False
        self.latest = None

        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass


class CameraState:
    def __init__(self):
        self.lock = threading.RLock()

        self.running = False
        self.url = ""
        self.error = None
        self.fps = 0.0

        self.latest: np.ndarray | None = None
        self.jpeg: bytes | None = None
        self.version = 0

        self.detected: list[dict] = []
        self.recent: list[dict] = []

        self.scan_active = False
        self.scan_mode = None
        self.period = None
        self.last_report = None
        self.manual_request = None

        self.stop_event = threading.Event()
        self.capture_thread: threading.Thread | None = None
        self.scan_thread: threading.Thread | None = None
        self.cap = None

        self.recognition_alive = False
        self.recognition_errors = 0
        self.last_recognition = 0.0
        self.detected_until = 0.0

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "url": self.url,
                "error": self.error,
                "fps": round(self.fps, 1),
                "detected": list(self.detected),
                "recent": list(self.recent),
                "scan_active": self.scan_active,
                "scan_mode": self.scan_mode,
                "period": self.period,
                "last_report": self.last_report,
                "recognition_alive": bool(self.recognition_alive),
                "recognition_errors": self.recognition_errors,
                "last_recognition": self.last_recognition,
                "face_engine": (
                    "face_recognition" if face_recognition is not None else "unavailable"
                ),
                "face_engine_error": FACE_IMPORT_ERROR,
            }

    # ------------------------------------------------------------------
    # camera
    # ------------------------------------------------------------------
    def start_camera(self, url: str) -> bool:
        self.stop_camera()

        url = (url or DEFAULT_CAMERA_URL).strip()
        if not url:
            return False

        with self.lock:
            self.url = url
            self.error = "Connecting to camera..."
            self.running = False
            self.fps = 0.0
            self.latest = None
            self.jpeg = None
            self.version = 0
            self.detected = []
            self.recent = []
            self.last_report = None
            self.detected_until = 0.0
            self.stop_event.clear()

        self.capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="attendance-camera-capture",
        )
        self.scan_thread = threading.Thread(
            target=self._scan_loop,
            daemon=True,
            name="attendance-face-recognition",
        )
        self.capture_thread.start()
        self.scan_thread.start()

        return True

    def stop_camera(self) -> None:
        self.stop_event.set()

        with self.lock:
            self.running = False
            self.scan_active = False
            self.scan_mode = None
            self.manual_request = None
            cap = self.cap
            self.cap = None
            self.recognition_alive = False

        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _open_capture(self):
        url = self.url or DEFAULT_CAMERA_URL

        # 1) OpenCV FFmpeg: path that previously produced real frames.
        for transport in ("tcp", "udp"):
            old_opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
            try:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                    f"rtsp_transport;{transport}"
                )
                cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                try:
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                except Exception:
                    pass

                if cap.isOpened():
                    # Require an actual frame before accepting the connection.
                    deadline = time.monotonic() + FRAME_READ_TIMEOUT
                    while time.monotonic() < deadline:
                        ok, frame = cap.read()
                        if ok and frame is not None and frame.size:
                            return cap, frame
                        time.sleep(0.03)

                cap.release()
            except Exception:
                pass
            finally:
                if old_opts is None:
                    os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
                else:
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = old_opts

        # 2) OpenCV default backend.
        try:
            cap = cv2.VideoCapture(url, cv2.CAP_ANY)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

            if cap.isOpened():
                deadline = time.monotonic() + FRAME_READ_TIMEOUT
                while time.monotonic() < deadline:
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size:
                        return cap, frame
                    time.sleep(0.03)
            cap.release()
        except Exception:
            pass

        # 3) External ffmpeg fallback.
        try:
            ff = ExternalFFmpegReader(url)
            if ff.is_opened():
                ok, frame = ff.read()
                if ok and frame is not None:
                    return ff, frame
            ff.release()
        except Exception:
            pass

        return None, None

    def _capture_loop(self) -> None:
        frame_count = 0
        fps_start = time.monotonic()
        cap = None

        while not self.stop_event.is_set():
            try:
                if cap is None:
                    cap, frame = self._open_capture()

                    if cap is None or frame is None:
                        with self.lock:
                            self.running = False
                            self.fps = 0.0
                            self.error = (
                                "Camera connection failed — retrying..."
                            )
                            self.cap = None
                        time.sleep(RECONNECT_DELAY)
                        continue

                    with self.lock:
                        self.cap = cap
                        self.running = True
                        self.error = None
                        self.recognition_alive = face_recognition is not None

                    # First validated frame.
                    self._publish_frame(frame)
                    frame_count = 1
                    fps_start = time.monotonic()
                    continue

                ok, frame = cap.read()
                if not ok or frame is None or not frame.size:
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None

                    with self.lock:
                        self.running = False
                        self.fps = 0.0
                        self.cap = None
                        self.error = "Camera frame lost — reconnecting..."

                    time.sleep(RECONNECT_DELAY)
                    continue

                self._publish_frame(frame)
                frame_count += 1

                elapsed = time.monotonic() - fps_start
                if elapsed >= 1.0:
                    with self.lock:
                        self.fps = frame_count / elapsed
                    frame_count = 0
                    fps_start = time.monotonic()

            except Exception as exc:
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass

                cap = None

                with self.lock:
                    self.running = False
                    self.fps = 0.0
                    self.cap = None
                    self.error = f"Camera recovered: {type(exc).__name__}: {exc}"

                time.sleep(RECONNECT_DELAY)

        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass

        with self.lock:
            self.running = False
            self.cap = None

    def _publish_frame(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]

        if w > DISPLAY_WIDTH:
            scale = DISPLAY_WIDTH / float(w)
            display = cv2.resize(
                frame,
                (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )
        else:
            display = frame.copy()
            scale = 1.0

        # Draw a clean face box and name when a match exists.
        with self.lock:
            matches = list(self.detected)

        for item in matches:
            loc = item.get("location")
            if not loc:
                continue

            top, right, bottom, left = loc
            top = int(top * scale)
            right = int(right * scale)
            bottom = int(bottom * scale)
            left = int(left * scale)

            cv2.rectangle(
                display,
                (left, top),
                (right, bottom),
                (0, 40, 255),
                2,
            )

            label = (
                f"{item.get('name', 'Unknown')}  "
                f"{item.get('confidence', 0.0):.0%}"
            )
            cv2.rectangle(
                display,
                (left, max(0, top - 28)),
                (right, top),
                (0, 40, 255),
                -1,
            )
            cv2.putText(
                display,
                label,
                (left + 6, max(18, top - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        ok, encoded = cv2.imencode(
            ".jpg",
            display,
            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
        )

        if ok:
            with self.lock:
                self.latest = frame
                self.jpeg = encoded.tobytes()
                self.version += 1

    # ------------------------------------------------------------------
    # recognition
    # ------------------------------------------------------------------
    def _recognize(self, frame: np.ndarray, students: list[tuple]) -> list[dict]:
        if face_recognition is None:
            self.recognition_alive = False
            return []

        if frame is None or not students:
            return []

        self.recognition_alive = True

        h, w = frame.shape[:2]
        scale = min(
            1.0,
            MAX_RECOGNITION_WIDTH / float(max(1, w)),
        )

        work = (
            cv2.resize(
                frame,
                (int(w * scale), int(h * scale)),
                interpolation=cv2.INTER_AREA,
            )
            if scale < 1.0
            else frame
        )

        rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)

        # Stronger detector, still fast enough for a live classroom feed.
        locations = face_recognition.face_locations(
            rgb,
            model="hog",
            number_of_times_to_upsample=1,
        )

        if not locations:
            return []

        encodings = face_recognition.face_encodings(
            rgb,
            locations,
            num_jitters=1,
            model="small",
        )

        if not encodings:
            return []

        valid = []
        for student in students:
            try:
                enc = np.asarray(student[2], dtype=np.float64).reshape(-1)
                if enc.size == 128:
                    valid.append(
                        (student[0], student[1], enc)
                    )
            except Exception:
                continue

        if not valid:
            return []

        known = np.asarray([x[2] for x in valid], dtype=np.float64)

        results = []

        for loc, encoding in zip(locations, encodings):
            distances = face_recognition.face_distance(
                known,
                encoding,
            )

            if len(distances) == 0:
                continue

            idx = int(np.argmin(distances))
            distance = float(distances[idx])

            if distance > MATCH_TOLERANCE:
                continue

            top, right, bottom, left = loc

            if scale < 1.0:
                inv = 1.0 / scale
                top = int(top * inv)
                right = int(right * inv)
                bottom = int(bottom * inv)
                left = int(left * inv)

            sid, name, _ = valid[idx]

            results.append(
                {
                    "id": sid,
                    "name": name,
                    "confidence": round(max(0.0, 1.0 - distance), 3),
                    "distance": round(distance, 4),
                    "location": (top, right, bottom, left),
                }
            )

        return results

    # ------------------------------------------------------------------
    # scanning / attendance
    # ------------------------------------------------------------------
    def manual_scan(self, seconds: int = 10):
        with self.lock:
            if not self.running:
                return False, "Start the camera first."

            if self.scan_active or self.manual_request:
                return False, "A scan is already running."

            now = datetime.now()

            self.manual_request = {
                "session_key": (
                    "manual-"
                    + now.strftime("%Y%m%d-%H%M%S-")
                    + uuid.uuid4().hex[:8]
                ),
                "session_date": now.strftime("%Y-%m-%d"),
                "label": f"Manual Scan {now.strftime('%H:%M:%S')}",
                "started_at": now.strftime("%H:%M:%S"),
                "duration_sec": max(3, min(int(seconds), 120)),
            }

            self.last_report = None
            self.detected = []
            self.recent = []
            self.detected_until = 0.0

            return True, dict(self.manual_request)

    def _scan_loop(self):
        students = []
        last_refresh = 0.0
        last_rec = 0.0

        active_mode = None
        active_session = None
        active_period = None
        active_end = None
        logged = set()

        while not self.stop_event.is_set():
            try:
                with self.lock:
                    running = self.running
                    request = (
                        dict(self.manual_request)
                        if self.manual_request
                        else None
                    )

                if not running:
                    time.sleep(0.10)
                    continue

                now = time.time()

                # Refresh enrollment every 2 seconds so a newly enrolled
                # student becomes recognizable without restarting the service.
                if now - last_refresh >= 2.0:
                    students = db.get_all_students()
                    last_refresh = now

                if active_mode is None and request:
                    active_mode = "manual"
                    active_session = request
                    active_period = None
                    active_end = now + request["duration_sec"]
                    logged = set()

                    with self.lock:
                        self.manual_request = None
                        self.scan_active = True
                        self.scan_mode = "manual"
                        self.period = dict(request)
                        self.detected = []
                        self.recent = []
                        self.last_report = None

                if active_mode is None:
                    # Automatic scheduled periods.
                    try:
                        status = db.get_period_status(datetime.now())
                    except Exception:
                        status = {"scan": None}

                    period = status.get("scan")

                    if period:
                        active_mode = "scheduled"
                        active_period = dict(period)
                        active_session = None
                        active_end = now + int(
                            period["scan_duration_sec"]
                        )
                        logged = set()

                        with self.lock:
                            self.scan_active = True
                            self.scan_mode = "scheduled"
                            self.period = dict(period)
                            self.detected = []
                            self.recent = []
                            self.last_report = None

                if active_mode is not None and now >= active_end:
                    self._finish_scan(
                        active_mode,
                        active_period,
                        active_session,
                    )
                    active_mode = None
                    active_session = None
                    active_period = None
                    active_end = None
                    logged = set()
                    continue

                if (
                    students
                    and self.latest is not None
                    and now - last_rec >= RECOGNITION_INTERVAL
                ):
                    with self.lock:
                        frame = (
                            self.latest.copy()
                            if self.latest is not None
                            else None
                        )

                    if frame is not None:
                        last_rec = now

                        results = self._recognize(
                            frame,
                            students,
                        )

                        with self.lock:
                            if results:
                                self.detected = list(results)
                                self.last_recognition = time.time()
                                self.detected_until = (
                                    self.last_recognition
                                    + DETECTION_HOLD_SEC
                                )
                            elif time.time() >= self.detected_until:
                                self.detected = []

                        # Attendance is intentionally direct:
                        # recognized face -> present record.
                        if active_mode is not None:
                            session_date = datetime.now().strftime(
                                "%Y-%m-%d"
                            )

                            for result in results:
                                sid = result["id"]

                                if sid in logged:
                                    continue

                                try:
                                    if active_mode == "scheduled":
                                        already = (
                                            db.already_logged_in_period(
                                                sid,
                                                active_period["id"],
                                                session_date,
                                            )
                                        )
                                    else:
                                        already = (
                                            db.already_logged_in_session(
                                                sid,
                                                active_session["session_key"],
                                            )
                                        )
                                except Exception:
                                    already = False

                                if already:
                                    logged.add(sid)
                                    continue

                                if active_mode == "scheduled":
                                    db.log_attendance(
                                        sid,
                                        result["name"],
                                        result["confidence"],
                                        period_id=active_period["id"],
                                        session_date=session_date,
                                        period_label=active_period["label"],
                                        session_key=(
                                            f"{session_date}:period:"
                                            f"{active_period['id']}"
                                        ),
                                        session_type="scheduled",
                                    )
                                else:
                                    db.log_attendance(
                                        sid,
                                        result["name"],
                                        result["confidence"],
                                        session_date=session_date,
                                        period_label=active_session["label"],
                                        session_key=active_session["session_key"],
                                        session_type="manual",
                                    )

                                logged.add(sid)

                                with self.lock:
                                    self.recent.insert(
                                        0,
                                        {
                                            "name": result["name"],
                                            "id": sid,
                                            "time": datetime.now().strftime(
                                                "%H:%M:%S"
                                            ),
                                            "confidence": result["confidence"],
                                        },
                                    )
                                    self.recent = self.recent[:30]

            except Exception as exc:
                with self.lock:
                    self.recognition_errors += 1
                    self.error = (
                        f"Recognition recovered: "
                        f"{type(exc).__name__}: {exc}"
                    )
                time.sleep(0.15)

            time.sleep(0.01)

    def _finish_scan(self, mode, period, session):
        date_str = datetime.now().strftime("%Y-%m-%d")

        try:
            if mode == "scheduled":
                present = db.get_period_attendance(
                    period["id"],
                    date_str,
                )
                absent = db.get_period_absentees(
                    period["id"],
                    date_str,
                )
            else:
                present = db.get_session_attendance(
                    session["session_key"],
                )
                absent = db.get_session_absentees(
                    session["session_key"],
                )
        except Exception:
            present = []
            absent = []

        report = {
            "type": mode,
            "date": date_str,
            "present": present,
            "absent": absent,
            "period": period or {"label": session["label"]},
            "completed_at": datetime.now().strftime("%H:%M:%S"),
        }

        with self.lock:
            self.last_report = report
            self.scan_active = False
            self.scan_mode = None
            self.period = None
            # Keep detection visible briefly after report completion.
            self.detected_until = time.time() + DETECTION_HOLD_SEC

        return report


STATE = CameraState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/status":
            self._json(200, STATE.snapshot())
            return

        if path == "/mjpeg":
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "multipart/x-mixed-replace; boundary=frame",
            )
            self.send_header(
                "Cache-Control",
                "no-cache, no-store, must-revalidate",
            )
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            last_version = -1

            try:
                while not STATE.stop_event.is_set():
                    with STATE.lock:
                        data = STATE.jpeg
                        version = STATE.version
                        running = STATE.running

                    if data is not None and version != last_version:
                        self.wfile.write(
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            + b"Content-Length: "
                            + str(len(data)).encode()
                            + b"\r\n\r\n"
                            + data
                            + b"\r\n"
                        )
                        self.wfile.flush()
                        last_version = version

                    elif not running:
                        time.sleep(0.08)
                    else:
                        time.sleep(0.005)

            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                ConnectionError,
            ):
                pass

            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        length = int(
            self.headers.get("Content-Length", "0")
        )

        raw = self.rfile.read(length) if length else b"{}"

        try:
            payload = json.loads(
                raw.decode("utf-8") or "{}"
            )
        except Exception:
            payload = {}

        if path == "/start":
            url = (
                payload.get("url")
                or DEFAULT_CAMERA_URL
            ).strip()

            ok = STATE.start_camera(url)

            self._json(
                200,
                {
                    "ok": ok,
                    "url": url,
                },
            )
            return

        if path == "/stop":
            STATE.stop_camera()
            self._json(200, {"ok": True})
            return

        if path == "/manual":
            seconds = payload.get("seconds", 10)
            try:
                seconds = int(seconds)
            except Exception:
                seconds = 10

            ok, result = STATE.manual_scan(seconds)

            self._json(
                200,
                {
                    "ok": ok,
                    "result": result,
                },
            )
            return

        if path == "/encode":
            try:
                data = base64.b64decode(
                    payload.get("jpeg", "")
                )
                arr = np.frombuffer(
                    data,
                    dtype=np.uint8,
                )
                frame = cv2.imdecode(
                    arr,
                    cv2.IMREAD_COLOR,
                )

                if face_recognition is None:
                    raise RuntimeError(
                        FACE_IMPORT_ERROR
                        or "face_recognition unavailable"
                    )

                locations = face_recognition.face_locations(
                    cv2.cvtColor(
                        frame,
                        cv2.COLOR_BGR2RGB,
                    ),
                    model="hog",
                    number_of_times_to_upsample=1,
                )

                if len(locations) != 1:
                    result = {
                        "count": len(locations),
                        "encoding": None,
                    }
                else:
                    encodings = face_recognition.face_encodings(
                        cv2.cvtColor(
                            frame,
                            cv2.COLOR_BGR2RGB,
                        ),
                        locations,
                        num_jitters=1,
                        model="small",
                    )
                    result = {
                        "count": 1,
                        "encoding": (
                            encodings[0].tolist()
                            if encodings
                            else None
                        ),
                    }

                self._json(
                    200,
                    {
                        "ok": True,
                        "result": result,
                    },
                )

            except Exception as exc:
                self._json(
                    200,
                    {
                        "ok": False,
                        "error": (
                            f"{type(exc).__name__}: {exc}"
                        ),
                    },
                )
            return

        self._json(404, {"error": "not found"})


def main():
    db.init_db()

    server = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    print(
        f"CAMERA_SERVICE_READY {HOST}:{PORT}",
        flush=True,
    )

    try:
        server.serve_forever(
            poll_interval=0.2
        )
    finally:
        STATE.stop_camera()
        server.server_close()


if __name__ == "__main__":
    main()
