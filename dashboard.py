"""
dashboard.py — Phase 4/5: teacher-facing dashboard.

Run with:
    streamlit run dashboard.py

Shows: today's attendance, full log, per-student attendance %, and a simple
"add student" form (webcam capture still happens via enroll.py — this just
lets you review/manage what's already enrolled).
"""

import streamlit as st
import pandas as pd
import numpy as np
from PIL import Image
import face_recognition
from datetime import datetime
from db import (
    init_db,
    get_all_students,
    get_all_students_admin,
    get_attendance_log,
    get_attendance_percentage,
    add_student,
    revoke_consent,
    delete_student,
)

st.set_page_config(page_title="Attendance Dashboard", layout="wide")
init_db()

st.title("Attendance Dashboard")

students = get_all_students()
log = get_attendance_log(limit=500)

col1, col2, col3 = st.columns(3)
col1.metric("Enrolled students", len(students))

today = datetime.now().strftime("%Y-%m-%d")
today_count = len([r for r in log if r["timestamp"].startswith(today)])
col2.metric("Present today", today_count)
col3.metric("Not yet marked today", max(len(students) - today_count, 0))

st.divider()

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["Today's Attendance", "Full Log", "Attendance %", "Self-Enroll", "Manage Students"]
)

with tab1:
    today_rows = [r for r in log if r["timestamp"].startswith(today)]
    if today_rows:
        df = pd.DataFrame(today_rows)
        df["time"] = pd.to_datetime(df["timestamp"]).dt.strftime("%H:%M:%S")
        st.dataframe(df[["student_id", "name", "time", "confidence"]], use_container_width=True)
    else:
        st.info("No attendance logged yet today.")

    marked_ids = {r["student_id"] for r in today_rows}
    absent = [s for s in students if s[0] not in marked_ids]
    if absent:
        st.subheader("Not yet marked present")
        st.table(pd.DataFrame(absent, columns=["student_id", "name", "_"])[["student_id", "name"]])

with tab2:
    if log:
        df = pd.DataFrame(log)
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "Download full log as CSV",
            df.to_csv(index=False),
            file_name="attendance_log.csv",
            mime="text/csv",
        )
    else:
        st.info("No attendance records yet.")

with tab3:
    total_sessions = st.number_input(
        "Total class sessions held so far (for % calculation)",
        min_value=1, value=1, step=1,
    )
    pct_data = get_attendance_percentage(total_sessions)
    if pct_data:
        df = pd.DataFrame(pct_data).sort_values("attendance_pct")
        st.dataframe(df, use_container_width=True)

        threshold = st.slider("Flag students below this attendance %", 0, 100, 75)
        defaulters = df[df["attendance_pct"] < threshold]
        if not defaulters.empty:
            st.warning(f"{len(defaulters)} student(s) below {threshold}% attendance")
            st.table(defaulters)
    else:
        st.info("No attendance data yet to calculate percentages.")

with tab4:
    st.subheader("Enroll yourself")
    st.caption(
        "Anyone with this link can enroll themselves here — no coding needed. "
        "Consent is required before any photo is processed."
    )

    new_id = st.text_input("Your ID (e.g. roll number or a short unique code)", key="enroll_id")
    new_name = st.text_input("Your full name", key="enroll_name")

    st.markdown("**Consent**")
    st.write(
        "By checking the box below, you agree that a face-derived numeric vector "
        "(not a stored photo) will be created from your webcam image and used only "
        "for attendance matching in this class. You can ask to have it deleted at "
        "any time — see the 'Manage Students' tab."
    )
    consent = st.checkbox("I understand and consent to enrollment", key="consent_box")

    if not consent:
        st.info("Check the consent box above to unlock the camera.")
    elif not new_id or not new_name:
        st.warning("Enter your ID and name first.")
    else:
        st.write("Take 3 photos: straight on, slightly left, slightly right.")
        shot1 = st.camera_input("Shot 1 — look straight at the camera", key="shot1")
        shot2 = st.camera_input("Shot 2 — turn slightly left", key="shot2")
        shot3 = st.camera_input("Shot 3 — turn slightly right", key="shot3")

        if st.button("Submit enrollment"):
            shots = [s for s in (shot1, shot2, shot3) if s is not None]
            if len(shots) < 1:
                st.error("Take at least one photo before submitting.")
            else:
                encodings = []
                for shot in shots:
                    img = Image.open(shot).convert("RGB")
                    arr = np.array(img)
                    locations = face_recognition.face_locations(arr)
                    if len(locations) != 1:
                        st.warning(f"Skipped a photo — found {len(locations)} face(s), need exactly 1.")
                        continue
                    enc = face_recognition.face_encodings(arr, locations)[0]
                    encodings.append(enc)

                if not encodings:
                    st.error("Could not detect a clear single face in any photo. Try again with better lighting.")
                else:
                    avg_encoding = np.mean(encodings, axis=0)
                    try:
                        add_student(new_id, new_name, avg_encoding, consent_given=True)
                        st.success(f"Enrolled {new_name} ({new_id}). You're all set for attendance recognition.")
                    except ValueError as e:
                        st.error(str(e))

with tab5:
    st.subheader("Manage enrolled students")
    st.caption("Anyone can view this list. Removing consent or deleting a record is irreversible in this session.")

    admin_rows = get_all_students_admin()
    if admin_rows:
        df = pd.DataFrame(admin_rows)
        df["consent_given"] = df["consent_given"].map({1: "Yes", 0: "Revoked"})
        st.dataframe(df, use_container_width=True)

        st.markdown("---")
        st.write("**Withdraw consent or remove a record**")
        target_id = st.selectbox(
            "Select a student ID",
            [r["student_id"] for r in admin_rows],
        )
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("Revoke consent (keeps attendance history)"):
                revoke_consent(target_id)
                st.success(f"Consent revoked for {target_id}. They'll no longer be recognized.")
                st.rerun()
        with col_b:
            if st.button("Delete entirely (removes profile)"):
                delete_student(target_id)
                st.success(f"Deleted {target_id} from the students table.")
                st.rerun()
    else:
        st.info("No students enrolled yet — use the Self-Enroll tab.")
