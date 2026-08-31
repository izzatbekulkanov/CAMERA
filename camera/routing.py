from django.urls import re_path

from camera.consumers import (
    LiveAttendanceConsumer,
    IpCameraConsumer,
    Go2RtcProxyConsumer
)
from camera.jsmpeg_consumer import JSMpegCameraConsumer

websocket_urlpatterns = [
    # Jonli davomat ro'yxati
    re_path(r"ws/attendance/live/$", LiveAttendanceConsumer.as_asgi()),

    # JSMpeg (Python WebSocket + FFmpeg)
    re_path(r"ws/jsmpeg/(?P<camera_id>\d+)/$", JSMpegCameraConsumer.as_asgi()),

    # IP kameralar (ffmpeg + GPU/CPU + face detection ramkalar bilan)
    re_path(r"ws/ipcamera/(?P<camera_id>\d+)/$", IpCameraConsumer.as_asgi()),
    
    # Go2RTC Proxy for WebRTC MSE streaming
    re_path(r"ws/go2rtc/(?P<stream_name>[\w\-]+)/$", Go2RtcProxyConsumer.as_asgi()),
]
