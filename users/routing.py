# users/routing.py
from django.urls import re_path
from .consumers import FaceEncodingConsumer, SyncProgressConsumer

websocket_urlpatterns = [
    re_path(r'ws/sync-progress/', SyncProgressConsumer.as_asgi()),
    re_path(r'ws/face-encoding/$', FaceEncodingConsumer.as_asgi()),

]