# AI Facial Recognition Attendance System — Master Project File (v3)

Paste this whole file back into a new chat any time to pick up exactly where you
left off. This supersedes v2 — everything below reflects the current state
after adding Windows auto-start for the app.

---

## Project goal

AI facial-recognition classroom attendance system. Started as a personal/
portfolio project, currently deployed as a single-classroom pilot, with the
longer-term goal of formal, college-wide institutional rollout (approval not
yet obtained — see "Open items" below).

---

## Current interface: Flask (`app_web.py`) — NOT Streamlit

A Streamlit version was built at one point (`app_streamlit.py`, `.streamlit/
config.toml`) with the Nothing-phone black/white/red aesthetic. It was
**reverted back to Flask** as the primary interface. The Streamlit files still
exist but are not the active version — don't confuse the two if both are
sitting in the project folder.

---

## Environment — READ THIS FIRST

**This machine could NOT use a standard `venv` + `pip` setup.** Windows Device
Guard / Application Control policy blocks `pip.exe` directly and blocks
pandas' compiled DLLs. **Conda is required, not venv:**

```
conda activate attendance
cd C:\Users\nikhil\attendance_system
```

Conda env lives inside the project folder:
`C:\Users\nikhil\attendance_system\conda\envs\attendance`

**Full working install sequence:**
```
conda create -n attendance python=3.11 -y
conda activate attendance
conda install -c conda-forge dlib -y
pip install face_recognition opencv-python numpy Flask Pillow openpyxl
pip install "setuptools<81"
```

**Recurring gotcha:** `face_recognition` throws a misleading "please install
face_recognition_models" error even when that package IS installed — the real
cause is `pkg_resources` (from `setuptools`) being missing/broken on newer
setuptools versions. Fix is always: `pip install "setuptools<81"`. This has
hit multiple times across sessions — check this FIRST before anything else
if that error reappears.

---

## NEW (v3): Auto-start on Windows — no more manual terminal

The app now starts automatically at login instead of needing someone to open
a terminal and type the run command every time.

**`start_attendance.bat`** (in `C:\Users\nikhil\attendance_system`):
```bat
@echo off
call conda activate attendance
cd /d C:\Users\nikhil\attendance_system
python app_web.py >> attendance_log.txt 2>&1
```
All Flask console output (including errors) goes to `attendance_log.txt`
since there's no visible terminal window to read it from.

**Task Scheduler setup:**
- Task name: "Attendance System Autostart"
- Trigger: At log on
- Action: Start a program → `C:\Users\nikhil\attendance_system\start_attendance.bat`
- General tab: **"Run only when user is logged on"** (NOT "run whether logged
  on or not" — that option demanded a Windows account password that kept
  getting rejected; since this is a personal laptop that's logged into
  directly, "run only when logged on" avoids the password prompt entirely
  and still auto-starts on login)

**Confirmed working**: verified by closing all terminals, confirming no
`python.exe` process was running via Task Manager, running the scheduled
task manually once, and seeing `localhost:5000` load without any terminal
being opened.

**Important gotcha to remember:** if you manually open a terminal and type
`python app_web.py` yourself (instead of letting the task run it), closing
that terminal WILL kill the server — that's normal and separate from the
auto-start task. Only the process launched by Task Scheduler survives
independent of any terminal window.

---

## Full file list and status

| File | Purpose | Status |
|---|---|---|
| `db.py` | SQLite schema + all DB functions (students, attendance, consent, edit/rename) | ✅ Working, unchanged since original build |
| `enroll.py` | CLI enrollment (webcam or photo folder) | ✅ Working, unchanged |
| `recognize.py` | CLI recognition, EAR-based blink liveness check, auto-refreshes Excel export | ✅ Working. Source of truth for the liveness logic reused elsewhere |
| `app_web.py` | **Main app, currently active.** Flask dashboard + login system, now auto-started via Task Scheduler | ✅ Working, auto-start added |
| `auth.py` | Login layer — users table, sha256+salt hashing, teacher/student roles | ✅ Working |
| `create_teacher.py` | One-time CLI script to create the first teacher login | ✅ Working, run once already |
| `auto_scanner.py` | Standalone background scanner — NOT currently needed for Flask | ✅ Built, works, but redundant while on Flask |
| `excel_export.py` | Builds `.xlsx` using `openpyxl` | ✅ Working, unchanged |
| `view_log.py` | Zero-dependency terminal viewer | ✅ Working, unchanged |
| `camera_test.py` | Diagnostic for camera backend/index issues | ✅ Working, unchanged |
| `start_attendance.bat` | Launcher script for Task Scheduler auto-start | ✅ Working, new this round |
| `attendance_log.txt` | Captures Flask console output since there's no visible terminal on auto-start | ✅ New this round |
| `dashboard.py` | Old Streamlit-adjacent legacy file | ⚠️ Legacy, don't use |
| `app_streamlit.py` | Full Streamlit rebuild (not currently active) | ⚠️ Built and working as of last test, but NOT the active interface |
| `.streamlit/config.toml` | Streamlit theme (Nothing-phone aesthetic) | ⚠️ Only relevant if Streamlit is reactivated |

