from django.urls import re_path
from . import consumers
from .consumers import PsychologyConsumer

websocket_urlpatterns = [
    re_path(r'ws/psychology/$', PsychologyConsumer.as_asgi()),
]
