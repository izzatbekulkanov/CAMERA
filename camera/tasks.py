# camera/tasks.py
import logging

import requests
from celery import shared_task
from django.utils import timezone

from camera.models import Camera

logger = logging.getLogger(__name__)


def start_background_tasks() -> None:
    """
    ASGI import paytida chaqiriladigan yengil hook.

    Real kamera recognition oqimi `python manage.py camera_daemon` orqali yuradi.
    Bu funksiya mavjud bo‘lmasa ASGI startida import xatosi chiqadi.
    """
    logger.info("[CAMERA] ASGI background hook ready; camera_daemon handles RTSP recognition.")


@shared_task
def check_camera_health() -> None:
    """
    Celery beat orqali kameralarning ulanishini tekshiradi.

    - `is_active=True` kameralar uchun HTTP endpointni ping qiladi
    - `last_checked` ni yangilaydi
    - Agar keyinroq Camera modelga `is_online` qo‘shsangiz, avtomatik update qiladi
    """
    cams = Camera.objects.filter(is_active=True)

    for cam in cams:
        ok = False
        url = f"http://{cam.ip}:{cam.port}/"

        try:
            r = requests.get(url, auth=(cam.username, cam.password), timeout=5)
            if r.status_code in (200, 401, 302):
                ok = True
        except Exception as exc:
            logger.warning("[HEALTH] %s ulanmagan: %s", cam.ip, exc)

        cam.last_checked = timezone.now()
        update_fields = ["last_checked"]

        if hasattr(cam, "is_online"):
            if getattr(cam, "is_online") != ok:
                setattr(cam, "is_online", ok)
                update_fields.append("is_online")

        cam.save(update_fields=update_fields)
