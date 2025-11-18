# camera/consumers.py — TO‘LIQ TO‘G‘RILANGAN, XAVFSIZ, PROFESSIONAL VERSIYA

import asyncio
import json
import base64
import cv2
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from camera.tasks import get_camera, detect_faces
from attendance.models import Attendance
from django.utils import timezone
from collections import defaultdict


class CameraStreamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            self.camera_id = int(self.scope["url_route"]["kwargs"].get("camera_id", 0))
        except (ValueError, TypeError):
            await self.close(code=4001)
            return

        print(f"[VIDEO] Kamera {self.camera_id} ga ulanmoqda...")

        self.cam = await get_camera(self.camera_id)
        if not self.cam:
            print(f"[XATO] Kamera {self.camera_id} ochilmadi! Mavjud kameralar: 0, 1, 2... ni sinab ko‘ring.")
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": f"Kamera {self.camera_id} topilmadi yoki ishlamayapti!",
                "available": "Sinab ko‘ring: 0, 1, 2, 3..."
            }))
            await self.close(code=4002)
            return

        # Muvaffaqiyatli ulandi
        await self.accept()
        self.stream_task = asyncio.create_task(self.stream_video())
        print(f"[MUVOFFAQIYAT] Kamera {self.camera_id} muvaffaqiyatli ulandi!")

    async def disconnect(self, close_code):
        print(f"[VIDEO] Kamera {self.camera_id} uzildi → code: {close_code}")

        if hasattr(self, "stream_task") and self.stream_task:
            self.stream_task.cancel()

        if hasattr(self, "cam") and self.cam:
            self.cam["users"] -= 1
            # Kamera yopilishi avtomatik tasks.py da amalga oshiriladi
            # release_camera_if_unused() endi async emas, oddiy funksiya bo‘lishi kerak
            from camera.tasks import release_camera_if_unused
            asyncio.create_task(asyncio.to_thread(release_camera_if_unused, self.camera_id))

    async def stream_video(self):
        try:
            async for frame, faces in detect_faces(self.cam):
                if frame is None:
                    await asyncio.sleep(0.1)
                    continue

                # Sifat va tezlikni muvozanatlash
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                success, buffer = cv2.imencode('.jpg', frame, encode_param)
                if not success:
                    continue

                frame_b64 = base64.b64encode(buffer).decode()

                await self.send(text_data=json.dumps({
                    "type": "frame",
                    "frame": frame_b64,
                    "detected_faces": faces or []
                }, ensure_ascii=False))

                await asyncio.sleep(0.035)  # ~28-30 FPS

        except asyncio.CancelledError:
            print(f"[VIDEO] Stream bekor qilindi (kamera {self.camera_id})")
        except Exception as e:
            print(f"[XATO] Video oqimida xato: {e}")
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": "Video oqimi uzildi. Qayta ulaning."
            }))


# ===================================================================
# JONLI DAVOMAT — ALOHIDA WEBSOCKET (ws/attendance/live/)
# ===================================================================

@database_sync_to_async
def get_live_attendance_data():
    today = timezone.localdate()
    attendances = Attendance.objects.filter(
        date=today,
        is_present=True
    ).select_related('user').prefetch_related('attendancephoto_set__image').order_by('-last_seen')

    result = []
    seen_users = set()

    for att in attendances:
        user = att.user
        if user.id in seen_users:
            continue
        seen_users.add(user.id)

        photos = [p.image.url for p in att.attendancephoto_set.order_by('-captured_at')[:4] if p.image]
        extra = att.attendancephoto_set.count() - 4 if att.attendancephoto_set.count() > 4 else 0

        result.append({
            "user": {
                "id": user.student_id_number or user.employee_id_number or str(user.id),
                "full_name": user.full_name or user.username,
                "role": user.get_role_display() if hasattr(user, 'get_role_display') else "Xodim",
                "photo": user.image.url if user.image else None,
            },
            "entry_time": att.entry_time.strftime("%H:%M") if att.entry_time else "-",
            "last_seen": att.last_seen.strftime("%H:%M:%S"),
            "photos": photos,
            "extra_count": extra
        })

    return result


class LiveAttendanceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self.update_task = asyncio.create_task(self.send_updates())
        print("[ATTENDANCE] Yangi jonli davomat ulandi")

    async def disconnect(self, close_code):
        if hasattr(self, "update_task"):
            self.update_task.cancel()
        print(f"[ATTENDANCE] Ulanish uzildi → {close_code}")

    async def send_updates(self):
        try:
            while True:
                data = await get_live_attendance_data()
                await self.send(text_data=json.dumps({
                    "type": "live_attendance",
                    "count": len(data),
                    "users": data
                }, ensure_ascii=False))
                await asyncio.sleep(4.5)  # 4-5 sekund optimal
        except asyncio.CancelledError:
            print("[ATTENDANCE] Yangilanish bekor qilindi")