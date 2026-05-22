import json

try:
    from channels.generic.websocket import AsyncWebsocketConsumer
    from channels.layers import get_channel_layer
    CHANNELS_AVAILABLE = True
except ImportError:
    CHANNELS_AVAILABLE = False
    AsyncWebsocketConsumer = None

from django.utils import timezone
from asgiref.sync import sync_to_async
from django.utils.dateparse import parse_date
import asyncio

from .models import Attendance

try:
    from .tasks import analyze_attendance_psychology
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False


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
    """
    Enhanced WebSocket consumer for real-time log streaming.

    Validates service names against ALLOWED_SERVICES, supports
    historical log retrieval, and ensures proper subprocess cleanup.

    Requirements covered:
    - 1.3: Reject WebSocket for services not in ALLOWED_SERVICES (code 4003)
    - 4.1: Stream logs starting with last 50 lines
    - 4.2: Send log lines as JSON with type="stream"
    - 4.3: Terminate journalctl subprocess on disconnect within 5 seconds
    - 4.4: Handle get_history command (default 100, max 500 lines)
    - 4.7: Reject non-allowed services with close code 4003
    - 4.8: Send error and close if journalctl exits unexpectedly
    - 6.2: Reject unauthenticated WebSocket connections with code 4001
    """

    async def connect(self):
        from .services import ALLOWED_SERVICES

        self.service_name = self.scope["url_route"]["kwargs"]["service_name"]
        self.proc = None
        self.task = None

        # Reject unauthenticated users with close code 4001
        user = self.scope.get("user")
        if not user or user.is_anonymous:
            await self.close(code=4001)
            return

        # Reject connections for services not in the allowed list
        if self.service_name not in ALLOWED_SERVICES:
            await self.close(code=4003)
            return

        await self.accept()
        self.task = asyncio.create_task(self.stream_logs())

    async def disconnect(self, close_code):
        # Cancel the streaming task
        if hasattr(self, "task") and self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

        # Terminate the journalctl subprocess
        if hasattr(self, "proc") and self.proc:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    self.proc.kill()
                except (ProcessLookupError, OSError):
                    pass

    async def receive(self, text_data):
        """Handle client commands like requesting historical logs."""
        try:
            data = json.loads(text_data)
        except (json.JSONDecodeError, TypeError):
            return

        if data.get("action") == "get_history":
            try:
                lines_count = int(data.get("lines", 100))
            except (ValueError, TypeError):
                lines_count = 100
            # Cap at 500 lines maximum, minimum 1
            lines_count = max(1, min(lines_count, 500))
            await self.send_history(lines_count)

    async def send_history(self, lines_count: int):
        """Send last N lines of logs (non-streaming, one-shot retrieval)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl",
                "-u", self.service_name,
                "-n", str(lines_count),
                "--no-pager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)

            for line in stdout.decode(errors="ignore").splitlines():
                text = line.strip()
                if text:
                    await self.send(text_data=json.dumps({
                        "line": text,
                        "type": "history",
                    }))
        except asyncio.TimeoutError:
            await self.send(text_data=json.dumps({
                "error": "History retrieval timed out",
            }))
        except Exception as exc:
            await self.send(text_data=json.dumps({
                "error": str(exc),
            }))

    async def stream_logs(self):
        """
        Stream real-time logs from journalctl -f, starting with the last 50 lines.
        Sends JSON messages with a 'type' field of "stream".
        If the subprocess exits unexpectedly, sends an error and closes the connection.
        """
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "journalctl",
                "-u", self.service_name,
                "-f",
                "--no-pager",
                "-n", "50",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    # Subprocess exited unexpectedly
                    break

                text = line.decode(errors="ignore").strip()
                if text:
                    await self.send(text_data=json.dumps({
                        "line": text,
                        "type": "stream",
                    }))

            # If we reach here, the subprocess exited unexpectedly
            await self.send(text_data=json.dumps({
                "error": "Log stream ended unexpectedly",
            }))
            await self.close(code=1011)

        except asyncio.CancelledError:
            pass

        except Exception as exc:
            try:
                await self.send(text_data=json.dumps({
                    "error": str(exc),
                }))
                await self.close(code=1011)
            except Exception:
                pass