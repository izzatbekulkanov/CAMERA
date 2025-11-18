import json
from channels.generic.websocket import AsyncWebsocketConsumer
from .models import Attendance
from .tasks import analyze_attendance_psychology
from django.utils import timezone
from asgiref.sync import sync_to_async

class PsychologyConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_group_name = 'psychology_updates'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()
        print("WebSocket: Connection accepted")

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        print(f"WebSocket: Disconnected with code {close_code}")

    async def receive(self, text_data):
        data = json.loads(text_data)
        action = data.get("action")
        if action != "start_analysis":
            return

        date_str = data.get("date")
        if date_str:
            from django.utils.dateparse import parse_date
            date_obj = parse_date(date_str)
        else:
            date_obj = timezone.localdate()

        attendances = await sync_to_async(list)(
            Attendance.objects.filter(date=date_obj).select_related('user')
        )

        for att in attendances:
            analyze_attendance_psychology.delay(att.id)
            # progress boshlanishi haqida xabar (optional)
            await self.send(text_data=json.dumps({
                "attendance_id": att.id,
                "user_id": att.user.id,
                "user_full_name": att.user.full_name,
                "progress": 0
            }))

    async def progress_update(self, event):
        await self.send(text_data=json.dumps({
            "attendance_id": event["attendance_id"],
            "user_id": event["user_id"],
            "user_full_name": event.get("user_full_name", "User"),
            "progress": event["progress"]
        }))

    async def analysis_completed(self, event):
        await self.send(text_data=json.dumps({
            "attendance_id": event["attendance_id"],
            "user_id": event["user_id"],
            "user_full_name": event.get("user_full_name", "User"),
            "dominant_emotion": event["dominant_emotion"],
            "stress_level": event["stress_level"],
            "energy_level": event["energy_level"],
            "mood_score": event["mood_score"],
            "summary_text": event["summary_text"],
            "completed": True
        }))