---

## Login system (`auth.py`) — how it works

- Separate `users` table (username, password_hash, salt, role, student_id,
  created_at). SHA-256 + per-user salt — adequate for a college pilot, not
  bank-grade; swap to bcrypt if this ever needs to be hardened.
- **Teacher accounts:** created via `python create_teacher.py` (one-time CLI,
  asks for username/password). No self-service teacher signup by design.
- **Student accounts:** created automatically during Self-Enroll — the
  enrollment form now also collects a password, so enrolling your face and
  creating your login happen in the same step. Username = student ID.
- **Roles determine what's visible:**
  - Teacher: full dashboard (Today's Attendance, Full Log, Attendance %,
    Self-Enroll, Manage Students, Live Camera Scan, Export)
  - Student: only `/my_attendance` — their own records, nothing else
- **`/enroll` and `/enroll_submit` are intentionally NOT behind login** —
  that's the only way a new student can create an account in the first place.
  Everything else requires login; teacher-only routes explicitly check role.
- **Password reset:** Manage Students page now has a "Set password" /
  "Reset password" button per student (`/reset_password/<student_id>`) —
  closes the earlier gap where manually-added students (via the teacher's
  "add without camera" form) had no login. `auth.set_student_password()`
  creates the account if missing, resets it if it exists.
- **Known limitation, now MORE urgent because of auto-start:**
  `FLASK_SECRET_KEY` defaults to a random value that changes every server
  restart. Every time the auto-start task restarts the app (e.g. on reboot),
  everyone gets logged out. Needs to be set as a fixed environment variable —
  this is the next priority fix, see "Immediate next steps."

---

## Live camera scanning — TWO separate mechanisms, don't confuse them

1. **`app_web.py`'s built-in live scan** (`/live` page): a background thread
   (`camera_worker`) started when a teacher clicks "Start scanning" with an
   RTSP URL. Runs recognition every ~2 seconds while active, independent of
   any browser tab (it's a server-side thread, not tied to the page being
   open). **This is what's currently in use.**

