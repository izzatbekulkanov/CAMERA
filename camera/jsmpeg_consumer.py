import asyncio
import logging
import os
import subprocess
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .models import Camera

logger = logging.getLogger(__name__)

@database_sync_to_async
def get_camera_url(camera_id):
    try:
        c = Camera.objects.get(pk=camera_id, is_active=True)
        return f"rtsp://{c.username}:{c.password}@{c.ip}:554/Streaming/Channels/101"
    except Camera.DoesNotExist:
        return None

class JSMpegCameraConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.camera_id = self.scope['url_route']['kwargs'].get('camera_id')
        self.process = None
        self.task = None
        await self.accept()
        logger.info(f"[JSMpeg] Connected websocket for camera {self.camera_id}")
        
        url = await get_camera_url(self.camera_id)
        if not url:
            await self.close()
            return
            
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-hwaccel", "cuda",
            "-hwaccel_output_format", "cuda",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtsp_transport", "tcp",
            "-i", url,
            "-vf", "scale_cuda=1280:720",
            "-c:v", "h264_nvenc",
            "-profile:v", "baseline",
            "-level", "4.1",
            "-preset", "p1",
            "-tune", "ull",
            "-b:v", "2500k",
            "-g", "25",
            "-r", "25",
            "-an",
            "-f", "mp4",
            "-movflags", "empty_moov+default_base_moof+frag_keyframe",
            "-"
        ]
        
        self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.task = asyncio.create_task(self.read_ffmpeg_stdout())

    async def disconnect(self, close_code):
        if self.task:
            self.task.cancel()
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        logger.info(f"[JSMpeg] Disconnected websocket for camera {self.camera_id}")

    async def read_ffmpeg_stdout(self):
        loop = asyncio.get_running_loop()
        fd = self.process.stdout.fileno()
        try:
            while True:
                chunk = await loop.run_in_executor(None, os.read, fd, 4096)
                if not chunk:
                    break
                await self.send(bytes_data=chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[JSMpeg] Read error: {e}")
        finally:
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass

    async def receive(self, text_data=None, bytes_data=None):
        pass
