"""
auto_scanner.py — standalone background scanner.

Runs INDEPENDENTLY of any dashboard (Flask or Streamlit) — talks directly to
db.py. Two scheduling modes:

  FIXED interval (cron-style, you set the exact gap):
      python auto_scanner.py --source "rtsp://..." --interval 60
      (scans every 60 minutes, on the dot, repeating)

  RANDOMIZED window (default — less predictable, avoids students timing
  when the scan happens):
      python auto_scanner.py --source "rtsp://..."
      python auto_scanner.py --source "rtsp://..." --min-interval 30 --max-interval 90

Each cycle: opens the camera, watches for known faces for a short window,
runs the same blink-liveness check as recognize.py, logs attendance,
regenerates the Excel report, then closes the camera and sleeps until the
next cycle.

Why standalone: if scanning were tied to a dashboard page being open in a
browser, it would stop the moment the tab closes. This script runs on its
own, in its own terminal (or set up as a Windows Task Scheduler job later),
and any dashboard just reads what it logs — it never drives the camera itself.

Run this in a SEPARATE terminal from your dashboard (streamlit run ... or
python app_web.py).
"""

import argparse
import random
import time
import cv2
import numpy as np
import face_recognition
from datetime import datetime

from db import init_db, get_all_students, log_attendance, already_logged_today
from excel_export import save_to_file as export_to_excel

MATCH_TOLERANCE = 0.5      # keep in sync with recognize.py / app_streamlit.py
EAR_THRESHOLD = 0.21
BLINK_CONSEC_FRAMES = 2
LIVENESS_TIMEOUT_SEC = 8    # per detected face, how long to wait for a blink
SCAN_WINDOW_SEC = 15        # how long each scan cycle watches the room


def eye_aspect_ratio(eye_points):
    """Mirrors recognize.py exactly."""
    eye_points = np.array(eye_points)
    a = np.linalg.norm(eye_points[1] - eye_points[5])
    b = np.linalg.norm(eye_points[2] - eye_points[4])
    c = np.linalg.norm(eye_points[0] - eye_points[3])
    return (a + b) / (2.0 * c)


def check_liveness(cap, timeout=LIVENESS_TIMEOUT_SEC):
    """Same blink-detection heuristic as recognize.py, no display window needed here."""
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

        left_ear = eye_aspect_ratio(landmarks["left_eye"])
        right_ear = eye_aspect_ratio(landmarks["right_eye"])
        avg_ear = (left_ear + right_ear) / 2.0

        if avg_ear < EAR_THRESHOLD:
            consec_closed += 1
        else:
            if consec_closed >= BLINK_CONSEC_FRAMES:
                return True
            consec_closed = 0

    return False


def run_scan_cycle(source, known_ids, known_names, known_encodings):
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        print(f"[{datetime.now()}] Could not open camera stream — check URL/network.")
        return

    print(f"[{datetime.now()}] Scan cycle started ({SCAN_WINDOW_SEC}s window).")
    already_prompted = set()  # avoid re-triggering liveness for the same face repeatedly this cycle
    start = time.time()

    while time.time() - start < SCAN_WINDOW_SEC:
        ret, frame = cap.read()
        if not ret:
            continue

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        face_locations = face_recognition.face_locations(rgb)
        face_encodings = face_recognition.face_encodings(rgb, face_locations)

        for encoding in face_encodings:
            if not known_encodings:
                continue
            distances = face_recognition.face_distance(known_encodings, encoding)
            best = int(np.argmin(distances))
            if distances[best] > MATCH_TOLERANCE:
                continue  # unrecognized face — not logged, matches recognize.py behavior

            student_id = known_ids[best]
            name = known_names[best]
            confidence = round(1 - distances[best], 3)

            if already_logged_today(student_id) or student_id in already_prompted:
                continue

            already_prompted.add(student_id)
            print(f"[{datetime.now()}] Detected {name}. Checking liveness...")
            if check_liveness(cap):
                log_attendance(student_id, name, confidence)
                print(f"[{datetime.now()}] Logged {name} ({student_id}), confidence {confidence}.")
            else:
                print(f"[{datetime.now()}] Liveness check failed for {name}. Not logged.")
                already_prompted.discard(student_id)

    cap.release()
    export_to_excel()
    print(f"[{datetime.now()}] Scan cycle ended. Report updated.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str, required=True,
                         help="Camera stream URL (rtsp://... or http://...)")
    parser.add_argument("--interval", type=int, default=None,
                         help="FIXED minutes between scan cycles (cron-style, e.g. 60 = every hour exactly). "
                              "If set, this overrides --min-interval/--max-interval.")
    parser.add_argument("--min-interval", type=int, default=45,
                         help="Minimum minutes between scan cycles when using the randomized window (default 45)")
    parser.add_argument("--max-interval", type=int, default=75,
                         help="Maximum minutes between scan cycles when using the randomized window (default 75)")
    args = parser.parse_args()

    if args.interval is None and args.min_interval > args.max_interval:
        raise SystemExit("--min-interval cannot be greater than --max-interval")

    init_db()
    if args.interval is not None:
        print(f"Auto-scanner starting. Fixed interval: every {args.interval} minutes. Press Ctrl+C to stop.")
    else:
        print(
            f"Auto-scanner starting. Scanning at a random time every "
            f"{args.min_interval}-{args.max_interval} minutes (averages ~1/hour). "
            f"Press Ctrl+C to stop."
        )

    try:
        while True:
            students = get_all_students()
            if not students:
                print(f"[{datetime.now()}] No enrolled students yet. Waiting for next cycle...")
            else:
                known_ids = [s[0] for s in students]
                known_names = [s[1] for s in students]
                known_encodings = [np.array(s[2]) for s in students]
                run_scan_cycle(args.source, known_ids, known_names, known_encodings)

            if args.interval is not None:
                wait_minutes = args.interval
            else:
                wait_minutes = random.uniform(args.min_interval, args.max_interval)

            print(f"[{datetime.now()}] Next scan in {wait_minutes:.1f} minutes.")
            time.sleep(wait_minutes * 60)
    except KeyboardInterrupt:
        print("\nAuto-scanner stopped.")


if __name__ == "__main__":
    main()
