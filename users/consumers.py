# users/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from django.core.cache import cache

class SyncProgressConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user_id = self.scope["user"].id
        await self.accept()

        # Har 1 soniyada progress yuborish
        import asyncio
        while True:
            progress = cache.get(f"sync_progress_{self.user_id}", {"percent": 0, "message": "..."})
            await self.send(text_data=json.dumps(progress))
            await asyncio.sleep(1)

    async def disconnect(self, close_code):
        pass