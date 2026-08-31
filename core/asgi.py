import os
import django
from django.core.asgi import get_asgi_application

try:
    import uvloop
    uvloop.install()
except Exception:
    pass

try:
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.auth import AuthMiddlewareStack
    CHANNELS_AVAILABLE = True
except ImportError:
    CHANNELS_AVAILABLE = False

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
django_asgi_app = get_asgi_application()

if CHANNELS_AVAILABLE:
    import users.routing
    import camera.routing
    import attendance.routing
    import youtube.routing

    application = ProtocolTypeRouter({
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(
            URLRouter(
                users.routing.websocket_urlpatterns +
                camera.routing.websocket_urlpatterns +
                attendance.routing.websocket_urlpatterns +
                youtube.routing.websocket_urlpatterns
            )
        ),
    })
else:
    application = django_asgi_app
