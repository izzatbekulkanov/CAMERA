import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from channels.db import database_sync_to_async
from .models import Camera

logger = logging.getLogger(__name__)

@database_sync_to_async
def get_camera_url(camera_id):
    try:
        c = Camera.objects.get(pk=camera_id, is_active=True)
        return f"rtsp://{c.username}:{c.password}@{c.ip}:554/Streaming/Channels/102"
    except Camera.DoesNotExist:
        return None

class AiortcCameraConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.camera_id = self.scope['url_route']['kwargs'].get('camera_id')
        self.pc = None
        self.player = None
        await self.accept()
        logger.info(f"[Aiortc] Connected websocket for camera {self.camera_id}")

    async def disconnect(self, close_code):
        if self.pc:
            await self.pc.close()
        logger.info(f"[Aiortc] Disconnected websocket for camera {self.camera_id}")

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            
            if data['type'] == 'offer':
                url = await get_camera_url(self.camera_id)
                if not url:
                    await self.send(json.dumps({'type': 'error', 'message': 'Camera not found'}))
                    return

                self.pc = RTCPeerConnection()
                
                # Options for lowest latency PyAV decoding
                options = {
                    "rtsp_transport": "tcp",
                    "fflags": "nobuffer",
                    "flags": "low_delay",
                }
                self.player = MediaPlayer(url, format="rtsp", options=options)
                
                if self.player.video:
                    self.pc.addTrack(self.player.video)
                    
                offer = RTCSessionDescription(sdp=data['sdp'], type=data['type'])
                await self.pc.setRemoteDescription(offer)
                
                answer = await self.pc.createAnswer()
                await self.pc.setLocalDescription(answer)
                
                await self.send(json.dumps({
                    'type': 'answer',
                    'sdp': self.pc.localDescription.sdp
                }))
                
        except Exception as e:
            logger.error(f"[Aiortc] Error: {e}", exc_info=True)
