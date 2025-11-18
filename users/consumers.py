# users/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.core.cache import cache

from PIL import Image
import face_recognition
import numpy as np
import os
import asyncio
from io import BytesIO

from core import settings

User = get_user_model()
MAX_THREADS = 4


def has_image(user):
    """Foydalanuvchi rasmga ega ekanligini tekshiradi"""
    return hasattr(user, "image") and user.image and bool(getattr(user.image, "name", None))


class FaceEncodingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        print("[WS] Connected:", self.channel_name)
        await self.send_json({"status": "connected", "message": "WS connected for face encoding."})

    async def disconnect(self, close_code):
        print("[WS] Disconnected:", self.channel_name, "code:", close_code)

    async def receive(self, text_data):
        print("[WS] Received:", text_data)
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_json({"status": "error", "error": "Invalid JSON"})
            return

        action = data.get("action")
        user_id = data.get("user_id")
        role = data.get("role")

        if action == "generate_single" and user_id:
            print(f"[WS] Action: generate_single user_id={user_id}")
            await self.generate_single(user_id)
        elif action == "generate_all" and role:
            print(f"[WS] Action: generate_all role={role}")
            await self.generate_all(role)
        else:
            print("[WS] Unknown action:", action)
            await self.send_json({"status": "error", "error": "Unknown action"})

    async def generate_single(self, user_id):
        print(f"[WS] Generating single encoding for user_id={user_id}")
        user = await database_sync_to_async(User.objects.filter(id=user_id).first)()
        if not user or not has_image(user):
            await self.send_json({"status": "error", "user_id": user_id, "error": "User has no image"})
            return

        # Agar encoding mavjud bo'lsa skip qilamiz
        existing = await database_sync_to_async(user.face_encodings.exists)()
        if existing:
            await self.send_json({"status": "skipped", "user_id": user_id, "message": "Encoding already exists"})
            return

        encoding, error = await self.get_encoding(user.image.url)
        if encoding:
            await database_sync_to_async(self.save_encoding)(user, encoding)
            await self.send_json({"status": "ok", "user_id": user.id})
        else:
            await self.send_json({"status": "error", "user_id": user.id, "error": error})

    async def generate_all(self, role):
        print(f"[WS] Generating all encodings for role={role}")
        users = await database_sync_to_async(list)(
            User.objects.filter(role=role).only("id", "image")
        )
        total_users = len(users)
        print(f"[WS] Total users to process: {total_users}")

        for idx, user in enumerate(users, start=1):
            if not has_image(user):
                await self.send_json({
                    "status": "skipped",
                    "user_id": user.id,
                    "progress": f"{idx}/{total_users}",
                    "message": "No image"
                })
                continue

            # Encoding mavjud bo‘lsa skip qilamiz
            existing = await database_sync_to_async(user.face_encodings.exists)()
            if existing:
                await self.send_json({
                    "status": "skipped",
                    "user_id": user.id,
                    "progress": f"{idx}/{total_users}",
                    "message": "Encoding already exists"
                })
                continue

            encoding, error = await self.get_encoding(user.image.url)
            if encoding:
                await database_sync_to_async(self.save_encoding)(user, encoding)
                await self.send_json({
                    "status": "ok",
                    "user_id": user.id,
                    "progress": f"{idx}/{total_users}"
                })
            else:
                await self.send_json({
                    "status": "error",
                    "user_id": user.id,
                    "error": error,
                    "progress": f"{idx}/{total_users}"
                })

        await self.send_json({"status": "done", "message": "Barcha users processed"})
        print("[WS] All users processed")

    async def get_encoding(self, image_url):
        try:
            if image_url.startswith("/media/"):
                path = os.path.join(settings.BASE_DIR, image_url.lstrip("/"))
                if not os.path.exists(path):
                    return None, f"File not found: {path}"
                img = Image.open(path)
            else:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(image_url) as resp:
                        if resp.status != 200:
                            return None, f"HTTP {resp.status}"
                        content = await resp.read()
                img = Image.open(BytesIO(content))

            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((400, 400), Image.Resampling.LANCZOS)
            img_array = np.array(img)
            encodings = face_recognition.face_encodings(img_array, num_jitters=2, model='large')
            if not encodings:
                return None, "Face not found"
            encoding = encodings[0] / np.linalg.norm(encodings[0])
            return encoding.tolist(), None
        except Exception as e:
            print("[WS] Exception in get_encoding:", e)
            return None, str(e)

    def save_encoding(self, user, encoding):
        from users.models import FaceEncoding
        FaceEncoding.objects.update_or_create(
            user=user,
            defaults={"encoding_data": encoding}
        )
        print(f"[WS] Encoding saved for user_id={user.id}")

    async def send_json(self, content):
        await self.send(text_data=json.dumps(content))


class SyncProgressConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user_id = self.scope["user"].id
        await self.accept()
        print(f"[WS] SyncProgressConsumer connected for user {self.user_id}")

        try:
            while True:
                progress = cache.get(f"sync_progress_{self.user_id}", {"percent": 0, "message": "..."})
                await self.send(text_data=json.dumps(progress))
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            print(f"[WS] SyncProgressConsumer cancelled for user {self.user_id}")

    async def disconnect(self, close_code):
        print(f"[WS] SyncProgressConsumer disconnected for user {self.user_id}, code={close_code}")
