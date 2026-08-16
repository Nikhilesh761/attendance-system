"""
enroll.py — Phase 1: Data Collection (Enrollment)

Two ways to enroll a student:
  1. --webcam   : capture live photos from your webcam (5 shots, different angles)
  2. --folder   : point to a folder of existing photos for one student

Usage:
    python enroll.py --webcam --id S001 --name "Jane Doe"
    python enroll.py --folder ./photos/jane_doe --id S001 --name "Jane Doe"

Consent note: only run this after you've collected written consent from the
student (see the DPDP Act note in the project doc) — this is biometric data.
"""

import argparse
import os
import cv2
import face_recognition
import numpy as np
from db import init_db, add_student


def open_camera(source=0, max_index: int = 3):
    """
    Tries to open a working video source.
    - If `source` is a string containing '://' (rtsp://, http://), it's treated
      as a network/IP camera stream and opened directly.
    - Otherwise, tries local webcam indices with Windows-friendly backends —
      DirectShow (CAP_DSHOW) is usually more reliable there than the default.
    """
    if isinstance(source, str) and "://" in source:
        cap = cv2.VideoCapture(source)
        if cap.isOpened():
            frame = None
            for _ in range(20):
                ret, f = cap.read()
                if ret and f is not None:
                    frame = f
            if frame is not None:
                return cap
        cap.release()
        return None

    backends = [cv2.CAP_DSHOW, cv2.CAP_ANY] if os.name == "nt" else [cv2.CAP_ANY]
    for backend in backends:
        for idx in range(max_index):
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                frame = None
                for _ in range(20):
                    ret, f = cap.read()
                    if ret and f is not None:
                        frame = f
                if frame is not None:
                    return cap
            cap.release()
    return None


def brighten_for_display(frame, alpha=1.6, beta=40):
    """Boosts brightness/contrast for the on-screen preview only — does NOT
    affect the frame used for face detection/encoding, so recognition
    accuracy is unaffected by this cosmetic adjustment."""
    return cv2.convertScaleAbs(frame, alpha=alpha, beta=beta)


def get_consent(name: str) -> bool:
    """Requires the student to type their own name to confirm consent — not a Y/n shortcut."""
    print(f"\nConsent check for {name}:")
    print("This will store a face-derived numeric vector (not a photo) for attendance matching.")
    print("It can be deleted at any time on request.")
    typed = input(f'Type "{name}" exactly to confirm you consent to enrollment: ').strip()
    return typed == name

NUM_WEBCAM_SHOTS = 5
PROMPTS = [
    "Look straight at the camera",
    "Turn slightly LEFT",
    "Turn slightly RIGHT",
    "Tilt chin up slightly",
    "Neutral, look straight again",
]


def capture_from_webcam():
    """Capture NUM_WEBCAM_SHOTS frames, one per prompt, on spacebar press."""
    video = open_camera()
    if video is None:
        raise RuntimeError(
            "Could not open webcam. Check: (1) Windows Settings > Privacy > Camera "
            "> 'Let desktop apps access your camera' is ON, (2) no other app "
            "(Zoom/Teams/browser) is currently using the camera, (3) try unplugging "
            "and replugging an external webcam if you have one."
        )

    frames = []
    shot = 0
    print("Press SPACE to capture each shot, ESC to cancel.")

    while shot < NUM_WEBCAM_SHOTS:
        ret, frame = video.read()
        if not ret:
            continue

        display = brighten_for_display(frame)
        cv2.putText(display, PROMPTS[shot], (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(display, f"Shot {shot + 1}/{NUM_WEBCAM_SHOTS} - SPACE or C to capture",
                    (20, 470), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow("Enrollment", display)
        cv2.setWindowProperty("Enrollment", cv2.WND_PROP_TOPMOST, 1)

        key = cv2.waitKey(1) & 0xFF
        if key != 255:  # 255 = no key pressed; anything else, show what was received
            print(f"  (key received: {key})")
        if key == 27:  # ESC
            video.release()
            cv2.destroyAllWindows()
            raise KeyboardInterrupt("Enrollment cancelled")
        elif key in (32, ord('c'), ord('C')):  # SPACE or C
            faces = face_recognition.face_locations(frame)
            if len(faces) != 1:
                print(f"  -> Expected exactly 1 face, found {len(faces)}. Try again.")
                continue
            frames.append(frame)
            shot += 1
            print(f"  -> Captured shot {shot}/{NUM_WEBCAM_SHOTS}")

    video.release()
    cv2.destroyAllWindows()
    return frames


def load_from_folder(folder_path: str):
    frames = []
    valid_ext = (".jpg", ".jpeg", ".png")
    for fname in sorted(os.listdir(folder_path)):
        if fname.lower().endswith(valid_ext):
            img = cv2.imread(os.path.join(folder_path, fname))
            if img is not None:
                frames.append(img)
    if not frames:
        raise ValueError(f"No valid images found in {folder_path}")
    return frames


def frames_to_average_encoding(frames):
    """Encode each frame, average the encodings into one representative vector."""
    encodings = []
    for i, frame in enumerate(frames):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb)
        if len(locations) != 1:
            print(f"  Skipping frame {i}: found {len(locations)} faces (need exactly 1)")
            continue
        enc = face_recognition.face_encodings(rgb, locations)[0]
        encodings.append(enc)

    if not encodings:
        raise ValueError("Could not extract any valid face encodings from the given frames.")

    return np.mean(encodings, axis=0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--webcam", action="store_true", help="Capture from webcam")
    parser.add_argument("--folder", type=str, help="Path to folder of existing photos")
    parser.add_argument("--id", required=True, help="Student ID, e.g. S001")
    parser.add_argument("--name", required=True, help="Student full name")
    args = parser.parse_args()

    if not args.webcam and not args.folder:
        parser.error("Provide either --webcam or --folder")

    init_db()

    if not get_consent(args.name):
        print("Consent not confirmed. Enrollment cancelled.")
        return

    if args.webcam:
        frames = capture_from_webcam()
    else:
        frames = load_from_folder(args.folder)

    print(f"Processing {len(frames)} photo(s) for {args.name}...")
    avg_encoding = frames_to_average_encoding(frames)
    add_student(args.id, args.name, avg_encoding, consent_given=True)
    print(f"Enrolled: {args.name} ({args.id})")


if __name__ == "__main__":
    main()
