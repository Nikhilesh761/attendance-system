"""Tiny supervisor that keeps camera_service.py alive.

Streamlit talks to this process. The supervisor restarts camera_service if a
native OpenCV crash or other fatal process exit occurs.
"""
import os, subprocess, sys, time

HERE=os.path.dirname(os.path.abspath(__file__))
PORT=os.environ.get('ATTENDANCE_CAMERA_PORT','8777')

if __name__=='__main__':
    while True:
        env=os.environ.copy(); env['ATTENDANCE_CAMERA_PORT']=PORT
        proc=subprocess.Popen([sys.executable, os.path.join(HERE,'camera_service.py')], cwd=HERE, env=env)
        code=proc.wait()
        time.sleep(0.6)
