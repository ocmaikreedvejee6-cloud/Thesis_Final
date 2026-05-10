import cv2
import numpy as np
import time
import os
import smtplib
import requests
import threading
import pickle
import queue
from datetime import datetime
from email.message import EmailMessage
from flask import Flask, Response
from pyngrok import ngrok

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ================= CONFIG =================
SCOPES = ['https://www.googleapis.com/auth/drive.file']

TELEGRAM_TOKEN = "8490765768:AAFU-Vpi0HAiS5_2V2mcboWYeiG8W4neiVE"
CHAT_ID = "7175315173"

EMAIL_ADDRESS = "ocmaikreedvejee1@gmail.com"
EMAIL_APP_PASSWORD = "zpakcoctznasrirq"
RECEIVER_EMAIL = "ocmaikreedvejee6@gmail.com"

NGROK_AUTH_TOKEN = "3CuyBmODW6s830X8lYEvc1Hnh7O_GxAh2wGfQpMeayQ5jKfG"

GDRIVE_FOLDER_ID = "1UsVEk8AbZZjS8bonWxDp5M2_PQykhEx5"

VIDEO_TIMEOUT = 10
TELEGRAM_COOLDOWN = 5
EMAIL_COOLDOWN = 15

# ================= FIX: Add recording FPS constant =================
RECORDING_FPS = 20  # Changed from hardcoded 20 to a constant

# ================= GLOBALS =================
frame_global = None
lock = threading.Lock()

cap = None
recording = False
video_writer = None
current_video_path = None

last_intruder_time = 0
last_telegram_time = 0
last_email_time = 0
STREAM_URL = None

# ================= STABLE TRACKING =================
last_boxes = []
last_box_time = 0
box_timeout = 1.0

# ================= QUEUE FOR ASYNC TASKS =================
task_queue = queue.Queue()
upload_queue = queue.Queue()

# ================= DETECTORS =================
face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

hog = cv2.HOGDescriptor()
hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

fgbg = cv2.createBackgroundSubtractorMOG2()

# ================= NGROK =================
ngrok.set_auth_token(NGROK_AUTH_TOKEN)

# ================= GOOGLE DRIVE AUTH =================
def authenticate_google_drive():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return build('drive', 'v3', credentials=creds)

# ================= BACKGROUND WORKER THREADS =================
def worker_telegram():
    """Background worker for Telegram messages"""
    while True:
        try:
            image_path, caption = task_queue.get(timeout=1)
            if image_path and os.path.exists(image_path):
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                with open(image_path, 'rb') as photo:
                    files = {'photo': photo}
                    data = {'chat_id': CHAT_ID, 'caption': caption}
                    requests.post(url, files=files, data=data, timeout=10)
                try:
                    os.remove(image_path)
                except:
                    pass
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Telegram error: {e}")

def worker_email():
    """Background worker for Email alerts"""
    while True:
        try:
            image_path, video_path = task_queue.get(timeout=1)
            if image_path and os.path.exists(image_path):
                msg = EmailMessage()
                msg['Subject'] = f'Intruder Alert - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
                msg['From'] = EMAIL_ADDRESS
                msg['To'] = RECEIVER_EMAIL
                msg.set_content(f'Intruder detected at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
                
                with open(image_path, 'rb') as img:
                    msg.add_attachment(img.read(), maintype='image', subtype='jpeg', filename=os.path.basename(image_path))
                
                if video_path and os.path.exists(video_path):
                    with open(video_path, 'rb') as video:
                        msg.add_attachment(video.read(), maintype='video', subtype='x-msvideo', filename=os.path.basename(video_path))
                
                with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                    smtp.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
                    smtp.send_message(msg)
                
                try:
                    os.remove(image_path)
                except:
                    pass
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Email error: {e}")

def worker_gdrive():
    """Background worker for Google Drive uploads"""
    service = None
    while True:
        try:
            video_path = upload_queue.get(timeout=1)
            if video_path and os.path.exists(video_path):
                if service is None:
                    service = authenticate_google_drive()
                
                file_name = os.path.basename(video_path)
                file_metadata = {
                    'name': file_name,
                    'parents': [GDRIVE_FOLDER_ID]
                }
                media = MediaFileUpload(video_path, resumable=True)
                service.files().create(body=file_metadata, media_body=media, fields='id').execute()
                print(f"Uploaded to GDrive: {file_name}")
        except queue.Empty:
            continue
        except Exception as e:
            print(f"GDrive error: {e}")

# ================= CAMERA =================
def connect_camera():
    global cap
    while True:
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            cap.set(3, 640)
            cap.set(4, 360)
            print("Camera connected")
            return
        time.sleep(2)

# ================= RECORDING =================
def start_recording(frame):
    global recording, video_writer, current_video_path

    os.makedirs("videos", exist_ok=True)
    os.makedirs("snapshots", exist_ok=True)

    filename = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_video_path = f"videos/intruder_{filename}.avi"

    fourcc = cv2.VideoWriter_fourcc(*"XVID")
    # ================= FIX: Use RECORDING_FPS constant =================
    video_writer = cv2.VideoWriter(current_video_path, fourcc, RECORDING_FPS, (640, 360))

    recording = True

    snap = f"snapshots/intruder_{filename}.jpg"
    cv2.imwrite(snap, frame)

    print("Recording started")
    
    return snap

def stop_recording():
    global recording, video_writer, current_video_path

    if video_writer:
        video_writer.release()
        video_writer = None

    recording = False
    print("Recording stopped")
    
    if current_video_path and os.path.exists(current_video_path):
        upload_queue.put(current_video_path)

    current_video_path = None

# ================= FLASK =================
app = Flask(__name__)

def generate_frames():
    global frame_global

    while True:
        with lock:
            if frame_global is None:
                continue
            frame = frame_global.copy()

        _, buffer = cv2.imencode(".jpg", frame)

        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' +
               buffer.tobytes() + b'\r\n')

