"""
recognize.py — Phase 2 (real-time capture), Phase 3 (basic liveness check),
and Phase 4 (logging) combined.

Runs the webcam, matches faces against enrolled students, and logs attendance
once per student per day (see db.already_logged_today).

Liveness check (basic anti-spoofing):
This uses a simple blink-detection heuristic via eye-aspect-ratio (EAR) —
NOT as robust as Silent-Face-Anti-Spoofing (mentioned in the project doc),
but it stops the most obvious spoof: holding up a static printed photo.
For a real deployment, swap this out for Silent-Face-Anti-Spoofing once
Phase 3 needs to be production-grade — this is a placeholder that works.

Usage:
    python recognize.py                  # run with liveness check
    python recognize.py --no-liveness    # skip liveness check (faster testing)
"""

import argparse
import time
import cv2
import numpy as np
import face_recognition
from db import init_db, get_all_students, log_attendance, already_logged_today
from excel_export import save_to_file as export_to_excel
from enroll import open_camera, brighten_for_display

MATCH_TOLERANCE = 0.5          # lower = stricter match (0.6 is face_recognition default)
EAR_THRESHOLD = 0.21           # eye-aspect-ratio below this = eye closed
BLINK_CONSEC_FRAMES = 2        # frames eye must stay closed to count as a blink
LIVENESS_TIMEOUT_SEC = 8       # give up waiting for a blink after this long


def eye_aspect_ratio(eye_points):
    """Standard EAR formula from 6 eye landmark points."""
    eye_points = np.array(eye_points)
    a = np.linalg.norm(eye_points[1] - eye_points[5])
    b = np.linalg.norm(eye_points[2] - eye_points[4])
    c = np.linalg.norm(eye_points[0] - eye_points[3])
    return (a + b) / (2.0 * c)


def check_liveness(video, timeout=LIVENESS_TIMEOUT_SEC):
    """
    Watches for a blink within `timeout` seconds. Returns True if a blink
    was detected (real face), False if not (possible spoof / static photo).
    """
    start = time.time()
    consec_closed = 0

    while time.time() - start < timeout:
        ret, frame = video.read()
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

        display = brighten_for_display(frame)
        cv2.putText(display, "Liveness check: please blink naturally", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        cv2.imshow("Attendance", display)
        cv2.waitKey(1)

        if avg_ear < EAR_THRESHOLD:
            consec_closed += 1
        else:
            if consec_closed >= BLINK_CONSEC_FRAMES:
                return True
            consec_closed = 0

    return False


def run(use_liveness: bool = True, source=0):
    init_db()
    students = get_all_students()
    if not students:
        print("No students enrolled yet. Run enroll.py first.")
        return

    known_ids = [s[0] for s in students]
    known_names = [s[1] for s in students]
    known_encodings = [np.array(s[2]) for s in students]

    video = open_camera(source=source)
    if video is None:
        raise RuntimeError(
            "Could not open video source. Check: (1) Windows Settings > Privacy > Camera "
            "> 'Let desktop apps access your camera' is ON, (2) no other app "
            "(Zoom/Teams/browser) is currently using the camera."
        )

    print("Recognition running. Press ESC to quit.")
    already_prompted = set()  # avoid re-triggering liveness for the same face repeatedly

    try:
        while True:
            ret, frame = video.read()
            if not ret:
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(rgb)
            face_encodings = face_recognition.face_encodings(rgb, face_locations)

            for (top, right, bottom, left), encoding in zip(face_locations, face_encodings):
                distances = face_recognition.face_distance(known_encodings, encoding)
                best_idx = int(np.argmin(distances)) if len(distances) else None
                match = best_idx is not None and distances[best_idx] <= MATCH_TOLERANCE

                if match:
                    student_id = known_ids[best_idx]
                    name = known_names[best_idx]
                    confidence = round(1 - distances[best_idx], 3)  # rough confidence proxy
                    color = (0, 200, 0)
                    label = f"{name} ({confidence})"

                    if already_logged_today(student_id):
                        label += " - already logged"
                    elif student_id in already_prompted:
                        pass  # currently mid-liveness-check for this student this run
                    else:
                        already_prompted.add(student_id)
                        if use_liveness:
                            print(f"Detected {name}. Running liveness check...")
                            is_live = check_liveness(video)
                            if not is_live:
                                print(f"  -> Liveness check failed for {name}. Not logged.")
                                already_prompted.discard(student_id)
                                continue
                        log_attendance(student_id, name, confidence)
                        export_to_excel()
                        print(f"  -> Logged attendance for {name} ({student_id}) and updated attendance_export.xlsx")
                else:
                    color = (0, 0, 255)
                    label = "Unknown - flagged for review"

                cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                cv2.putText(frame, label, (left, top - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            cv2.imshow("Attendance", frame)
            if cv2.waitKey(1) & 0xFF == 27:  # ESC
                break
    finally:
        video.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-liveness", action="store_true", help="Skip liveness/blink check")
    parser.add_argument("--source", type=str, default="0",
                         help="Camera index (e.g. 0) or a network stream URL (rtsp://... or http://...)")
    args = parser.parse_args()
    source = int(args.source) if args.source.isdigit() else args.source
    run(use_liveness=not args.no_liveness, source=source)
