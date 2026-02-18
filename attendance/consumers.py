import json
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone
from asgiref.sync import sync_to_async
from django.utils.dateparse import parse_date
import asyncio

from .models import Attendance
from .tasks import analyze_attendance_psychology


class PsychologyConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_group_name = "psychology_updates"
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        print("WebSocket: Connection accepted")

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        print(f"WebSocket: Disconnected with code {close_code}")

    async def receive(self, text_data):
        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            return

        if data.get("action") != "start_analysis":
            return

        # Sana parse
        date_str = data.get("date")
        date_obj = parse_date(date_str) if date_str else None
        if not date_obj:
            date_obj = timezone.localdate()

        # Shu kundagi attendances
        attendances = await sync_to_async(list)(
            Attendance.objects.filter(date=date_obj).select_related("user")
        )

        # Hech narsa topilmasa clientga xabar beramiz
        if not attendances:
            await self.send(text_data=json.dumps({
                "error": "Bu sana uchun attendance topilmadi",
                "date": str(date_obj),
            }))
            return

        # Har attendance uchun task
        for att in attendances:
            analyze_attendance_psychology.delay(att.id)

            # Har bir user uchun boshlang'ich progress (0)
            await self.send(text_data=json.dumps({
                "attendance_id": att.id,
                "user_id": att.user.id,
                "user_full_name": getattr(att.user, "full_name", "User"),
                "progress": 0,
                "started": True,
                "date": str(date_obj),
            }))

    async def progress_update(self, event):
        # event: tasks.py group_send yuborgan dict
        payload = {
            "attendance_id": event.get("attendance_id"),
            "user_id": event.get("user_id"),
            "user_full_name": event.get("user_full_name", "User"),
            "progress": event.get("progress", 0),
        }
        await self.send(text_data=json.dumps(payload))

    async def analysis_completed(self, event):
        # Yangi fieldlar bilan to'liq response
        payload = {
            "attendance_id": event.get("attendance_id"),
            "user_id": event.get("user_id"),
            "user_full_name": event.get("user_full_name", "User"),

            "dominant_emotion": event.get("dominant_emotion"),
            "stress_level": event.get("stress_level"),
            "energy_level": event.get("energy_level"),
            "mood_score": event.get("mood_score"),
            "summary_text": event.get("summary_text"),

            # Qo'shimcha insightlar (agar tasks.py yuborsa)
            "emotion_probs": event.get("emotion_probs"),
            "confidence": event.get("confidence"),
            "stability": event.get("stability"),
            "negative_ratio": event.get("negative_ratio"),
            "positive_ratio": event.get("positive_ratio"),
            "neutral_ratio": event.get("neutral_ratio"),
            "valence": event.get("valence"),
            "arousal": event.get("arousal"),
            "photo_count": event.get("photo_count"),
            "face_quality": event.get("face_quality"),

            "completed": True
        }
        await self.send(text_data=json.dumps(payload))


class ServiceLogConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.service_name = self.scope["url_route"]["kwargs"]["service_name"]
        await self.accept()
        self.proc = None
        self.task = asyncio.create_task(self.stream_logs())

    async def disconnect(self, close_code):
        if self.task:
            self.task.cancel()

        if self.proc:
            try:
                self.proc.terminate()
            except:
                pass

    async def stream_logs(self):
        """
        journalctl -u <service> -f --no-pager
        """
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "journalctl",
                "-u", self.service_name,
                "-f",
                "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break

                text = line.decode(errors="ignore").strip()
                await self.send(text_data=json.dumps({"line": text}))

        except asyncio.CancelledError:
            pass

        except Exception as exc:
            await self.send(text_data=json.dumps({"error": str(exc)}))