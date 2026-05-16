# camera/views.py
import json
import logging
import os
import shutil
import time
from urllib.parse import quote

from django.conf import settings
from django.db import transaction
import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


from camera.models import Camera

logger = logging.getLogger(__name__)


def build_rtsp_url(camera: Camera) -> str:
    """Default RTSP URL builder (Hikvision-like)."""
    if camera.rtsp_url:
        return camera.rtsp_url
    user = camera.username or "admin"
    pwd = quote(camera.password or "", safe="")
    ip = camera.ip
    return f"rtsp://{user}:{pwd}@{ip}:554/Streaming/Channels/101"


def build_rtsp_candidates(camera: Camera) -> list[str]:
    """Common RTSP variants for Hikvision/Dahua/Generic cameras."""
    if camera.rtsp_url:
        return [camera.rtsp_url]

    user = camera.username or "admin"
    pwd = quote(camera.password or "", safe="")
    ip = camera.ip
    rtsp_port = 554

    return [
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/101",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/102",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/103",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/cam/realmonitor?channel=1&subtype=1",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/live/ch00_0",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/live/ch00_1",
    ]


def build_preview_rtsp_candidates(camera: Camera) -> list[str]:
    """Preview uchun yengilroq oqimlarni birinchi sinaydi."""
    candidates = build_rtsp_candidates(camera)
    preferred_parts = (
        "/Streaming/Channels/102",
        "/Streaming/Channels/103",
        "subtype=1",
    )
    preferred = [url for url in candidates if any(part in url for part in preferred_parts)]
    rest = [url for url in candidates if url not in preferred]
    return preferred + rest


def build_go2rtc_mjpeg_url(rtsp_url: str) -> str:
    base = getattr(settings, "GO2RTC_BASE_URL", "").rstrip("/")
    path = getattr(settings, "GO2RTC_MJPEG_PATH", "/stream.html")
    if not base:
        return ""
    src = quote(rtsp_url, safe="")
    return f"{base}{path}?src={src}"


def build_go2rtc_mjpeg_urls(camera: Camera) -> list[str]:
    return [url for url in (build_go2rtc_mjpeg_url(rtsp) for rtsp in build_rtsp_candidates(camera)) if url]


async def _local_mjpeg_frames(camera: Camera):
    """Async generator: RTSP → MJPEG frames via OpenCV in a thread."""
    import asyncio
    import queue
    import threading

    try:
        import cv2
    except Exception as exc:
        logger.error("[LOCAL MJPEG] cv2 import failed: %s", exc)
        return

    frame_queue = queue.Queue(maxsize=2)
    stop_event = threading.Event()

    def _capture_thread():
        """Sinxron thread: OpenCV bilan RTSP o'qiydi, JPEG qilib queue'ga qo'yadi."""
        for rtsp_url in build_preview_rtsp_candidates(camera):
            if stop_event.is_set():
                break
            cap = None
            try:
                logger.info("[LOCAL MJPEG] opening camera=%s url=%s", camera.id, rtsp_url)
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                if not cap.isOpened():
                    logger.warning("[LOCAL MJPEG] open failed camera=%s url=%s", camera.id, rtsp_url)
                    continue

                logger.info("[LOCAL MJPEG] stream started camera=%s url=%s", camera.id, rtsp_url)
                misses = 0
                while not stop_event.is_set():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        misses += 1
                        if misses >= 20:
                            logger.warning("[LOCAL MJPEG] read failed camera=%s url=%s", camera.id, rtsp_url)
                            break
                        time.sleep(0.05)
                        continue

                    misses = 0
                    h, w = frame.shape[:2]
                    if w > 1280:
                        scale = 1280 / float(w)
                        frame = cv2.resize(frame, (1280, int(h * scale)))

                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                    if not ok:
                        continue

                    jpeg_data = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Cache-Control: no-cache\r\n\r\n"
                        + encoded.tobytes()
                        + b"\r\n"
                    )

                    # Queue to'lsa, eski frameni tashlab yangi qo'yamiz
                    try:
                        frame_queue.put_nowait(jpeg_data)
                    except queue.Full:
                        try:
                            frame_queue.get_nowait()
                        except queue.Empty:
                            pass
                        frame_queue.put_nowait(jpeg_data)

                    time.sleep(1 / 12)

                # Agar bitta URL muvaffaqiyatli ishlagan bo'lsa, boshqasini sinashga hojat yo'q
                if not stop_event.is_set():
                    continue
                break
            except Exception as exc:
                logger.warning("[LOCAL MJPEG] stream error camera=%s url=%s err=%s", camera.id, rtsp_url, exc)
            finally:
                if cap is not None:
                    cap.release()

        # Thread tugadi — sentinel qo'yamiz
        frame_queue.put(None)

    # Threadni ishga tushiramiz
    thread = threading.Thread(target=_capture_thread, daemon=True)
    thread.start()

    try:
        while True:
            # Queue'dan frame olish (async-safe)
            try:
                data = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: frame_queue.get(timeout=5.0)
                )
            except Exception:
                # Timeout — kamera javob bermayapti
                break

            if data is None:
                # Thread tugadi
                break

            yield data
    except (asyncio.CancelledError, GeneratorExit):
        pass
    finally:
        stop_event.set()
        thread.join(timeout=3.0)


