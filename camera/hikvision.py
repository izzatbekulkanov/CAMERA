# camera/hikvision.py
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import warnings
from collections import defaultdict

import requests
from django.utils import timezone
from urllib3.exceptions import InsecureRequestWarning

warnings.simplefilter("ignore", InsecureRequestWarning)
logger = logging.getLogger(__name__)

HIKVISION_URL = "https://dc.namspi.uz/DS-K1T673DX.php"
TIMEOUT = 2.0

_queue: queue.Queue[dict] = queue.Queue(maxsize=1000)

# 🔒 ASOSIY SHABLON (dict) — hamma maydonlar saqlanadi
_BASE_EVENT = {
    "ipAddress": "0.0.0.0",  # runtime da almashtiramiz
    "ipv6Address": "fe80::a6d5:c2ff:fe4e:339e",
    "portNo": 443,
    "protocol": "HTTPS",
    "macAddress": "a4:d5:c2:4e:33:9e",
    "channelID": 1,
    "dateTime": "1970-01-01T00:00:00+00:00",  # runtime da almashtiramiz
    "activePostCount": 1,
    "eventType": "AccessControllerEvent",
    "eventState": "active",
    "eventDescription": "Access Controller Event",
    "shortSerialNumber": "FY0597362",
    "AccessControllerEvent": {
        "deviceName": "DS-K1T673DX-NamDPI",
        "majorEventType": 5,
        "subEventType": 75,
        "name": "",  # runtime da almashtiramiz
        "cardReaderNo": 1,
        "employeeNoString": "",  # runtime da almashtiramiz
        "serialNo": 25523,
        "userType": "normal",
        "currentVerifyMode": "faceOrFpOrCardOrPw",
        "frontSerialNo": 25522,
        "label": "",
        "mask": "no",
        "helmet": "unknown",
        "picturesNumber": 1,
        "purePwdVerifyEnable": True,
        "FaceRect": {"height": 0.051, "width": 0.092, "x": 0.012, "y": 0.511},
    },
}

SEND_COOLDOWN_S = 10.0  # ✅ user+camera uchun 10s da 1 marta

_last_sent: dict[tuple[str, str], float] = defaultdict(float)
_last_sent_lock = threading.Lock()


def _deepcopy_base_event() -> dict:
    # nested dictlarni ham copy qilish uchun ishonchli yo'l
    return json.loads(json.dumps(_BASE_EVENT))


def _build_event_string(camera_ip: str, full_name: str, person_id: str) -> str:
    ev = _deepcopy_base_event()

    ev["ipAddress"] = camera_ip
    ev["dateTime"] = timezone.now().isoformat()

    ev["AccessControllerEvent"]["name"] = full_name
    ev["AccessControllerEvent"]["employeeNoString"] = person_id or ""

    # Hikvision endpoint: string ichida JSON bo‘lishi kerak
    return json.dumps(ev, ensure_ascii=False)


def enqueue_hikvision_event(
        camera_ip: str,
        full_name: str,
        person_id: str,
        user_id: int | None = None,
        similarity: float | None = None
) -> None:
    """
    RTSP runner ichidan chaqiriladi.
    Queue ga event qo'yadi.
    Throttle: har user+camera uchun 10 sekundda 1 martadan ortiq yubormaydi.
    """
    key = (camera_ip, str(person_id or user_id or "unknown"))

    nowm = time.monotonic()
    with _last_sent_lock:
        last = _last_sent.get(key, 0.0)
        if nowm - last < SEND_COOLDOWN_S:
            return
        _last_sent[key] = nowm

    event_str = _build_event_string(camera_ip=camera_ip, full_name=full_name, person_id=person_id)

    payload = {
        "_meta": {  # log/notify uchun
            "camera_ip": camera_ip,
            "user_id": user_id,
            "similarity": similarity,
            "full_name": full_name,
            "person_id": person_id,
        },
        "AccessControllerEvent": event_str,
    }

    try:
        _queue.put_nowait(payload)
        logger.info(
            "[HIKVISION] enqueue camera=%s user_id=%s person_id=%s name=%s",
            camera_ip,
            user_id,
            person_id,
            full_name,
        )
    except queue.Full:
        logger.warning("[HIKVISION] queue full, event dropped camera=%s user_id=%s", camera_ip, user_id)


def _try_notify_telegram(meta: dict, status_code: int) -> None:
    """
    Hikvision yuborilgandan keyin telegramga xabar:
    - Bot token bo'lsa
    - User botda ro'yxatdan o'tgan bo'lsa
    """
    # xohlasangiz status_code shartini olib tashlaysiz
    if status_code != 200:
        return

    try:
        from camera.telegram_bot import notify_user_arrival

        notify_user_arrival(
            user_id=meta.get("user_id"),
            person_id=meta.get("person_id"),
            camera_ip=meta.get("camera_ip") or "",
            similarity=meta.get("similarity"),
        )
    except Exception as exc:
        logger.warning("[TG] notify error: %s", exc)


def _worker() -> None:
    logger.info("[HIKVISION] sender worker started")
    while True:
        payload = _queue.get()
        try:
            meta = payload.pop("_meta", {})  # log/notify uchun

            resp = requests.post(
                HIKVISION_URL,
                json=payload,
                timeout=TIMEOUT,
                verify=False,
            )

            body = (resp.text or "").strip()
            if len(body) > 800:
                body = body[:800] + "...<truncated>"

            logger.info(
                "[HIKVISION] sent status=%s camera=%s user_id=%s person_id=%s name=%s body=%s",
                resp.status_code,
                meta.get("camera_ip"),
                meta.get("user_id"),
                meta.get("person_id"),
                meta.get("full_name"),
                body,
            )

            # ✅ telegram notify (agar ro'yxatdan o'tgan bo'lsa)
            _try_notify_telegram(meta, resp.status_code)

        except Exception as exc:
            logger.warning("[HIKVISION] send failed: %s", exc)
        finally:
            _queue.task_done()


def start_hikvision_worker() -> None:
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    logger.info("[HIKVISION] worker thread launched")
