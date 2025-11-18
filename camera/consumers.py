# camera/consumers.py — TO‘LIQ YANGI VERSIYA (tasks.py bilan 100% mos!)

import asyncio
import json
import base64
import cv2
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from camera.tasks import get_camera, release_camera_if_unused, detect_faces
from attendance.models import Attendance, AttendancePhoto
from django.utils import timezone
from collections import defaultdict


class CameraStreamConsumer(AsyncWebsocketConsumer):
    """
    Faqat video oqimi + real-time yuz ramkalari
    Hech qanday davomat ma'lumoti yubormaydi → juda tez va silliq
    """
    async def connect(self):
        try:
            self.camera_id = int(self.scope["url_route"]["kwargs"]["camera_id"])
        except (ValueError, KeyError):
            print(f"[XATO] Noto‘g‘ri kamera ID")
            await self.close(code=4001)
            return

        self.cam = await get_camera(self.camera_id)
        if not self.cam:
            print(f"[XATO] Kamera {self.camera_id} ochilmadi")
            await self.close(code=4002)
            return

        # Video oqimi boshlanadi
        self.stream_task = asyncio.create_task(self.stream_video())
        await self.accept()
        print(f"[VIDEO] Kamera {self.camera_id} ga muvaffaqiyatli ulandi")

    async def disconnect(self, close_code):
        print(f"[VIDEO] Kamera {self.camera_id} uzildi (code: {close_code})")
        if hasattr(self, "stream_task") and self.stream_task:
            self.stream_task.cancel()

        if self.cam:
            self.cam["users"] -= 1
            await release_camera_if_unused(self.camera_id)

    async def stream_video(self):
        """
        Har bir frame → frontendga yuboriladi
        Yuz ramkalari doimiy → hech qachon yo‘qolmaydi!
        """
        try:
            async for frame, faces in detect_faces(self.cam):
                if frame is None:
                    await asyncio.sleep(0.1)
                    continue

                # Optimal sifat va hajm
                success, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if not success:
                    continue

                frame_b64 = base64.b64encode(buffer).decode()

                await self.send(text_data=json.dumps({
                    "type": "frame",
                    "frame": frame_b64,
                    "detected_faces": faces or []  # Har doim list bo‘lsin
                }, ensure_ascii=False))

                # ~25-30 FPS
                await asyncio.sleep(0.035)

        except asyncio.CancelledError:
            print(f"[VIDEO] Stream task bekor qilindi (kamera {self.camera_id})")
        except Exception as e:
            print(f"[XATO] Video streamda xato: {e}")


# =============================================================================
# JONLI DAVOMAT — ALOHIDA WEBSOCKET (ws/attendance/live/)
# =============================================================================

@database_sync_to_async
def get_grouped_attendance():
    """
    Bugun binoda bo‘lganlar → guruhlangan + rasmlar
    Har bir odam faqat 1 marta chiqadi
    """
    today = timezone.localdate()
    attendances = Attendance.objects.filter(
        date=today,
        is_present=True
    ).select_related('user').prefetch_related('attendancephoto_set').order_by('-last_seen')

    grouped = defaultdict(lambda: {
        "user": None,
        "entry_time": "-",
        "last_seen": "-",
        "photos": []
    })

    for att in attendances:
        u = att.user
        key = u.id

        if not grouped[key]["user"]:
            grouped[key]["user"] = {
                "id": u.student_id_number or u.employee_id_number or str(u.id),
                "full_name": u.full_name or u.username,
                "role": getattr(u, 'get_role_display', lambda: u.role or "Noma'lum")(),
                "photo": u.image.url if u.image and hasattr(u.image, 'url') else None,
            }
            grouped[key]["entry_time"] = att.entry_time.strftime("%H:%M") if att.entry_time else "-"
            grouped[key]["last_seen"] = att.last_seen.strftime("%H:%M:%S")

        # Eng yangi 5 ta rasm
        for photo in att.attendancephoto_set.order_by('-captured_at')[:5]:
            if photo.image and photo.image.url not in grouped[key]["photos"]:
                grouped[key]["photos"].append(photo.image.url)

    result = []
    for data in grouped.values():
        photos = data["photos"][:4]
        result.append({
            "user": data["user"],
            "entry_time": data["entry_time"],
            "last_seen": data["last_seen"],
            "photos": photos,
            "extra_count": len(data["photos"]) - 4 if len(data["photos"]) > 4 else 0
        })

    print(f"[ATTENDANCE] {len(result)} ta odam yuborildi (guruhlangan)")
    return result


class LiveAttendanceConsumer(AsyncWebsocketConsumer):
    """
    Alohida WebSocket → faqat davomat ma'lumotlari
    Video oqimiga hech qanday ta'sir qilmaydi
    """
    async def connect(self):
        await self.accept()
        self.send_task = asyncio.create_task(self.send_live_updates())
        print("[ATTENDANCE] Yangi klient ulandi → ws/attendance/live/")

    async def disconnect(self, close_code):
        if hasattr(self, "send_task"):
            self.send_task.cancel()
        print(f"[ATTENDANCE] Klient uzildi (code: {close_code})")

    async def send_live_updates(self):
        """
        Har 4 sekundda yangi ma'lumot yuboradi
        Frontendda ro‘yxat + rasmlar yangilanadi
        """
        try:
            while True:
                data = await get_grouped_attendance()
                await self.send(text_data=json.dumps({
                    "type": "grouped_attendance",
                    "count": len(data),
                    "users": data
                }, ensure_ascii=False))
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            print("[ATTENDANCE] Task bekor qilindi")