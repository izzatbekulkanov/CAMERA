# camera/routing.py
from django.urls import re_path

from camera.consumers import CameraStreamConsumer, LiveAttendanceConsumer, IpCameraConsumer

websocket_urlpatterns = [
    re_path(r"ws/camera/(?P<camera_id>\d+)/stream/$", CameraStreamConsumer.as_asgi()),
    re_path(r"ws/attendance/live/$", LiveAttendanceConsumer.as_asgi()),
    re_path(r'ws/ipcamera/(?P<camera_id>\d+)/$', IpCameraConsumer.as_asgi()),
]
