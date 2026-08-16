"""Standalone camera + attendance service.

The Streamlit process never opens OpenCV/dlib for live scanning. This service is
an independent process. If OpenCV or the recognition worker crashes, the UI
process remains alive and can restart the service.
"""
import base64
import json
import multiprocessing as mp
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

import db

PORT = int(os.environ.get("ATTENDANCE_CAMERA_PORT", "8777"))
HOST = "127.0.0.1"
MATCH_TOLERANCE = 0.50
RECOGNITION_INTERVAL = 0.70
BACKGROUND_INTERVAL = 1.00
# Keep the last successful recognition visible briefly so the DETECTED
# counter, face box, and recognition text do not flicker back to zero between
# recognition frames. The camera itself remains fully live.
DETECTION_HOLD_SEC = 2.0
MAX_WIDTH = 640
DISPLAY_WIDTH = 960
JPEG_QUALITY = 65
REQUIRE_LIVENESS = False
DEFAULT_CAMERA_URL = "rtsp://192.168.1.113:8080/h264_pcm.sdp"
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;udp|stimeout;5000000|max_delay;500000")


class RecognitionClient:
    def __init__(self):
        self.ctx = mp.get_context("spawn")
        self.in_q = None
        self.out_q = None
        self.proc = None
        self.rid = 0
        self.lock = threading.RLock()

    def start(self):
        with self.lock:
            self.stop()
            import recognition_worker
            self.in_q = self.ctx.Queue(maxsize=1)
            self.out_q = self.ctx.Queue(maxsize=2)
            self.proc = self.ctx.Process(
                target=recognition_worker.worker_main,
                args=(self.in_q, self.out_q),
                daemon=True,
                name="attendance-recognition-worker",
            )
            self.proc.start()

    def alive(self):
        return self.proc is not None and self.proc.is_alive()

    def ensure(self):
        if not self.alive():
            self.start()

    def request(self, op, frame, students=None, timeout=3.0, **kwargs):
        if frame is None:
            return [] if op == "recognize" else False
        ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return [] if op == "recognize" else False
        with self.lock:
            try:
                self.ensure()
                self.rid += 1
                rid = self.rid
                req = {"id": rid, "op": op, "jpeg": enc.tobytes()}
                if students is not None:
                    req["students"] = students
                req.update(kwargs)
                # Never allow an old request to build a queue.
                try:
                    while True:
                        self.in_q.get_nowait()
                except Exception:
                    pass
                self.in_q.put(req, timeout=0.4)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        reply = self.out_q.get(timeout=0.15)
                    except queue.Empty:
                        if not self.alive():
                            self.start()
                            return [] if op == "recognize" else False
                        continue
                    if reply.get("id") != rid:
                        continue
                    if reply.get("ok"):
                        return reply.get("result")
                    return [] if op == "recognize" else False
                if not self.alive():
                    self.start()
                return [] if op == "recognize" else False
            except Exception:
                try:
                    self.start()
                except Exception:
                    pass
                return [] if op == "recognize" else False

    def stop(self):
        proc = self.proc
        iq = self.in_q
        self.proc = None
        self.in_q = None
        self.out_q = None
        if iq is not None:
            try:
                iq.put_nowait({"op": "stop", "id": -1})
            except Exception:
                pass
        if proc is not None:
            proc.join(timeout=0.3)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=0.3)