@login_required(login_url='login')
async def ip_camera_mjpeg_stream(request, camera_id: int):
    from asgiref.sync import sync_to_async
    camera = await sync_to_async(
        lambda: Camera.objects.filter(pk=camera_id, is_active=True).first()
    )()
    if not camera:
        raise Http404("Kamera topilmadi yoki faol emas.")

    response = StreamingHttpResponse(
        _local_mjpeg_frames(camera),
        content_type="multipart/x-mixed-replace; boundary=frame",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    return response


# ================== API ==================

@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_camera(request):
    """Kamera qo‘shish (HTTP orqali tekshirib, bazaga yozadi)."""
    try:
        data = json.loads(request.body or "{}")
    except Exception as exc:
        logger.warning("JSON parse error: %s", exc)
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    ip = data.get("ip")
    username = data.get("username") or "admin"
    password = data.get("password")
    port_raw = data.get("port", 80)
    rtsp_url = data.get("rtsp_url")

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Port son bo‘lishi kerak"}, status=400)

    if not ip or not password:
        return JsonResponse({"success": False, "message": "IP va parol majburiy"}, status=400)

    url = f"http://{ip}:{port}"
    logger.info("Kamera qo'shish: %s (user=%s)", url, username)

    try:
        r = requests.get(url, auth=(username, password), timeout=6, verify=False)  # noqa: S501
    except requests.RequestException as exc:
        logger.error("Kamera bilan bog'lanishda xato: %s", exc)
        return JsonResponse(
            {"success": False, "message": "Kamera javob bermadi yoki login/parol noto‘g‘ri"},
            status=502,
        )

    if r.status_code in (200, 401, 302):
        camera, created = Camera.objects.update_or_create(
            ip=ip,
            defaults={
                "port": port,
                "username": username,
                "password": password,
                "rtsp_url": rtsp_url,
                "is_active": True,
                "name": f"Kamera {ip}",
            },
        )
        logger.info("Kamera saqlandi: %s (created=%s)", camera, created)
        return JsonResponse(
            {"success": True, "message": "Kamera muvaffaqiyatli qo‘shildi!"},
            status=201 if created else 200,
        )

    return JsonResponse(
        {"success": False, "message": "Kamera javob bermadi yoki login/parol noto‘g‘ri"},
        status=502,
    )


@login_required(login_url='login')
def api_active_cameras(request):
    """Faol kameralar ro‘yxati."""
    cams = (
        Camera.objects
        .filter(is_active=True)
        .order_by("-added_at")
        .only("ip", "port", "name")
    )

    data = [
        {
            "ip": cam.ip,
            "port": cam.port,
            "name": cam.name or f"Kamera {cam.ip}",
        }
        for cam in cams
    ]
    return JsonResponse({"cameras": data})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_remove_camera(request, ip):
    """Kamerani IP bo‘yicha o‘chirish."""
    deleted_count, _ = Camera.objects.filter(ip=ip).delete()

    logger.info("Kamera o'chirildi: ip=%s, count=%s", ip, deleted_count)

    return JsonResponse(
        {
            "success": True,
            "deleted": deleted_count,
            "message": f"{ip} o‘chirildi" if deleted_count else "Kamera topilmadi",
        }
    )


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_cameras(request, ip):
    """
    Kamera maydonlarini yangilash:
    - is_active
    - enable_face_detection
    """
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "message": "JSON format xato"},
            status=400,
        )

    try:
        camera = Camera.objects.get(ip=ip)
    except Camera.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Kamera topilmadi"},
            status=404,
        )

    # Qaysi maydonlarni yangilashga ruxsat beramiz
    updatable_fields = {
        "is_active": "is_active",
        "enable_face_detection": "enable_face_detection",
    }

    updated_fields = []

    for key, field_name in updatable_fields.items():
        if key in payload:
            setattr(camera, field_name, bool(payload[key]))
            updated_fields.append(field_name)

    if not updated_fields:
        return JsonResponse(
            {"success": False, "message": "Yangilash uchun ma'lumot berilmagan"},
            status=400,
        )

    camera.save(update_fields=updated_fields)

    logger.info(
        "Kamera yangilandi: ip=%s, fields=%s",
        ip,
        updated_fields,
    )

    return JsonResponse(
        {
            "success": True,
            "message": "Kamera sozlamalari yangilandi",
            "updated_fields": updated_fields,
        }
    )

