"""Stable face-recognition worker for the attendance camera service."""

def _ear(points):
    import numpy as np
    p=np.asarray(points,dtype=np.float64)
    a=np.linalg.norm(p[1]-p[5]); b=np.linalg.norm(p[2]-p[4]); c=np.linalg.norm(p[0]-p[3])
    return float((a+b)/(2*c)) if c else 0.0

def _normalise_known(students):
    import numpy as np
    known=[]; valid=[]
    for s in students or []:
        try:
            enc=np.asarray(s[2],dtype=np.float64).reshape(-1)
            if enc.size == 128 and np.isfinite(enc).all():
                known.append(enc); valid.append(s)
        except Exception:
            continue
    if not known:
        return np.empty((0,128),dtype=np.float64), []
    return np.vstack(known), valid

def _recognize(frame, students, tolerance=0.5, max_width=640):
    import cv2
    import numpy as np
    import face_recognition

    if frame is None or not students:
        return []

    h, w = frame.shape[:2]

    # Keep recognition fast, but give the detector enough pixels for faces.
    scale = min(1.0, max_width / float(max(1, w)))
    work = (
        cv2.resize(
            frame,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA
        )
        if scale < 1.0 else frame
    )

    rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)

    # Fast pass first.
    locations = face_recognition.face_locations(
        rgb,
        model="hog",
        number_of_times_to_upsample=1
    )

    # Strong fallback when the face is small / farther from camera.
    if not locations:
        locations = face_recognition.face_locations(
            rgb,
            model="hog",
            number_of_times_to_upsample=2
        )

    if not locations:
        return []

    encodings = face_recognition.face_encodings(
        rgb,
        locations,
        num_jitters=1,
        model="small"
    )

    if not encodings:
        return []

    known_encodings = []
    valid_students = []

    for student in students:
        try:
            enc = np.asarray(student[2], dtype=np.float64).reshape(-1)

            # face_recognition encodings must contain 128 values.
            if enc.size != 128:
                continue

            known_encodings.append(enc)
            valid_students.append(student)
        except Exception:
            continue

    if not known_encodings:
        return []

    known = np.asarray(known_encodings, dtype=np.float64)

    results = []

    # 0.60 is the face_recognition standard boundary and gives
    # significantly better real-world recognition than the previous 0.50.
    effective_tolerance = max(float(tolerance), 0.60)

    for location, encoding in zip(locations, encodings):
        distances = face_recognition.face_distance(known, encoding)

        if len(distances) == 0:
            continue

        best_index = int(np.argmin(distances))
        distance = float(distances[best_index])

        if distance > effective_tolerance:
            continue

        top, right, bottom, left = location

        if scale < 1.0:
            inverse = 1.0 / scale
            top = int(top * inverse)
            right = int(right * inverse)
            bottom = int(bottom * inverse)
            left = int(left * inverse)

        student = valid_students[best_index]

        results.append({
            "id": student[0],
            "name": student[1],
            "confidence": round(max(0.0, 1.0 - distance), 3),
            "distance": round(distance, 4),
            "location": (top, right, bottom, left)
        })

    return results

def _blink(frame,threshold=0.21):
    import cv2, face_recognition
    rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
    lm=face_recognition.face_landmarks(rgb)
    if not lm: return False
    item=lm[0]
    if 'left_eye' not in item or 'right_eye' not in item: return False
    return ((_ear(item['left_eye'])+_ear(item['right_eye']))/2.0)<threshold

def worker_main(in_q,out_q):
    import cv2, numpy as np
    while True:
        req=in_q.get()
        if req is None or req.get('op')=='stop': return
        rid=req.get('id')
        try:
            arr=np.frombuffer(req['jpeg'],dtype=np.uint8)
            frame=cv2.imdecode(arr,cv2.IMREAD_COLOR)
            if frame is None: raise RuntimeError('decode failed')
            op=req.get('op')
            if op=='recognize':
                result=_recognize(frame,req.get('students') or [],float(req.get('tolerance',0.5)),int(req.get('max_width',800)))
            elif op=='blink':
                result=_blink(frame,float(req.get('threshold',0.21)))
            elif op=='encode':
                import face_recognition
                locs=face_recognition.face_locations(frame,model='hog',number_of_times_to_upsample=1)
                if len(locs)!=1: result={'count':len(locs),'encoding':None}
                else:
                    enc=face_recognition.face_encodings(frame,locs,num_jitters=1,model='small')
                    result={'count':1,'encoding':enc[0].tolist() if enc else None}
            else: result=None
            out_q.put({'id':rid,'ok':True,'result':result})
        except Exception as exc:
            try: out_q.put({'id':rid,'ok':False,'error':f'{type(exc).__name__}: {exc}'})
            except Exception: pass
