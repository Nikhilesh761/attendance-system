"""
camera_test.py — standalone diagnostic. Doesn't touch face_recognition or the
rest of the project at all. Just tries to open the camera every way OpenCV
knows how, and reports which combination (if any) actually returns a real,
non-blank frame.

Usage:
    python camera_test.py
"""

import cv2
import numpy as np

BACKENDS = [
    ("CAP_DSHOW", cv2.CAP_DSHOW),
    ("CAP_MSMF", cv2.CAP_MSMF),
    ("CAP_ANY", cv2.CAP_ANY),
]

print("Scanning camera indices 0-4 across backends...\n")

working = []

for backend_name, backend in BACKENDS:
    for idx in range(5):
        cap = cv2.VideoCapture(idx, backend)
        if not cap.isOpened():
            cap.release()
            continue

        # Give the camera a moment to warm up — first few frames are often blank/black
        frame = None
        for _ in range(10):
            ret, f = cap.read()
            if ret and f is not None:
                frame = f

        if frame is None:
            print(f"[{backend_name} idx={idx}] Opened but no frame returned.")
            cap.release()
            continue

        brightness = float(np.mean(frame))
        is_blank = brightness < 2.0  # near-black average = likely blank

        status = "BLANK/BLACK" if is_blank else "OK - real image"
        print(f"[{backend_name} idx={idx}] Opened. Frame shape={frame.shape}, "
              f"avg brightness={brightness:.1f} -> {status}")

        if not is_blank:
            working.append((backend_name, idx))
            # Show it briefly so you can visually confirm
            cv2.imshow(f"{backend_name} idx={idx}", frame)
            cv2.waitKey(1500)
            cv2.destroyAllWindows()

        cap.release()

print("\n--- Summary ---")
if working:
    print("Working combinations (backend, index):")
    for b, i in working:
        print(f"  {b}, index {i}")
    print(f"\nUse this one in the code: backend={working[0][0]}, index={working[0][1]}")
else:
    print("No backend/index returned a real image.")
    print("This points to a driver, privacy-setting, or physical camera issue —")
    print("not a bug in the attendance system code.")