@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_camera(request, ip):
    """
    Kamera ma'lumotlarini to'liq yoki qisman yangilash uchun API
    Kelishi mumkin bo'lgan maydonlar:
        - name
        - port
        - username
        - password (faqat kiritilsa yangilanadi)
        - enable_face_detection
    """
    try:
        data = json.loads(request.body.decode('utf-8') if request.body else "{}")
    except Exception as e:
        logger.error("JSON parse xato: %s | body: %s", e, request.body)
        return JsonResponse({"success": False, "message": "Notoʻgʻri JSON format"}, status=400)

    # Debugging uchun konsolga chiqaramiz (developmentda juda foydali)
    print("\n=== KAMERA YANGILASH SOʻROVI===")
    print(f"IP: {ip}")
    print(f"Foydalanuvchi: {request.user}")
    print(f"Kelgan ma'lumotlar: {data}")
    print("==============================\n")

    try:
        camera = Camera.objects.get(ip=ip)
    except Camera.DoesNotExist:
        return JsonResponse({"success": False, "message": "Kamera topilmadi"}, status=404)

    updated_fields = []

    # Har bir maydonni tekshirib, agar kelsa yangilaymiz
    if 'name' in data:
        new_name = data['name'].strip() if data['name'] else None
        if camera.name != new_name:
            camera.name = new_name
            updated_fields.append('name')

    if 'rtsp_url' in data:
        new_rtsp = data['rtsp_url'].strip() if data['rtsp_url'] else None
        if camera.rtsp_url != new_rtsp:
            camera.rtsp_url = new_rtsp
            updated_fields.append('rtsp_url')

    if 'port' in data:
        try:
            new_port = int(data['port'])
            if 1 <= new_port <= 65535 and camera.port != new_port:
                camera.port = new_port
                updated_fields.append('port')
        except (ValueError, TypeError):
            return JsonResponse({"success": False, "message": "Port notoʻgʻri formatda"}, status=400)

    if 'username' in data:
        new_username = data['username'].strip()
        if camera.username != new_username:
            camera.username = new_username
            updated_fields.append('username')

    if 'password' in data and data['password'].strip():
        new_password = data['password'].strip()
        if camera.password != new_password:  # eski parol bilan solishtirish shart emas, har doim yangilaymiz
            camera.password = new_password
            updated_fields.append('password')
            logger.info("Kamera paroli yangilandi: %s", ip)

    if 'enable_face_detection' in data:
        new_val = bool(data['enable_face_detection'])
        if camera.enable_face_detection != new_val:
            camera.enable_face_detection = new_val
            updated_fields.append('enable_face_detection')

    # Agar hech nima oʻzgarmagan boʻlsa ham success qaytaramiz
    if not updated_fields:
        return JsonResponse({
            "success": True,
            "message": "Hech narsa oʻzgartirilmadi",
            "updated_fields": []
        })

    # Faqat oʻzgargan maydonlarni saqlaymiz (tezkor va samarali)
    try:
        with transaction.atomic():
            camera.save(update_fields=updated_fields)
    except Exception as e:
        logger.error("DB saqlash xatosi [IP: %s]: %s", ip, e)
        return JsonResponse({"success": False, "message": "Ma'lumotlarni saqlashda xatolik"}, status=500)

    logger.info("Kamera muvaffaqiyatli yangilandi: %s | Yangilangan: %s", ip, updated_fields)

    return JsonResponse({
        "success": True,
        "message": "Kamera ma'lumotlari yangilandi",
        "updated_fields": updated_fields,
        "camera": {
            "name": camera.name or "",
            "port": camera.port,
            "username": camera.username,
            "enable_face_detection": camera.enable_face_detection
        }
    })


