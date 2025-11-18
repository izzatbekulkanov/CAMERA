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

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            users.routing.websocket_urlpatterns +
            camera.routing.websocket_urlpatterns +
            attendance.routing.websocket_urlpatterns
        )
    ),
})

# ================================================================
# Agar event loop mavjud bo‘lsa, background task ishga tushirish
# ================================================================
async def start_tasks_safe():
    try:
        from camera.tasks import start_background_tasks
        await start_background_tasks()
    except Exception as e:
        print("Background task start xatolik:", e)

# Daphne yoki Uvicorn ishga tushganda
try:
    import asyncio
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(start_tasks_safe())
    else:
        loop.run_until_complete(start_tasks_safe())
except Exception as e:
    print("ASGI loop error:", e)

print("ASGI muvaffaqiyatli yuklandi | WebSocket + Background Tasks faol")
