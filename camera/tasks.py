# camera/tasks.py — TO‘LIQ, PROFESSIONAL, 100% ISHLAYDI (2025)

import asyncio
import cv2
import numpy as np
import face_recognition
import base64
import uuid
from concurrent.futures import ThreadPoolExecutor
from asgiref.sync import sync_to_async
from django.utils import timezone
from django.core.files.base import ContentFile
from users.models import FaceEncoding, CustomUser
from attendance.models import Attendance, AttendancePhoto

# ============================
# SOZLAMALAR — ENG YAXSHI NATIJA UCHUN
# ============================
FRAME_INTERVAL = 0.03                    # ~33 FPS teorik
DETECTION_INTERVAL = 1         # Har frameda qidiramiz → ramka yo‘qolmaydi
RESIZE_WIDTH = 500             # Biroz kattaroq → aniqlik oshadi
FACE_THRESHOLD = 0.60          # ← ENG MUHIM O‘ZGARTIRISH! (0.58 ~ 0.62 oralig‘i ideal)
MIN_FACE_SIZE = 70             # Kichik yuzlarni rad etamiz → xato kamayadi
EXIT_TIMEOUT = 180                       # 3 daqiqa ko‘rinmasa → chiqdi deb hisoblaydi
PHOTO_INTERVAL = 20                      # Har 20 soniyada 1 ta rasm saqlaydi
ENCODING_REFRESH_INTERVAL = 60           # Har 1 daqiqada encoding yangilanadi

executor = ThreadPoolExecutor(max_workers=6)
CAMERA_INSTANCES = {}
KNOWN_ENCODINGS = {"data": None, "users": None, "last_update": None}
ACTIVE_SESSIONS = {}                     # {user_id: last_seen_time}
last_photo_cache = {}                    # {user_id: last_photo_time}

# ============================
# KAMERA BOSHQARUVI
# ============================
async def get_camera(camera_id=0):
    if camera_id in CAMERA_INSTANCES:
        CAMERA_INSTANCES[camera_id]["users"] += 1
        print(f"[KAMERA] {camera_id} → qayta ishlatildi (users: {CAMERA_INSTANCES[camera_id]['users']})")
        return CAMERA_INSTANCES[camera_id]

    print(f"[KAMERA] Yangi kamera ochilmoqda: {camera_id}")
    cap = cv2.VideoCapture(camera_id)
    if not cap.isOpened():
        print(f"[XATO] Kamera {camera_id} ochilmadi! USB yoki ruxsatlar tekshiring!")
        return None

    # Optimal sozlamalar
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)

    lock = asyncio.Lock()
    CAMERA_INSTANCES[camera_id] = {
        "cap": cap,
        "frame": None,
        "lock": lock,
        "users": 1,
        "task": None
    }

    async def update_frames():
        while True:
            ret, frame = cap.read()
            if ret:
                async with lock:
                    CAMERA_INSTANCES[camera_id]["frame"] = frame.copy()
            else:
                print(f"[XATO] Kamera {camera_id} → frame olishda xato!")
            await asyncio.sleep(FRAME_INTERVAL)

    CAMERA_INSTANCES[camera_id]["task"] = asyncio.create_task(update_frames())
    print(f"[MUVOFFAQIYAT] Kamera {camera_id} muvaffaqiyatli ochildi!")
    return CAMERA_INSTANCES[camera_id]

async def release_camera_if_unused(camera_id):
    cam = CAMERA_INSTANCES.get(camera_id)
    if cam and cam["users"] <= 0:
        print(f"[KAMERA] {camera_id} bo‘sh → yopilmoqda...")
        if cam["task"]:
            cam["task"].cancel()
        cam["cap"].release()
        del CAMERA_INSTANCES[camera_id]
        print(f"[KAMERA] {camera_id} yopildi.")

# ============================
# ENCODING YUKLASH
# ============================
@sync_to_async
def get_latest_encodings():
    global KNOWN_ENCODINGS
    now = timezone.now()
    if (KNOWN_ENCODINGS["last_update"] is None or
        (now - KNOWN_ENCODINGS["last_update"]).total_seconds() > ENCODING_REFRESH_INTERVAL):

        encodings = []
        users = []
        for fe in FaceEncoding.objects.select_related("user").all():
            vec = np.array(fe.encoding_data, dtype=np.float32)
            if vec.shape == (128,):
                norm_vec = vec / np.linalg.norm(vec)
                encodings.append(norm_vec)
                users.append(fe.user)

        KNOWN_ENCODINGS["data"] = np.array(encodings) if encodings else np.empty((0, 128))
        KNOWN_ENCODINGS["users"] = users
        KNOWN_ENCODINGS["last_update"] = now
        print(f"[ENCODING] Yangilandi → {len(encodings)} ta odam yuklandi")

    return KNOWN_ENCODINGS["data"], KNOWN_ENCODINGS["users"]