# ================== HTML VIEWS ==================

@login_required(login_url='login')
def add_camera_view(request):
    """Kamera qo‘shish sahifasi."""
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Kameralar', 'url': '/cameras/list/'},
        {'name': 'Kamera qo‘shish', 'url': None},
    ]
    return render(request, 'cameras/add_camera.html', {'breadcrumbs': breadcrumbs})


@login_required(login_url='login')
def view_cameras(request):
    """Faol kameralarni grid ko‘rinishida ko‘rsatish."""
    cameras = Camera.objects.filter(is_active=True).order_by('name', 'ip')
    enable_ws = getattr(settings, "ENABLE_WS", False)

    if not enable_ws:
        for cam in cameras:
            cam.go2rtc_mjpeg_url = build_go2rtc_mjpeg_url(build_rtsp_url(cam))
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Jonli ko‘rish', 'url': None},
    ]
    context = {
        'cameras': cameras,
        'breadcrumbs': breadcrumbs,
        'total_cameras': cameras.count(),
        'enable_ws': enable_ws,
    }
    return render(request, 'cameras/view_cameras.html', context)


@login_required(login_url='login')
def ip_camera_view_auto(request):
    """is_active=True va enable_face_detection=True kameralardan birinchisini ko‘rsatish."""
    qs = Camera.objects.filter(is_active=True, enable_face_detection=True)
    if not qs.exists():
        raise Http404("Faol va yuzni aniqlash yoqilgan kamera topilmadi.")

    camera = qs.first()
    enable_ws = getattr(settings, "ENABLE_WS", False)
    rtsp_url = build_rtsp_url(camera)
    go2rtc_mjpeg_url = build_go2rtc_mjpeg_url(rtsp_url)
    go2rtc_mjpeg_urls = build_go2rtc_mjpeg_urls(camera)
    local_mjpeg_url = f"/cameras/ip/stream/{camera.id}/"
    camera_ws_enabled = enable_ws and bool(shutil.which("ffmpeg") or os.path.isfile(r"C:\Users\Izzatbek\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"))
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Avto IP Kamera Ko‘rinishi', 'url': None},
    ]
    context = {
        'camera': camera,
        'breadcrumbs': breadcrumbs,
        'total_cameras': qs.count(),
        'enable_ws': enable_ws,
        'camera_ws_enabled': camera_ws_enabled,
        'go2rtc_mjpeg_url': go2rtc_mjpeg_url,
        'local_mjpeg_url': local_mjpeg_url,
        'camera_stream_urls': [local_mjpeg_url] + (go2rtc_mjpeg_urls or ([go2rtc_mjpeg_url] if go2rtc_mjpeg_url else [])),
        'rtsp_url': rtsp_url,
    }
    return render(request, 'cameras/ip_camera_view.html', context)


@login_required(login_url='login')
def camera_list_view(request):
    """Barcha kameralar ro‘yxati + statistika."""
    cameras = Camera.objects.all().order_by('-is_active', 'ip')
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Kameralar', 'url': None},
    ]
    stats = {
        "total": cameras.count(),
        "active": cameras.filter(is_active=True).count(),
        "inactive": cameras.filter(is_active=False).count(),
    }
    context = {
        'breadcrumbs': breadcrumbs,
        'cameras': cameras,
        'stats': stats,
    }
    return render(request, 'cameras/camera_list.html', context)
