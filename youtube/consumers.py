# youtube/consumers.py
import json
from channels.generic.websocket import AsyncWebsocketConsumer

class YouTubeStreamConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.group_name = "youtube_stream_updates"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def stream_update(self, event):
        await self.send(text_data=json.dumps(event["payload"]))
