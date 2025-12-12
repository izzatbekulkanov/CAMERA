# camera/routing.py

from django.urls import re_path

from camera.consumers import (
    LiveAttendanceConsumer,
    IpCameraConsumer,
)

websocket_urlpatterns = [
    # Jonli davomat ro'yxati
    re_path(r"ws/attendance/live/$", LiveAttendanceConsumer.as_asgi()),

    # IP kameralar (ffmpeg + GPU/CPU + face detection ramkalar bilan)
    re_path(r"ws/ipcamera/(?P<camera_id>\d+)/$", IpCameraConsumer.as_asgi()),
]
