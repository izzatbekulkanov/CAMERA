from django.urls import re_path
from .consumers import PsychologyConsumer, ServiceLogConsumer

websocket_urlpatterns = [
    re_path(r'ws/psychology/$', PsychologyConsumer.as_asgi()),
    re_path(r"ws/logs/(?P<service_name>[^/]+)/$", ServiceLogConsumer.as_asgi()),

]
