from django.urls import re_path
from .consumers import YouTubeStreamConsumer

websocket_urlpatterns = [
    re_path(r"ws/youtube/$", YouTubeStreamConsumer.as_asgi()),
]