2. **`auto_scanner.py`**: a fully standalone script, independent of Flask or
   Streamlit entirely — talks directly to `db.py`. Built for scheduled,
   unattended scanning (cron-style). Supports:
   - Fixed interval: `python auto_scanner.py --source "rtsp://..." --interval 60`
     (scans every N minutes exactly, repeating)
   - Randomized window (default): `--min-interval` / `--max-interval` in
     minutes, less predictable timing
   - Auto-regenerates the Excel report after every scan cycle
   - Includes the same EAR-based blink liveness check as `recognize.py`

   **Current status: not actively needed** while on Flask, since option 1
   already covers continuous scanning independent of the browser. Would
   become relevant again if reverting to Streamlit (where it WAS needed,
   since Streamlit's live loop is tied to the page session) or if a
   fully separate always-on scanning process (outside the dashboard
   entirely) is wanted later.

---

## Network access — reachable from other devices now

`app_web.py` runs on `0.0.0.0:5000`, reachable by any device on the same
WiFi via `http://<pc-local-ip>:5000` (find IP with `ipconfig`).

**Firewall setup completed:**
- Added Windows Firewall inbound rule for TCP port 5000 (all profiles) via
  Windows Defender Firewall with Advanced Security → Inbound Rules → New
  Rule → Port → TCP 5000 → Allow. This was the fix that mattered — the
  app-based exception for `python.exe` already existed and wasn't sufficient
  on its own.
- If mobile/other-device access breaks again: check (a) same WiFi network on
  both devices, not a guest/isolated network, (b) PC's IP hasn't changed
  (`ipconfig` again), (c) the port-5000 inbound rule is still present, (d)
  the auto-start task actually ran (check Task Manager for `python.exe`, or
  `attendance_log.txt` for errors).

Deliberately NOT exposed to the public internet (no ngrok, no Streamlit
Community Cloud, no Lovable) — biometric/attendance data under India's DPDP
Act reasoning, consistent with the original plan.

---

## Open item: TCS iON sync

College uses TCS iON as the official attendance ERP. Goal: auto-sync this
system's attendance into TCS iON. **Blocked on one unanswered question**:
does the college's TCS iON instance support an API or bulk-file import, or
is it purely a manual web form?

- Need to ask college IT/ERP admin directly.
- If bulk import exists: `excel_export.py` can likely be adapted to match
  whatever exact column format TCS iON expects — straightforward once the
  template is known.
- If a real API exists: proper integration is possible, but needs official
  credentials from the college, not something to reverse-engineer.
- If neither exists (pure manual form): the only technical option is browser
  automation to auto-fill TCS iON's form, which was explicitly flagged as
  risky (possible ToS issues, fragile against UI changes) and NOT something
  to build without institutional sign-off.

**Status: waiting on IT's answer. Nothing built yet for this.**

---

## Open item: formal college-wide deployment

Discussed the full production roadmap (this is a distinct, larger effort
from the current single-classroom pilot):

1. Institutional approval first (IT/registrar/data protection officer) —
   biometric data under DPDP Act needs sign-off before wider building.
2. Written data policy (what's stored, retention, deletion, access).
3. Dedicated server on the campus network — **recommend Linux, not Windows**,
   to avoid the entire Device Guard/setuptools saga this project has hit
   repeatedly on the current Windows/conda setup.
4. Migrate off SQLite to PostgreSQL/MySQL for concurrent multi-classroom
   writes (function signatures in `db.py`/`auth.py` can stay the same from
   the app's point of view — only the underlying connection changes).
5. Multi-classroom schema (`classroom_id` column) + either one
   `auto_scanner.py` process per classroom, or extending Flask's
   `camera_state` to track multiple cameras.
6. Real WSGI server (gunicorn/waitress) + nginx reverse proxy — the Flask
   dev server explicitly isn't meant for production/concurrent users.
7. HTTPS + a fixed `FLASK_SECRET_KEY` env var (currently random per restart —
   see login system section above, now higher priority).
8. systemd services for auto-restart/auto-start on reboot — Windows now has
   an equivalent via Task Scheduler (done, see above), but a real Linux
   deployment would use systemd instead.
9. Scheduled database backups.
10. Pilot the new server-based setup in ONE classroom before expanding
    college-wide.

**Status: nothing built yet — waiting on IT to actually provide a server**
before any of this migration work starts. Come back once that happens.

---

## Immediate next steps (in order)

1. **Fix `FLASK_SECRET_KEY`** — set a fixed environment variable so login
   sessions survive restarts. Now more urgent since the auto-start task will
   restart the app on every reboot, silently logging everyone out each time.
2. Enroll real students (not just test accounts) via `/enroll`.
3. Test Live Camera Scan against the real classroom RTSP feed with multiple
   real people, tune `MATCH_TOLERANCE` (currently 0.5, in both `recognize.py`
   and `app_web.py`) based on real false positive/negative rates.
4. Confirm manually-added students (via teacher's "add manually" form) are
   actually getting logins set via the password reset/set button — this was
   a known gap, closed in code, but worth a real check.
5. Ask college IT the TCS iON API/bulk-import question.
6. If pursuing formal rollout: start the institutional approval conversation
   in parallel with running the current pilot.

---

## Quick command reference

```
conda activate attendance
cd C:\Users\nikhil\attendance_system

python db.py                        # create/reset the database (safe to re-run)
python create_teacher.py            # one-time: create a teacher login
python app_web.py                   # main app — login, dashboard, enroll, live scan, export
python recognize.py                 # CLI recognition, local webcam
python recognize.py --source "rtsp://user:pass@<ip>:554/stream1"   # CLI recognition, CCTV
python auto_scanner.py --source "rtsp://..." --interval 60         # standalone scheduled scanner (not currently needed on Flask)
python view_log.py                  # zero-dependency terminal log viewer
python camera_test.py               # diagnose local webcam issues
```

**Auto-start is now handled by Task Scheduler** (see above) — manually
running `python app_web.py` is only needed for debugging or if the
scheduled task isn't working. Dashboard: `http://localhost:5000` (or
`http://<pc-local-ip>:5000` from other devices on the same WiFi). Students
self-enroll (face + password) at `/enroll` without needing to log in first.

---

*Built by B. Nikhilesh*