@app.route('/')
def video_feed():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# ================= TELEGRAM =================
def send_telegram_photo_async(image_path, caption=""):
    task_queue.put((image_path, caption))

def send_telegram_message_async(message):
    def send():
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {'chat_id': CHAT_ID, 'text': message}
        try:
            requests.post(url, data=data, timeout=10)
        except:
            pass
    threading.Thread(target=send, daemon=True).start()

# ================= EMAIL =================
def send_email_alert_async(image_path, video_path=None):
    task_queue.put((image_path, video_path))

# ================= MAIN =================
def main():
    global frame_global, recording, last_intruder_time, last_boxes, last_box_time, STREAM_URL
    global last_telegram_time, last_email_time

    threading.Thread(target=worker_telegram, daemon=True).start()
    threading.Thread(target=worker_email, daemon=True).start()
    threading.Thread(target=worker_gdrive, daemon=True).start()

    connect_camera()

    tunnel = ngrok.connect(5000, "http")
    STREAM_URL = tunnel.public_url
    print("Live:", STREAM_URL)
    
    send_telegram_message_async(f"🚨 Security System Active!\nLive Stream: {STREAM_URL}")

    last_telegram_snapshot_time = 0
    last_email_snapshot_time = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            connect_camera()
            continue

        frame = cv2.resize(frame, (640, 360))

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, timestamp, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)

        fgmask = fgbg.apply(frame)
        _, fgmask = cv2.threshold(fgmask, 250, 255, cv2.THRESH_BINARY)
        fgmask = cv2.erode(fgmask, None, iterations=2)
        fgmask = cv2.dilate(fgmask, None, iterations=2)

        motion_pixels = cv2.countNonZero(fgmask)

        faces = []
        current_boxes = []
        person_detected = False

        if motion_pixels > 500:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.3, 5)

            boxes, weights = hog.detectMultiScale(
                frame,
                winStride=(8, 8),
                padding=(16, 16),
                scale=1.1,
                hitThreshold=0.1
            )

            for i, (x, y, w, h) in enumerate(boxes):
                if w < 60 or h < 120:
                    continue
                aspect_ratio = h / float(w)
                if aspect_ratio < 1.5:
                    continue
                if weights[i] > 0.3:
                    current_boxes.append((x, y, w, h))
                    person_detected = True

        now = time.time()

        if len(current_boxes) > 0:
            last_boxes = current_boxes
            last_box_time = now
        elif now - last_box_time < box_timeout:
            current_boxes = last_boxes

        for (x, y, w, h) in current_boxes:
            cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 0), 2)

        for (x, y, w, h) in faces:
            cv2.rectangle(frame, (x,y), (x+w,y+h), (0,0,255), 2)

        intruder = (motion_pixels > 500) and (len(faces) > 0 or person_detected)

        with lock:
            frame_global = frame.copy()

        if intruder and (now - last_intruder_time > 3):
            last_intruder_time = now

            if not recording:
                snapshot_path = start_recording(frame)
                
                if snapshot_path and (now - last_telegram_time >= TELEGRAM_COOLDOWN):
                    send_telegram_photo_async(snapshot_path, f"🚨 INTRUDER DETECTED! {timestamp}")
                    last_telegram_time = now
                    last_telegram_snapshot_time = now
                
                if snapshot_path and (now - last_email_time >= EMAIL_COOLDOWN):
                    send_email_alert_async(snapshot_path, None)
                    last_email_time = now
                    last_email_snapshot_time = now

        if intruder and recording:
            if now - last_telegram_snapshot_time >= TELEGRAM_COOLDOWN:
                temp_snap = f"snapshots/telegram_temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                cv2.imwrite(temp_snap, frame)
                send_telegram_photo_async(temp_snap, f"⚠️ Intruder still present - {timestamp}")
                last_telegram_snapshot_time = now
                last_telegram_time = now
            
            if now - last_email_snapshot_time >= EMAIL_COOLDOWN:
                temp_snap = f"snapshots/email_temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                cv2.imwrite(temp_snap, frame)
                send_email_alert_async(temp_snap, None)
                last_email_snapshot_time = now
                last_email_time = now

        if recording:
            video_writer.write(frame)

        if recording and (now - last_intruder_time > VIDEO_TIMEOUT):
            stop_recording()

        # ================= FIX: Changed from 0.03 to 0.05 seconds =================
        time.sleep(1.0 / RECORDING_FPS)  # 0.05 seconds for 20 FPS

# ================= START =================
if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main()
