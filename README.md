# AI Facial Recognition Attendance System

Starter implementation matching the build plan — Phases 1, 2, 3, and 4.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

If `dlib` fails to build (common on Windows), try:
```bash
pip install dlib-binary
pip install -r requirements.txt
```

## Files

| File | What it does |
|---|---|
| `db.py` | SQLite schema + all database read/write functions. Run once to initialize: `python db.py` |
| `enroll.py` | Optional CLI enrollment via webcam or photo folder — not required, everything's in the browser now |
| `recognize.py` | CLI recognition — works with a local webcam OR a network camera via `--source rtsp://...` |
| `app_web.py` | **Main dashboard — enroll, edit/manage students, view attendance, live CCTV scan, download Excel.** Use this for everything. |
| `excel_export.py` | Builds the `.xlsx` export using openpyxl — no pandas |
| `view_log.py` | Zero-dependency terminal fallback viewer |
| `dashboard.py` | Older Streamlit version — kept for reference only |
| `camera_test.py` | Diagnostic for local webcam backend/index issues |

## Run order

```bash
python db.py                # 1. create the database
python app_web.py           # 2. everything happens here — enroll, edit, view, scan, export
```

## Using a CCTV/IP camera (not a laptop webcam)

A browser can only access cameras physically connected to the device you're using
(`getUserMedia`) — it can't reach a network CCTV camera directly. For that, the
**Live Camera Scan** page (`/live`) has the server connect to the camera's RTSP stream
directly, run recognition there, and push the live feed + results into your browser.

1. Find your camera's RTSP URL — check its app or manual. Common patterns:
   - Hikvision: `rtsp://user:pass@<camera-ip>:554/Streaming/Channels/101`
   - Dahua: `rtsp://user:pass@<camera-ip>:554/cam/realmonitor?channel=1&subtype=0`
   - Generic/ONVIF cameras: usually `rtsp://user:pass@<camera-ip>:554/stream1`
2. Open `http://localhost:5000/live`, paste the URL, click **Start scanning**.
3. The page shows the live feed and a running list of who's been recognized —
   attendance logs and the Excel export update automatically in the background,
   the same as the local-webcam path.

**Requirements for this to work:** the PC running `app_web.py` must be able to reach
the camera on the network (same WiFi/LAN, or a wired connection to the same switch as
the camera) — not the open internet. If the URL is wrong or unreachable, the page shows
an error instead of a blank feed.

You can also run recognition against the same camera from the terminal without the
browser, if you prefer:
```bash
python recognize.py --source "rtsp://user:pass@192.168.1.50:554/stream1"
```

## Everything now happens in the browser

Open `http://localhost:5000` (or your shared network link):

- **Self-Enroll** — enroll via live camera (3 guided photos) OR upload an existing photo instead — no terminal needed either way. Consent is required for both paths.
- **Manage Students** — see everyone enrolled, **edit their ID/name**, revoke their consent, or delete them entirely. Editing relabels their attendance history too, so past records stay linked correctly.
- **Download Excel** — generates a fresh `.xlsx` on demand with two sheets: the full attendance log and the student roster with consent status. Also auto-regenerates (`attendance_export.xlsx`, saved in your project folder) every time `recognize.py` logs someone — so the file is always current without you doing anything.
- **Today's Attendance / Full Log / Attendance %** — same as before.

## Letting friends / your college access it

```bash
python app_web.py
```
Find your local IP (`ipconfig`), share `http://<your-ip>:5000` with people on the same WiFi. They can self-enroll straight from their own phone/laptop browser.

**Important:** local network only — not exposed to the wider internet, intentionally, per the DPDP Act note on biometric data in the original plan. A real college-wide rollout beyond one WiFi network is a separate, bigger step (proper hosting + login/auth).

## Why Flask instead of Streamlit

The original `dashboard.py` used Streamlit, which depends on pandas internally. On some
Windows machines with Device Guard / Application Control policies (common on college-managed
or security-hardened laptops), pandas' compiled components get blocked outright, breaking
the entire dashboard. `app_web.py` does the same job with Flask, which has no compiled
native dependencies beyond what you already need for face recognition — so it installs
and runs cleanly even on machines where Streamlit/pandas won't. If you're on a completely
unrestricted machine and want to still use the Streamlit version, uncomment `streamlit` and
`pandas` in `requirements.txt`.

## What's implemented vs. what's still a placeholder

**Implemented:**
- Enrollment via terminal (guided webcam shots or bulk photo folder) OR self-service in the browser (Self-Enroll tab)
- Consent is now mandatory in both paths — CLI requires typing your name to confirm; browser requires checking a consent box before the camera unlocks. Enrollment without consent is refused at the database layer, not just skipped in the UI.
- Consent can be withdrawn anytime (Manage Students tab) — removes the person from future recognition while keeping their attendance history
- Averaging multiple encodings per student for a more robust reference vector
- Real-time detection + recognition with confidence display
- Basic liveness check (blink detection via eye-aspect-ratio) — stops a printed photo held up to the camera
- Duplicate-prevention (one log entry per student per day)
- Dashboard: today's attendance, absentee list, full log with CSV export, attendance % with a configurable defaulter threshold, self-enrollment, and student management

**Still placeholder / needs upgrading before real deployment:**
- **Liveness detection only exists in `recognize.py`'s CLI path** (basic blink-check, not `Silent-Face-Anti-Spoofing`). The browser Live Camera Scan (`/live`) does NOT currently check for liveness — it logs whoever it recognizes in frame, so a printed photo held up to the CCTV camera would work. Worth adding before trusting this for real attendance, not just a demo.
- **Accuracy tuning** — `MATCH_TOLERANCE` (0.5, set in both `recognize.py` and `app_web.py`) needs testing in your actual classroom lighting/camera angle before you trust it.
- **No login/auth on the dashboard** — anyone with the link can see the student list and everyone's attendance %. Fine for a small trusted class/friend pilot on your own WiFi; not fine if you ever expose this beyond that.
- **Self-enrollment has no identity check** — nothing stops someone from typing a friend's name and enrolling their own face under it. For a casual pilot this is a non-issue; for a real class roster, spot-check the Manage Students list against the actual roster.

## Suggested next steps (matches the original 6-week plan)

- Week 1-2: run through setup above, enroll yourself + a few test faces, confirm recognition works
- Week 3: test `recognize.py` in the actual room you'll deploy in — check lighting, angle, distance from camera
- Week 4: swap in real anti-spoofing, add the consent field to `db.py`
- Week 5: real classroom test, tune `MATCH_TOLERANCE` based on false positives/negatives you see
- Week 6: polish the dashboard, add whatever reporting your institution actually asks for