class FFmpegCapture:
    """Reliable RTSP reader.

    FFmpeg owns the RTSP connection. Its stdout is consumed by a dedicated
    reader thread so Windows pipe buffering can never block the camera service.
    UDP is tried first, then TCP. A transport is accepted only after an actual
    JPEG frame has arrived.
    """
    def __init__(self, url):
        self.url = url
        self.proc = None
        self.latest = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.started = False
        self.transport = None
        self._start()

    def _drain_stderr(self, proc):
        try:
            while proc.poll() is None:
                proc.stderr.readline()
        except Exception:
            pass

    def _reader(self, proc):
        buf = bytearray()
        try:
            while not self.stop_event.is_set() and proc.poll() is None:
                chunk = proc.stdout.read(32768)
                if not chunk:
                    break
                buf.extend(chunk)

                while True:
                    a = buf.find(b"\xff\xd8")
                    if a < 0:
                        if len(buf) > 2 * 1024 * 1024:
                            del buf[:-2]
                        break
                    b = buf.find(b"\xff\xd9", a + 2)
                    if b < 0:
                        if a > 0:
                            del buf[:a]
                        break

                    jpg = bytes(buf[a:b + 2])
                    del buf[:b + 2]

                    arr = np.frombuffer(jpg, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None and frame.size:
                        with self.lock:
                            self.latest = frame
        except Exception:
            pass

    def _launch(self, transport):
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", transport,
            "-rw_timeout", "8000000",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "1000000",
            "-probesize", "1000000",
            "-i", self.url,
            "-an",
            "-vf", "fps=12",
            "-q:v", "5",
            "-f", "mjpeg",
            "pipe:1",
        ]

        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        threading.Thread(
            target=self._drain_stderr, args=(p,), daemon=True,
            name="ffmpeg-stderr"
        ).start()

        self.latest = None
        self.proc = p
        self.transport = transport

        self.thread = threading.Thread(
            target=self._reader, args=(p,), daemon=True,
            name="ffmpeg-reader"
        )
        self.thread.start()

        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline and not self.stop_event.is_set():
            with self.lock:
                if self.latest is not None:
                    self.started = True
                    return True
            if p.poll() is not None:
                break
            time.sleep(0.05)

        try:
            p.kill()
        except Exception:
            pass
        try:
            p.wait(timeout=1)
        except Exception:
            pass

        self.proc = None
        self.thread = None
        self.started = False
        return False

    def _start(self):
        # Never let a failed UDP attempt permanently block TCP.
        for transport in ("udp", "tcp"):
            try:
                if self._launch(transport):
                    return
            except (FileNotFoundError, OSError):
                break
            except Exception:
                pass

        self.started = False

    def isOpened(self):
        return bool(
            self.started and
            self.proc is not None and
            self.proc.poll() is None
        )

    def read(self):
        with self.lock:
            if self.latest is None:
                return False, None
            return True, self.latest.copy()

    def release(self):
        self.stop_event.set()
        p = self.proc
        self.proc = None
        self.started = False

        if p is not None:
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=1)
            except Exception:
                pass

        self.latest = None


