import cv2
cap = cv2.VideoCapture(4)
ret, frame = cap.read()
if ret:
    print("Index 4 works — phone camera detected")
else:
    print("Index 4 failed")
cap.release()