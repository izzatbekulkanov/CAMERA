# camera/tasks.py

import asyncio
import logging
import uuid

import cv2
import numpy as np
import requests
from asgiref.sync import sync_to_async
from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone

from attendance.models import Attendance, AttendancePhoto
from camera.models import Camera
from users.models import CustomUser

logger = logging.getLogger(__name__)

# ================== GLOBAL STATE ==================

EXIT_TIMEOUT = 180      # 3 daqiqa ko'rinmasa → chiqdi
PHOTO_INTERVAL = 20     # har 20 s da 1 ta rasm

ACTIVE_SESSIONS: dict[int, timezone.datetime] = {}      # user_id → last_seen
last_photo_cache: dict[int, timezone.datetime] = {}     # user_id → last_photo_time


# ================== ATTENDANCE & PHOTO ==================

@sync_to_async
def mark_exit(user: CustomUser) -> None:
    """EXIT_TIMEOUT dan oshsa → chiqish vaqtini belgilash."""
    today = timezone.localdate()
    att = Attendance.objects.filter(
        user=user,
        date=today,
        is_present=True,
    ).first()
    if not att:
        return

    now = timezone.now()
    att.exit_time = now
    att.is_present = False
    att.duration_minutes = max(
        1, int((att.exit_time - att.entry_time).total_seconds() // 60)
    )
    att.save()
    logger.info("[CHIQISH] %s → %s", user.full_name or user.username, att.exit_time)


async def auto_exit_detector() -> None:
    """Har 30 s da ACTIVE_SESSIONS ni tekshiradi va chiqishlarni belgilaydi."""
    logger.info("[AUTO EXIT] ishga tushdi")
    while True:
        await asyncio.sleep(30)
        now = timezone.now()

        expired_ids = [
            uid
            for uid, last_seen in list(ACTIVE_SESSIONS.items())
            if (now - last_seen).total_seconds() > EXIT_TIMEOUT
        ]

        for uid in expired_ids:
            try:
                user = await sync_to_async(CustomUser.objects.get)(id=uid)
            except CustomUser.DoesNotExist:
                ACTIVE_SESSIONS.pop(uid, None)
                continue

            await mark_exit(user)
            ACTIVE_SESSIONS.pop(uid, None)


@sync_to_async
def process_recognition(user: CustomUser, face_crop: np.ndarray) -> None:
    """
    Yuz tanilganda:
    - Attendance (entry/last_seen/is_present) yangilanadi
    - ACTIVE_SESSIONS update bo'ladi
    - FOTO interval bilan saqlanadi
    """
    today = timezone.localdate()
    now = timezone.now()

    att, created = Attendance.objects.get_or_create(
        user=user,
        date=today,
        defaults={"entry_time": now, "last_seen": now, "is_present": True},
    )

    if not created:
        att.last_seen = now
        att.is_present = True
        att.save(update_fields=["last_seen", "is_present"])

    ACTIVE_SESSIONS[user.id] = now
    logger.debug("[TANISH] %s → last_seen=%s", user.username, now)

    last_time = last_photo_cache.get(user.id)
    if last_time and (now - last_time).total_seconds() <= PHOTO_INTERVAL:
        return

    try:
        ok, buffer = cv2.imencode(".jpg", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    except Exception as exc:
        logger.error("[FOTO] %s encodlash xato: %s", user.id, exc)
        return

    if not ok:
        return

    filename = f"{user.username or 'user'}_{uuid.uuid4().hex[:8]}.jpg"
    photo = AttendancePhoto(attendance=att)
    photo.image.save(filename, ContentFile(buffer.tobytes()))
    last_photo_cache[user.id] = now

    logger.info("[FOTO] %s uchun rasm saqlandi: %s", user.full_name or user.username, filename)


# ================== BACKGROUND STARTER ==================

def start_background_tasks() -> None:
    """ASGI ishga tushganda auto_exit_detector ni loop ga qo'shish."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.create_task(auto_exit_detector())
    logger.info("[BACKGROUND] auto_exit_detector ishga tushdi")


# ================== CELERY TASK: HEALTH CHECK ==================

@shared_task
def check_camera_health() -> None:
    """
    Celery beat orqali kameralarning ish holatini tekshiradi
    va Camera.is_online maydonini yangilaydi.
    """
    cameras = Camera.objects.filter(is_active=True)

    for cam in cameras:
        url = f"http://{cam.ip}:{cam.port}/"
        try:
            r = requests.get(url, auth=(cam.username, cam.password), timeout=5)
        except Exception as exc:
            if cam.is_online:
                cam.is_online = False
                cam.save(update_fields=["is_online"])
            logger.warning("[HEALTH] %s ulanmagan: %s", cam.ip, exc)
            continue

        if r.status_code in (200, 401, 302):
            if not cam.is_online:
                cam.is_online = True
                cam.save(update_fields=["is_online"])
                logger.info("[HEALTH] %s → ONLINE", cam.ip)
        else:
            if cam.is_online:
                cam.is_online = False
                cam.save(update_fields=["is_online"])
                logger.info("[HEALTH] %s → OFFLINE (status=%s)", cam.ip, r.status_code)