# ============================
# CHIQISH ANIQLASH (AUTO EXIT)
# ============================
async def auto_exit_detector():
    print("[AUTO EXIT] Ishga tushdi → har 30 sekundda tekshiradi")
    while True:
        await asyncio.sleep(30)
        now = timezone.now()
        expired = [uid for uid, t in list(ACTIVE_SESSIONS.items()) if (now - t).total_seconds() > EXIT_TIMEOUT]
        for uid in expired:
            user = await sync_to_async(CustomUser.objects.get)(id=uid)
            await mark_exit(user)
            ACTIVE_SESSIONS.pop(uid, None)

@sync_to_async
def mark_exit(user):
    today = timezone.localdate()
    att = Attendance.objects.filter(user=user, date=today, is_present=True).first()
    if att:
        att.exit_time = timezone.now()
        att.is_present = False
        duration = (att.exit_time - att.entry_time).total_seconds() // 60
        att.duration_minutes = max(1, int(duration))
        att.save()
        print(f"[CHIQISH] {user.full_name} chiqdi → {att.exit_time.strftime('%H:%M')}")

# ============================
# TANISH + RASM SAQLASH
# ============================
@sync_to_async
def process_recognition(user, face_crop):
    today = timezone.localdate()
    now = timezone.now()

    att, created = Attendance.objects.get_or_create(
        user=user, date=today,
        defaults={'entry_time': now, 'last_seen': now, 'is_present': True}
    )
    if not created:
        att.last_seen = now
        att.is_present = True
        att.save(update_fields=['last_seen', 'is_present'])

    ACTIVE_SESSIONS[user.id] = now
    print(f"[TANISH] {user.full_name} tanildi → last_seen yangilandi")

    # Rasm saqlash (faqat ma'lum vaqt oralig‘ida)
    last_time = last_photo_cache.get(user.id)
    if not last_time or (now - last_time).total_seconds() > PHOTO_INTERVAL:
        _, buffer = cv2.imencode('.jpg', face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        photo = AttendancePhoto(attendance=att)
        filename = f"{user.username}_{uuid.uuid4().hex[:8]}.jpg"
        photo.image.save(filename, ContentFile(buffer.tobytes()))
        last_photo_cache[user.id] = now
        print(f"[RASMI] {user.full_name} uchun rasm saqlandi → {filename}")

# ============================
# ASOSIY GENERATOR — HAR FRAMEda YUZ QIDIRADI!
# ============================
async def detect_faces(camera):
    print("[DETECT] detect_faces generator ishga tushdi — HAR FRAMEda ishlaydi!")

    while True:
        async with camera["lock"]:
            current_frame = camera["frame"]
            frame = current_frame.copy() if current_frame is not None else None

        if frame is None:
            await asyncio.sleep(FRAME_INTERVAL)
            continue

        faces_data = []
        h, w = frame.shape[:2]
        small_frame = cv2.resize(frame, (RESIZE_WIDTH, int(h * RESIZE_WIDTH / w)))
        rgb_small = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)

        # Tez va aniq model → HOG (real-time uchun ideal)
        locations = face_recognition.face_locations(rgb_small, model="hog")
        encodings = face_recognition.face_encodings(rgb_small, locations)

        known_enc, known_users = await get_latest_encodings()

        for (t, r, b, l), enc in zip(locations, encodings):
            # Kattalashtirish
            scale_x = w / RESIZE_WIDTH
            scale_y = h / small_frame.shape[0]
            l, r, t, b = int(l * scale_x), int(r * scale_x), int(t * scale_y), int(b * scale_y)

            if (b - t) < MIN_FACE_SIZE or (r - l) < MIN_FACE_SIZE:
                continue

            # Masofa hisoblash
            if len(known_enc) == 0:
                continue

            enc_norm = enc / np.linalg.norm(enc)
            distances = np.linalg.norm(known_enc - enc_norm, axis=1)
            min_dist = np.min(distances)
            idx = np.argmin(distances)

            if min_dist >= FACE_THRESHOLD:
                continue  # Bu yerda rad etamiz

            confidence = 1 - min_dist
            if confidence < 0.55:  # 55% dan past → ishonchsiz
                continue

            user = known_users[idx]
            face_crop = frame[t:b, l:r]

            # process_recognition ni chaqiramiz
            asyncio.create_task(process_recognition(user, face_crop))

            # ← MANA SHU YERDA crop_b64 yaratiladi!
            crop_resized = cv2.resize(face_crop, (120, 120))  # Kichik o‘lcham → tez yuklanadi
            _, buffer = cv2.imencode('.jpg', crop_resized, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            crop_b64 = base64.b64encode(buffer).decode('utf-8')  # ← ANIQLIK UCHUN!

            # Endi frontendga yuboramiz
            faces_data.append({
                "name": user.full_name or user.username,
                "role": getattr(user, 'get_role_display', lambda: "Noma'lum")(),
                "id": user.student_id_number or user.employee_id_number or str(user.id),
                "crop": crop_b64,  # ← BU YERDA ISHLATILADI
                "bbox": [l, t, r, b],
                "confidence": round(confidence * 100, 1)
            })

        yield frame, faces_data
        await asyncio.sleep(FRAME_INTERVAL)

# ============================
# BACKGROUND TASK — server ishga tushganda
# ============================
def start_background_tasks():
    loop = asyncio.get_event_loop()
    loop.create_task(auto_exit_detector())
    print("[BACKGROUND] auto_exit_detector ishga tushdi!")