class CameraState:
    def __init__(self):
        self.lock = threading.RLock()
        self.running = False
        self.url = ""
        self.error = None
        self.fps = 0.0
        self.detected = []
        self.recent = []
        self.scan_active = False
        self.scan_mode = None
        self.period = None
        self.last_report = None
        self.jpeg = None
        self.version = 0
        self.latest = None
        self.stop_event = threading.Event()
        self.capture_thread = None
        self.scan_thread = None
        self.cap = None
        self.recognition = RecognitionClient()
        self.manual_request = None
        self.recognition_errors = 0
        self.last_recognition = 0.0
        self.last_ok = 0.0
        self.detected_until = 0.0

    def snapshot(self):
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
                "recognition_alive": self.recognition.alive(),
                "recognition_errors": self.recognition_errors,
                "last_recognition": self.last_ok,
            }

    def start_camera(self, url):
        self.stop_camera()
        url = (url or "").strip()
        if not url:
            return False
        with self.lock:
            self.url = url
            self.error = "Connecting to camera..."
            self.stop_event.clear()
            self.detected = []
            self.detected_until = 0.0
            self.recent = []
            self.last_report = None
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="camera-capture")
        self.scan_thread = threading.Thread(target=self._scan_loop, daemon=True, name="attendance-scan")
        self.capture_thread.start()
        self.scan_thread.start()
        return True

    def stop_camera(self):
        self.stop_event.set()
        with self.lock:
            self.running = False
            self.scan_active = False
            self.scan_mode = None
            self.manual_request = None
            cap = self.cap
            self.cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        self.recognition.stop()

    def manual_scan(self, seconds):
        with self.lock:
            if not self.running:
                return False, "Start the camera first."
            if self.scan_active or self.manual_request:
                return False, "A scan is already running."
            now = datetime.now()
            self.manual_request = {
                "session_key": "manual-" + now.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8],
                "session_date": now.strftime("%Y-%m-%d"),
                "label": f"Manual Scan {now.strftime('%H:%M:%S')}",
                "started_at": now.strftime("%H:%M:%S"),
                "duration_sec": max(3, min(int(seconds), 120)),
            }
            self.last_report = None
            self.detected = []
            self.recent = []
            return True, dict(self.manual_request)

    def _open_capture(self):
        url = self.url or DEFAULT_CAMERA_URL

        # Primary path: external ffmpeg owns RTSP transport and decoding.
        # This is deliberately independent of OpenCV's RTSP negotiation.
        try:
            cap = FFmpegCapture(url)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass

        # Fallback for installations where ffmpeg is unavailable.
        try:
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;udp|stimeout;5000000|max_delay;500000"
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass

        return None

    def _capture_loop(self):
        frame_count = 0
        fps_start = time.monotonic()
        cap = None
        while not self.stop_event.is_set():
            try:
                if cap is None or not cap.isOpened():
                    cap = self._open_capture()
                    with self.lock:
                        self.cap = cap
                    if cap is None:
                        with self.lock:
                            self.running = False
                            self.fps = 0.0
                            self.error = "Camera connection lost — reconnecting..."
                        time.sleep(1.0)
                        continue
                    with self.lock:
                        self.running = True
                        self.error = None
                    deadline = time.monotonic() + 12
                    first = None
                    while time.monotonic() < deadline and not self.stop_event.is_set():
                        ok, f = cap.read()
                        if ok and f is not None and f.size:
                            first = f
                            break
                        time.sleep(0.03)
                    if first is None:
                        try: cap.release()
                        except Exception: pass
                        cap = None
                        with self.lock:
                            self.running = False
                            self.error = "Camera opened but no frame — reconnecting..."
                        time.sleep(0.5)
                        continue
                    frame = first
                else:
                    ok, frame = cap.read()
                    if not ok or frame is None or not frame.size:
                        try: cap.release()
                        except Exception: pass
                        cap = None
                        with self.lock:
                            self.running = False
                            self.fps = 0.0
                            self.error = "Video frame stalled — reconnecting..."
                        time.sleep(0.2)
                        continue

                h, w = frame.shape[:2]
                if w > DISPLAY_WIDTH:
                    scale = DISPLAY_WIDTH / float(w)
                    display = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                else:
                    display = frame.copy()

                with self.lock:
                    matches = list(self.detected)
                for m in matches:
                    loc = m.get("location")
                    if not loc:
                        continue
                    top, right, bottom, left = loc
                    scale = min(1.0, DISPLAY_WIDTH / float(w))
                    top, right, bottom, left = [int(v * scale) for v in (top, right, bottom, left)]
                    cv2.rectangle(display, (left, top), (right, bottom), (0, 30, 255), 2)

                ok, enc = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with self.lock:
                        self.latest = frame
                        self.jpeg = enc.tobytes()
                        self.version += 1
                    frame_count += 1
                    elapsed = time.monotonic() - fps_start
                    if elapsed >= 1:
                        with self.lock:
                            self.fps = frame_count / elapsed
                        frame_count = 0
                        fps_start = time.monotonic()
            except Exception as exc:
                try:
                    if cap is not None: cap.release()
                except Exception:
                    pass
                cap = None
                with self.lock:
                    self.cap = None
                    self.running = False
                    self.error = f"Camera recovered: {type(exc).__name__}: {exc}"
                time.sleep(0.5)

        try:
            if cap is not None: cap.release()
        except Exception:
            pass
        with self.lock:
            self.cap = None
            self.running = False

    def _recognize(self, frame, students):
        return self.recognition.request("recognize", frame, students=students, timeout=2.8,
                                        tolerance=MATCH_TOLERANCE, max_width=MAX_WIDTH)

    def _blink(self, frame):
        return self.recognition.request("blink", frame, timeout=1.8, threshold=0.21, consecutive=2)

    def _finish(self, mode, period, session):
        today = datetime.now().strftime("%Y-%m-%d")
        if mode == "scheduled":
            present = db.get_period_attendance(period["id"], today)
            absent = db.get_period_absentees(period["id"], today)
        else:
            present = db.get_session_attendance(session["session_key"])
            absent = db.get_session_absentees(session["session_key"])
        report = {"type": mode, "date": today, "present": present, "absent": absent,
                  "period": period or {"label": session["label"]},
                  "completed_at": datetime.now().strftime("%H:%M:%S")}
        with self.lock:
            self.last_report = report
            self.scan_active = False
            self.scan_mode = None
            self.period = None
            self.detected = []
            self.recent = []
        return report

    def _scan_loop(self):
        active_mode = None
        active_period = None
        active_session = None
        active_end = None
        prompted = set()
        students = []
        last_refresh = 0.0
        last_rec = 0.0
        while not self.stop_event.is_set():
            try:
                if not self.running:
                    time.sleep(0.10)
                    continue
                now = time.time()
                now_dt = datetime.now()
                if now - last_refresh > 5:
                    students = db.get_all_students()
                    last_refresh = now

                with self.lock:
                    req = dict(self.manual_request) if self.manual_request else None
                if active_mode is None and req:
                    active_mode = "manual"; active_session = req; active_period = None
                    active_end = now + req["duration_sec"]; prompted = set()
                    with self.lock:
                        self.manual_request = None; self.scan_active = True; self.scan_mode = "manual"; self.period = req
                        self.detected = []; self.detected_until = 0.0; self.recent = []; self.last_report = None
                    last_rec = 0

                if active_mode is None:
                    status = db.get_period_status(now_dt)
                    p = status.get("scan")
                    if p:
                        active_mode = "scheduled"; active_period = dict(p); active_session = None
                        active_end = now + int(p["scan_duration_sec"]); prompted = set(); last_rec = 0
                        with self.lock:
                            self.scan_active = True; self.scan_mode = "scheduled"; self.period = dict(p)
                            self.detected = []; self.detected_until = 0.0; self.recent = []; self.last_report = None

                if active_mode and now >= active_end:
                    self._finish(active_mode, active_period, active_session)
                    active_mode = active_period = active_session = active_end = None
                    prompted = set()
                    continue

                interval = RECOGNITION_INTERVAL if active_mode else BACKGROUND_INTERVAL
                if students and now - last_rec >= interval:
                    frame = None
                    with self.lock:
                        frame = None if self.latest is None else self.latest.copy()
                    if frame is not None:
                        last_rec = now
                        results = self._recognize(frame, students)
                        if results:
                            with self.lock:
                                self.detected = list(results)
                                self.last_ok = time.time()
                                self.detected_until = self.last_ok + DETECTION_HOLD_SEC
                        else:
                            # Do not immediately blank the UI after one
                            # recognition cycle misses a face. This is what
                            # makes DETECTED/box/name look stable instead of
                            # flashing 1 -> 0 -> 1 while the camera remains live.
                            with self.lock:
                                if time.time() >= self.detected_until:
                                    self.detected = []

                        if active_mode:
                            date_str = now_dt.strftime("%Y-%m-%d")
                            for r in results:
                                sid = r["id"]
                                if sid in prompted:
                                    continue
                                if active_mode == "scheduled":
                                    if db.already_logged_in_period(sid, active_period["id"], date_str):
                                        prompted.add(sid); continue
                                else:
                                    if db.already_logged_in_session(sid, active_session["session_key"]):
                                        prompted.add(sid); continue
                                prompted.add(sid)
                                # Fast liveness: at most ~2 seconds. A failed
                                # liveness check never blocks the camera feed.
                                if REQUIRE_LIVENESS:
                                    live = self._blink(frame)
                                    if not live:
                                        prompted.discard(sid)
                                        continue
                                if active_mode == "scheduled":
                                    db.log_attendance(sid, r["name"], r["confidence"],
                                                      period_id=active_period["id"], session_date=date_str,
                                                      period_label=active_period["label"],
                                                      session_key=f"{date_str}:period:{active_period['id']}", session_type="scheduled")
                                else:
                                    db.log_attendance(sid, r["name"], r["confidence"],
                                                      session_date=date_str, period_label=active_session["label"],
                                                      session_key=active_session["session_key"], session_type="manual")
                                with self.lock:
                                    self.recent.insert(0, {"name": r["name"], "id": sid,
                                                           "time": datetime.now().strftime("%H:%M:%S"),
                                                           "confidence": r["confidence"]})
                                    self.recent = self.recent[:30]
            except Exception as exc:
                with self.lock:
                    self.recognition_errors += 1
                    self.error = f"Recognition recovered: {type(exc).__name__}: {exc}"
                time.sleep(0.15)
            time.sleep(0.01)


