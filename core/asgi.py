import os
import django
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack



os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()
django_asgi_app = get_asgi_application()

# Routing import
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

# ================================================================
# Background tasklarni ASGI loopga xavfsiz ulash
# ================================================================
import asyncio

async def start_tasks_safe():
    """
    Bu coroutine ichida start_background_tasks() ni chaqiramiz.
    start_background_tasks() ODDIY (sync) funksiya, uni await QILMAYMIZ.
    """
    try:
        from camera.tasks import start_background_tasks
        start_background_tasks()   # ✅ bu yerda 'await' yo'q
    except Exception as e:
        print("Background task start xatolik:", e)

try:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        # Agar loop allaqachon ishlayotgan bo'lsa (masalan, Daphne ichida)
        loop.create_task(start_tasks_safe())
    else:
        # Import vaqtida ishlashi uchun bir marta run qilamiz
        loop.run_until_complete(start_tasks_safe())
except Exception as e:
    print("ASGI loop error:", e)

print("ASGI muvaffaqiyatli yuklandi | WebSocket + Background Tasks faol")