STATE = CameraState()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            self._json(200, STATE.snapshot()); return
        if path == "/mjpeg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            last = -1
            try:
                while not STATE.stop_event.is_set():
                    with STATE.lock:
                        data = STATE.jpeg; ver = STATE.version; running = STATE.running
                    if data is not None and ver != last:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                        self.wfile.flush(); last = ver
                    elif not running:
                        time.sleep(0.08)
                    else:
                        time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ConnectionError):
                pass
            return
        self._json(404, {"error":"not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try: payload = json.loads(raw.decode() or "{}")
        except Exception: payload = {}
        if path == "/start":
            url = (payload.get("url") or DEFAULT_CAMERA_URL).strip(); ok = STATE.start_camera(url); self._json(200, {"ok":ok, "url":url}); return
        if path == "/stop":
            STATE.stop_camera(); self._json(200, {"ok":True}); return
        if path == "/manual":
            ok, result = STATE.manual_scan(payload.get("seconds", 10)); self._json(200, {"ok":ok, "result":result}); return
        if path == "/encode":
            try:
                data = base64.b64decode(payload.get("jpeg", ""))
                arr = np.frombuffer(data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                result = STATE.recognition.request("encode", frame, timeout=8.0)
                self._json(200, {"ok":True, "result":result})
            except Exception as exc:
                self._json(200, {"ok":False, "error":str(exc)})
            return
        self._json(404, {"error":"not found"})


def main():
    db.init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CAMERA_SERVICE_READY {HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        STATE.stop_camera()
        httpd.server_close()


if __name__ == "__main__":
    mp.freeze_support()
    main()
