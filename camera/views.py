# camera/views.py
import json
import logging
import os
import shutil
import time
import threading
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
    user = camera.username or "admin"
    pwd = quote(camera.password or "", safe="")
    ip = camera.ip
    rtsp_port = 554

    defaults = [
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/101",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/102",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/Streaming/Channels/103",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/cam/realmonitor?channel=1&subtype=1",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/live/ch00_0",
        f"rtsp://{user}:{pwd}@{ip}:{rtsp_port}/live/ch00_1",
    ]

    if camera.rtsp_url:
        candidates = [camera.rtsp_url]
        for url in defaults:
            if url not in candidates:
                candidates.append(url)
        return candidates

    return defaults


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


def register_go2rtc_stream(camera_id: int, rtsp_url: str, suffix: str = "") -> bool:
    """go2rtc-da kamera oqimini ro'yxatdan o'tkazish (POST /api/streams)"""
    from django.core.cache import cache
    if cache.get("go2rtc_down"):
        return False
    base_url = getattr(settings, "GO2RTC_API_URL", "").rstrip("/")
    if not base_url or not rtsp_url:
        return False

    stream_name = f"camera_{camera_id}{suffix}"
    try:
        import httpx
        # Avval mavjudligini tekshiramiz
        r = httpx.get(f"{base_url}/api/streams", timeout=0.8)
        if r.status_code == 200:
            streams = r.json() or {}
            if stream_name in streams:
                logger.info("[GO2RTC] Stream already exists: %s", stream_name)
                return True
        # Yangi stream qo'shamiz
        r = httpx.put(f"{base_url}/api/streams", params={"name": stream_name, "src": rtsp_url}, timeout=1.0)
        if r.status_code in (200, 201):
            logger.info("[GO2RTC] Stream registered: %s -> %s", stream_name, rtsp_url)
            return True
        else:
            logger.error("[GO2RTC] Registration failed: %s", r.text)
    except Exception as e:
        cache.set("go2rtc_down", True, 30)
        logger.debug("[GO2RTC] Error registering stream: %s", e)
    return False


# Orqaga moslik uchun alias
def register_zlmediakit_proxy(camera_id: int, rtsp_url: str, suffix: str = "") -> bool:
    """go2rtc ga o'tkazildi — orqaga moslik uchun saqlanmoqda."""
    return register_go2rtc_stream(camera_id, rtsp_url, suffix)


def build_go2rtc_mjpeg_url(rtsp_url: str) -> str:
    """go2rtc MJPEG stream URL qaytaradi."""
    from .models import Camera
    base_url = getattr(settings, "GO2RTC_API_URL", "").rstrip("/")
    if not base_url:
        return ""

    import re
    ip_match = re.search(r'@([\d\.]+)', rtsp_url)
    if ip_match:
        ip = ip_match.group(1)
        camera = Camera.objects.filter(ip=ip).first()
        if camera:
            stream_name = f"camera_{camera.id}"
            register_go2rtc_stream(camera.id, rtsp_url)
            return f"{base_url}/api/frame.jpeg?src={stream_name}"

    return ""


def build_go2rtc_mjpeg_urls(camera: Camera) -> list[str]:
    """go2rtc orqali kamera uchun MJPEG URL ro'yxatini qaytaradi."""
    base_url = getattr(settings, "GO2RTC_API_URL", "").rstrip("/")
    if not base_url:
        return []
    stream_name = f"camera_{camera.id}"
    rtsp_url = build_rtsp_url(camera)
    register_go2rtc_stream(camera.id, rtsp_url)
    return [f"{base_url}/api/stream.mjpeg?src={stream_name}"]



async def _local_mjpeg_frames(camera: Camera):
    """Async generator: go2rtc-ning H264/H265 oqimini go2rtc-ning o'ziga ffmpeg orqali transkod qildirtirib,
    mahalliy oqim ko'rinishida proksi (proxy) qilamiz. Bu orqali har bir foydalanuvchiga alohida ffmpeg
    jarayoni ishga tushmaydi, balki go2rtc-ning ichki bitta ffmpeg transkoderidan foydalanib resurs tejaladi.
    """
    import asyncio
    import httpx
    from django.core.cache import cache
    from asgiref.sync import sync_to_async

    # Agar kamera yaqinda offline deb topilgan bo'lsa, serverni qayta-qayta band qilmaymiz
    if cache.get(f"camera_offline_cooldown_{camera.id}"):
        return

    go2rtc_api_url = getattr(settings, "GO2RTC_API_URL", "").rstrip("/")
    stream_name = f"camera_{camera.id}"
    mjpeg_stream_name = f"{stream_name}_mjpeg"
    candidates = build_preview_rtsp_candidates(camera)

    cache.set(f"camera_stream_source_{camera.id}", "offline", timeout=15)

    if go2rtc_api_url and candidates:
        try:
            # 1. go2rtc-da asosiy RTSP oqimini ro'yxatdan o'tkazamiz (agar bo'lmasa)
            target_url = candidates[0]
            await sync_to_async(register_go2rtc_stream)(camera.id, target_url)

            # 2. go2rtc-da mjpeg transkod qiladigan oqimni ro'yxatdan o'tkazamiz
            # Bu oqim uchun go2rtc o'zining ichida 1 dona ffmpeg ishga tushiradi va barcha ulanishlarga tarqatadi.
            mjpeg_src = f"ffmpeg:{stream_name}#video=mjpeg#width=1280#height=720#hardware"
            await sync_to_async(register_go2rtc_stream)(camera.id, mjpeg_src, suffix="_mjpeg")

            # 3. go2rtc MJPEG stream URL
            url = f"{go2rtc_api_url}/api/stream.mjpeg?src={mjpeg_stream_name}"
            
            logger.info("[LOCAL MJPEG PROXY] Fetching from go2rtc: %s", url)
            cache.set(f"camera_stream_source_{camera.id}", "go2rtc", timeout=15)

            last_cache_update = 0.0
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code == 200:
                        async for chunk in r.aiter_bytes():
                            # Cache-ni har 5 soniyada yangilab turamiz
                            now = time.time()
                            if now - last_cache_update > 5.0:
                                cache.set(f"camera_stream_source_{camera.id}", "go2rtc", timeout=15)
                                last_cache_update = now
                            yield chunk
                    else:
                        logger.error("[LOCAL MJPEG PROXY] go2rtc returned status %s for %s", r.status_code, mjpeg_stream_name)
            return
        except Exception as exc:
            logger.warning("[LOCAL MJPEG] go2rtc streaming failed for camera %s: %s. Falling back to OpenCV.", camera.id, exc)    
    import asyncio
    
    rtsp_url = candidates[0]
    logger.info("[LOCAL MJPEG] Starting native FFmpeg proxy for camera=%s url=%s", camera.id, rtsp_url)
    
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-reorder_queue_size", "0",
        "-max_delay", "0",
        "-flags", "low_delay",
        "-i", rtsp_url,
        "-c:v", "mjpeg",
        "-q:v", "5",       # Optimize quality/bandwidth
        "-r", "15",        # Limit FPS for smooth playback over HTTP
        "-f", "mpjpeg",
        "-"
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )
    
    try:
        while True:
            chunk = await process.stdout.read(16384)
            if not chunk:
                break
            yield chunk
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("[LOCAL MJPEG] FFmpeg proxy error camera=%s: %s", camera.id, exc)
    finally:
        try:
            process.terminate()
            await process.wait()
        except:
            pass


async def ip_camera_mjpeg_stream(request, camera_id: int):
    from asgiref.sync import sync_to_async
    camera = await sync_to_async(
        lambda: Camera.objects.filter(pk=camera_id, is_active=True).first()
    )()
    if not camera:
        raise Http404("Kamera topilmadi yoki faol emas.")

    response = StreamingHttpResponse(
        _local_mjpeg_frames(camera),
        content_type="multipart/x-mixed-replace; boundary=ffmpeg",
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # Nginx bufferingni o'chirish
    return response


async def ip_camera_audio_stream(request, camera_id: int):
    from asgiref.sync import sync_to_async
    import asyncio
    from django.conf import settings
    from django.http import StreamingHttpResponse, Http404, HttpResponseForbidden
    from camera.models import Camera

    # JWT token yoki session orqali autentifikatsiya
    authenticated = False
    # 1) Session/cookie autentifikatsiya (wrapped in sync_to_async to avoid SynchronousOnlyOperation in async views)
    def check_session_auth(req):
        user_obj = getattr(req, 'user', None)
        return bool(user_obj and user_obj.is_authenticated)
    
    authenticated = await sync_to_async(check_session_auth)(request)
    # 2) URL query param orqali JWT token
    if not authenticated:
        token_str = request.GET.get('token', '')
        if token_str:
            try:
                import jwt
                secret = getattr(settings, 'DARS_APP_SECRET', settings.SECRET_KEY)
                jwt.decode(token_str, secret, algorithms=['HS256'])
                authenticated = True
            except Exception:
                pass
    if not authenticated:
        return HttpResponseForbidden("Autentifikatsiya talab etiladi.")

    camera = await sync_to_async(
        lambda: Camera.objects.filter(pk=camera_id, is_active=True).first()
    )()
    if not camera:
        raise Http404("Kamera topilmadi yoki faol emas.")

    # go2rtc-da oqim proksini faollashtirishni kafolatlaymiz
    candidates = await sync_to_async(build_preview_rtsp_candidates)(camera)
    if candidates:
        await sync_to_async(register_go2rtc_stream)(camera.id, candidates[0], suffix="_low")

    go2rtc_rtsp_port = getattr(settings, "GO2RTC_RTSP_PORT", 8554)
    stream_name = f"camera_{camera.id}_low"
    rtsp_go2rtc_url = f"rtsp://127.0.0.1:{go2rtc_rtsp_port}/{stream_name}"

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_go2rtc_url,
        "-vn", "-sn",
        "-acodec", "libmp3lame",
        "-ab", "64k",
        "-ar", "24000",
        "-ac", "1",
        "-f", "mp3",
        "pipe:1"
    ]

    async def audio_generator():
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Read first chunk of stdout to verify stream started
            chunk = await proc.stdout.read(8192)
            if not chunk:
                # Read stderr if empty stdout to capture error reason
                stderr_data = await proc.stderr.read(1024)
                logger.warning(
                    "[AUDIO STREAM] ffmpeg exited without producing audio output for camera %s: %s",
                    camera.id,
                    stderr_data.decode().strip()
                )
                return
            
            yield chunk
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass

    response = StreamingHttpResponse(audio_generator(), content_type="audio/mpeg")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response



# ================== API ==================

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

    if 'is_active' in data:
        new_val = bool(data['is_active'])
        if camera.is_active != new_val:
            camera.is_active = new_val
            updated_fields.append('is_active')

    if 'is_entry_camera' in data:
        new_val = bool(data['is_entry_camera'])
        if camera.is_entry_camera != new_val:
            camera.is_entry_camera = new_val
            updated_fields.append('is_entry_camera')

    if 'is_exit_camera' in data:
        new_val = bool(data['is_exit_camera'])
        if camera.is_exit_camera != new_val:
            camera.is_exit_camera = new_val
            updated_fields.append('is_exit_camera')

    if 'is_lesson_camera' in data:
        new_val = bool(data['is_lesson_camera'])
        if camera.is_lesson_camera != new_val:
            camera.is_lesson_camera = new_val
            updated_fields.append('is_lesson_camera')

    if 'enable_infraction_detection' in data:
        new_val = bool(data['enable_infraction_detection'])
        if camera.enable_infraction_detection != new_val:
            camera.enable_infraction_detection = new_val
            updated_fields.append('enable_infraction_detection')

    if 'is_infraction_camera' in data:
        new_val = bool(data['is_infraction_camera'])
        if camera.is_infraction_camera != new_val:
            camera.is_infraction_camera = new_val
            updated_fields.append('is_infraction_camera')

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
            "enable_face_detection": camera.enable_face_detection,
            "is_entry_camera": camera.is_entry_camera,
            "is_exit_camera": camera.is_exit_camera,
            "is_lesson_camera": camera.is_lesson_camera,
        }
    })


# ================== HTML VIEWS ==================




@login_required(login_url='login')
def ip_camera_view_auto(request):
    """Barcha faol Davomat (Kirish/Chiqish) kameralari va Juftliklarini (Nazorat punktlarini) ko'rsatish."""
    from django.db.models import Q
    from camera.models import CameraPair
    from attendance.models import Attendance
    from attendance.view.isup_views import get_isup_devices
    from django.utils import timezone

    today = timezone.now().date()

    qs = Camera.objects.filter(
        is_active=True,
        enable_face_detection=True
    ).filter(
        Q(is_entry_camera=True) | Q(is_exit_camera=True)
    ).order_by('id')

    # ISUP orqali online bo'lgan kameralarni ajratish
    raw_devices = get_isup_devices()
    online_ips = set()
    online_sns = set()

    for dev in raw_devices:
        if dev.get("online"):
            if dev.get("remote_ip"): online_ips.add(dev.get("remote_ip"))
            if dev.get("ip"): online_ips.add(dev.get("ip"))
            if dev.get("serial"): online_sns.add(dev.get("serial"))

    # Agar kameralar online ro'yxatda topilsa ularni, bo'lmasa barcha faol davomat kameralarini olamiz
    online_cams = []
    for cam in qs:
        if cam.ip in online_ips or (cam.serial_number and cam.serial_number in online_sns):
            online_cams.append(cam)

    display_cams = online_cams if online_cams else list(qs)
    if not display_cams:
        raise Http404("Davomat (Kirish/Chiqish) yoqilgan faol kameralar topilmadi.")

    enable_ws = getattr(settings, "ENABLE_WS", False)
    camera_ws_enabled = enable_ws and bool(shutil.which("ffmpeg") or os.path.isfile(r"C:\Users\Izzatbek\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"))

    def enrich_cam_dict(cam):
        rtsp_url = build_rtsp_url(cam)
        go2rtc_mjpeg_url = build_go2rtc_mjpeg_url(rtsp_url)
        go2rtc_mjpeg_urls = build_go2rtc_mjpeg_urls(cam)
        local_mjpeg_url = f"/cameras/ip/stream/{cam.id}/"
        preview_url = f"/settings/isup/api/camera/stream-preview/?ip={cam.ip}&camera_id={cam.id}"
        return {
            'instance': cam,
            'id': cam.id,
            'name': cam.name or cam.ip,
            'ip': cam.ip,
            'mac_address': cam.mac_address or "--",
            'serial_number': cam.serial_number or "--",
            'device_model': cam.device_model or "Hikvision",
            'is_entry_camera': cam.is_entry_camera,
            'is_exit_camera': cam.is_exit_camera,
            'rtsp_url': rtsp_url,
            'local_mjpeg_url': local_mjpeg_url,
            'preview_url': preview_url,
            'go2rtc_mjpeg_url': go2rtc_mjpeg_url,
            'camera_stream_urls': [local_mjpeg_url, preview_url] + (go2rtc_mjpeg_urls or ([go2rtc_mjpeg_url] if go2rtc_mjpeg_url else [])),
        }

    # Barcha kameralar ro'yxati (WebSocket va player uchun)
    cams_dict = {c.id: enrich_cam_dict(c) for c in Camera.objects.filter(is_active=True)}
    cameras_list = [cams_dict[c.id] for c in display_cams if c.id in cams_dict]

    # Kamera Juftliklari (Nazorat punktlari)
    db_pairs = CameraPair.objects.select_related('building', 'entry_camera', 'exit_camera').filter(is_active=True)
    camera_pairs_list = []
    for p in db_pairs:
        entry_dict = cams_dict.get(p.entry_camera_id) if p.entry_camera_id else None
        exit_dict = cams_dict.get(p.exit_camera_id) if p.exit_camera_id else None

        # Agar pair kameralari cameras_list da bo'lmasa, ularni ham qo'shib qo'yamiz
        if entry_dict and entry_dict not in cameras_list:
            cameras_list.append(entry_dict)
        if exit_dict and exit_dict not in cameras_list:
            cameras_list.append(exit_dict)

        entry_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera).count() if p.entry_camera else 0
        exit_count = Attendance.objects.filter(date=today, exit_camera=p.exit_camera).count() if p.exit_camera else 0
        inside_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera, is_present=True).count() if p.entry_camera else 0

        camera_pairs_list.append({
            'id': p.id,
            'name': p.name,
            'description': p.description or '',
            'building_name': p.building.name if p.building else '',
            'entry_camera': entry_dict,
            'exit_camera': exit_dict,
            'entry_count_today': entry_count,
            'exit_count_today': exit_count,
            'inside_count_today': inside_count,
        })

    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'AI Cyber Kamera & Juftliklar Kuzatuvi', 'url': None},
    ]

    first_cam = cameras_list[0]['instance'] if cameras_list else None
    context = {
        'camera': first_cam,
        'cameras_list': cameras_list,
        'camera_pairs_list': camera_pairs_list,
        'breadcrumbs': breadcrumbs,
        'total_cameras': len(cameras_list),
        'enable_ws': enable_ws,
        'camera_ws_enabled': camera_ws_enabled,
        
        # Backward compatibility fields
        'go2rtc_mjpeg_url': cameras_list[0]['go2rtc_mjpeg_url'] if cameras_list else '',
        'local_mjpeg_url': cameras_list[0]['local_mjpeg_url'] if cameras_list else '',
        'camera_stream_urls': cameras_list[0]['camera_stream_urls'] if cameras_list else [],
        'rtsp_url': cameras_list[0]['rtsp_url'] if cameras_list else '',
    }
    return render(request, 'cameras/ip_camera_view.html', context)









@login_required(login_url='login')
def api_camera_stream_source(request, camera_id: int):
    """Kameraning joriy oqim turini (go2rtc yoki opencv/ffmpeg) qaytaradigan API."""
    from django.core.cache import cache
    source = cache.get(f"camera_stream_source_{camera_id}", "offline")
    return JsonResponse({"camera_id": camera_id, "source": source})


# ================== go2rtc Boshqaruvi ==================

class Go2RTCClient:
    """go2rtc REST API client."""

    def __init__(self):
        self.base_url = getattr(settings, "GO2RTC_API_URL", "").rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _get(self, path: str, params: dict = None) -> dict:
        if not self.is_configured():
            return {"error": "go2rtc URL sozlanmagan"}
        url = f"{self.base_url}{path}"
        try:
            import httpx
            r = httpx.get(url, params=params or {}, timeout=4.0)
            r.raise_for_status()
            return r.json() or {}
        except Exception as e:
            logger.error("go2rtc API %s error: %s", path, e)
            return {"error": str(e)}

    def _put(self, path: str, params: dict = None) -> bool:
        if not self.is_configured():
            return False
        url = f"{self.base_url}{path}"
        try:
            import httpx
            r = httpx.put(url, params=params or {}, timeout=4.0)
            return r.status_code in (200, 201)
        except Exception as e:
            logger.error("go2rtc PUT %s error: %s", path, e)
            return False

    def _delete(self, path: str, params: dict = None) -> bool:
        if not self.is_configured():
            return False
        url = f"{self.base_url}{path}"
        try:
            import httpx
            r = httpx.delete(url, params=params or {}, timeout=4.0)
            return r.status_code in (200, 204)
        except Exception as e:
            logger.error("go2rtc DELETE %s error: %s", path, e)
            return False

    def get_streams(self) -> dict:
        """Barcha oqimlar ro'yxatini qaytaradi."""
        return self._get("/api/streams") or {}

    def add_stream(self, stream_name: str, rtsp_url: str) -> bool:
        """Yangi RTSP oqim qo'shadi."""
        return self._put("/api/streams", {"name": stream_name, "src": rtsp_url})

    def del_stream(self, stream_name: str) -> bool:
        """Oqimni o'chiradi."""
        return self._delete("/api/streams", {"name": stream_name})

    def is_online(self) -> bool:
        """go2rtc server ishlayaptimi."""
        try:
            import httpx
            r = httpx.get(f"{self.base_url}/api/streams", timeout=2.0)
            return r.status_code == 200
        except Exception:
            return False


# Orqaga moslik uchun alias
ZLMediaKitClient = Go2RTCClient


@login_required(login_url='login')
def go2rtc_dashboard(request):
    """go2rtc dashboard."""
    client = Go2RTCClient()

    streams_data = client.get_streams()
    is_online = client.is_online()

    # go2rtc streams formatini tizimga moslashtirish
    media_list = []
    for stream_name, stream_info in streams_data.items():
        producers = (stream_info.get("producers") or []) if isinstance(stream_info, dict) else []
        consumers = (stream_info.get("consumers") or []) if isinstance(stream_info, dict) else []
        media_list.append({
            "app": "live",
            "stream": stream_name,
            "readerCount": len(consumers),
            "bytesSpeed": 0,
            "originTypeStr": "go2rtc",
            "schemas": ["rtsp", "webrtc", "hls"],
        })

    streams_count = len(media_list)
    clients_count = sum(s["readerCount"] for s in media_list)

    cameras = Camera.objects.filter(is_active=True).order_by('id')
    camera_streams = []
    for cam in cameras:
        stream_name = f"camera_{cam.id}"
        matched_stream = stream_name if stream_name in streams_data else (cam.name if cam.name in streams_data else stream_name)
        is_active = stream_name in streams_data or (cam.name and cam.name in streams_data)

        live_stream_url = f"/api/go2rtc/stream/{matched_stream}/"
        live_frame_url = f"/api/go2rtc/frame/{matched_stream}/"
        play_url = f"/cameras/ip/stream/{cam.id}/"
        rtsp_url = f"rtsp://127.0.0.1:{getattr(settings, 'GO2RTC_RTSP_PORT', 8554)}/{matched_stream}"
        camera_rtsp_source = build_rtsp_url(cam)

        camera_streams.append({
            "camera": cam,
            "stream_name": matched_stream,
            "is_active_zlm": is_active,
            "live_stream_url": live_stream_url,
            "live_frame_url": live_frame_url,
            "play_url": play_url,
            "rtsp_url": rtsp_url,
            "camera_rtsp_source": camera_rtsp_source,
        })

    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'go2rtc boshqaruvi', 'url': None},
    ]

    context = {
        'is_online': is_online,
        'media_list': media_list,
        'threads_load': [],
        'streams_count': streams_count,
        'clients_count': clients_count,
        'camera_streams': camera_streams,
        'breadcrumbs': breadcrumbs,
        'zlm_api_url': client.base_url,
        'zlm_rtsp_port': getattr(settings, 'GO2RTC_RTSP_PORT', 8554),
    }
    return render(request, 'cameras/go2rtc_dashboard.html', context)


@login_required(login_url='login')
def api_go2rtc_stream_mjpeg(request, stream_name: str):
    """go2rtc orqali brauzerga jonli MJPEG oqim uzatish proxy-si."""
    import requests
    from urllib.parse import quote
    from django.http import StreamingHttpResponse, HttpResponse

    clean_stream = quote(stream_name.strip())
    target_url = f"http://127.0.0.1:1984/api/stream.mjpeg?src={clean_stream}"
    try:
        req = requests.get(target_url, stream=True, timeout=8.0)
        if req.status_code == 200:
            def stream_gen():
                try:
                    for chunk in req.iter_content(chunk_size=16384):
                        if chunk:
                            yield chunk
                except (GeneratorExit, Exception):
                    req.close()

            resp = StreamingHttpResponse(
                stream_gen(),
                content_type=req.headers.get("content-type", "multipart/x-mixed-replace; boundary=frame")
            )
            resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp["Pragma"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            return resp
    except Exception as e:
        logger.warning("[GO2RTC PROXY] Stream error for %s: %s", stream_name, e)

    # Fallback to local Django camera stream
    cam_id = None
    if stream_name.startswith("camera_"):
        try:
            cam_id = int(stream_name.replace("camera_", ""))
        except ValueError:
            pass
    if cam_id:
        camera = Camera.objects.filter(id=cam_id, is_active=True).first()
        if camera:
            return ip_camera_mjpeg_stream(request, cam_id)

    return HttpResponse(status=404)


@login_required(login_url='login')
def api_go2rtc_frame(request, stream_name: str):
    """go2rtc orqali kameradan bitta jonli kadr (JPEG snapshot) olish proxy-si."""
    import requests
    from urllib.parse import quote
    from django.http import HttpResponse

    clean_stream = quote(stream_name.strip())
    target_url = f"http://127.0.0.1:1984/api/frame.jpeg?src={clean_stream}"
    try:
        r = requests.get(target_url, timeout=3.0)
        if r.status_code == 200 and len(r.content) > 1000:
            resp = HttpResponse(r.content, content_type="image/jpeg")
            resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp["Pragma"] = "no-cache"
            return resp
    except Exception:
        pass

    # Agar stream_name orqali topilmasa, kamera ID orqali tekshirish
    if stream_name.startswith("camera_"):
        try:
            cam_id = int(stream_name.replace("camera_", ""))
            cam = Camera.objects.filter(id=cam_id, is_active=True).first()
            if cam and cam.name:
                alt_url = f"http://127.0.0.1:1984/api/frame.jpeg?src={quote(cam.name)}"
                r = requests.get(alt_url, timeout=3.0)
                if r.status_code == 200 and len(r.content) > 1000:
                    resp = HttpResponse(r.content, content_type="image/jpeg")
                    resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    resp["Pragma"] = "no-cache"
                    return resp
        except Exception:
            pass

    return HttpResponse(status=404)






KNOWN_CAMERA_PASSWORDS = [
    "Qwerty@123456.",
    "parol400",
    "Qwerty@12",
    "namdu309",
    "N@madu309",
    "N@mdu309",
    "n@mdu309",
    "admin",
    "12345",
    "123456",
    "Hikvision",
]

def fetch_camera_device_info(ip: str, port: int = 80, username: str = "admin", password: str = "") -> dict:
    """
    Kameraning ISAPI yoki boshqa protokollari orqali uning MAC manzili,
    Seriya raqami, Modeli va ishchi parolini avtomatik aniqlash.
    """
    import requests
    from requests.auth import HTTPDigestAuth, HTTPBasicAuth
    import xml.etree.ElementTree as ET
    import re
    import subprocess

    info = {
        "mac_address": None,
        "serial_number": None,
        "device_model": None,
        "channel_name": None,
        "working_password": password or "parol400",
        "working_port": port or 80,
        "working_username": username or "admin",
        "reachable": False,
        "auth_success": False,
    }

    port_list = [port] if port and int(port) > 0 else []
    for p in [80, 8000]:
        if p not in port_list:
            port_list.append(p)

    pwd_list = [password.strip()] if password and password.strip() else []
    for p in KNOWN_CAMERA_PASSWORDS:
        if p not in pwd_list:
            pwd_list.append(p)

    user_list = [username.strip()] if username and username.strip() else ["admin"]
    if "admin" not in user_list:
        user_list.append("admin")

    found_ok = False
    for p in port_list:
        if found_ok:
            break
        for u in user_list:
            if found_ok:
                break
            for pwd in pwd_list:
                for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
                    try:
                        url = f"http://{ip}:{p}/ISAPI/System/deviceInfo"
                        r = requests.get(url, auth=auth_cls(u, pwd), timeout=2.0)
                        if r.status_code == 200 and r.text:
                            info["reachable"] = True
                            info["auth_success"] = True
                            info["working_password"] = pwd
                            info["working_port"] = p
                            info["working_username"] = u

                            clean_xml = re.sub(r'xmlns(:\w+)?="[^"]+"', '', r.text)
                            root = ET.fromstring(clean_xml)
                            
                            mac = root.findtext('.//macAddress') or root.findtext('.//MAC')
                            sn = root.findtext('.//serialNumber') or root.findtext('.//subSerialNumber')
                            model = root.findtext('.//model') or root.findtext('.//deviceType')
                            name = root.findtext('.//deviceName')
                            
                            if mac: info["mac_address"] = mac.strip().lower()
                            if sn: info["serial_number"] = sn.strip()
                            if model: info["device_model"] = model.strip()
                            if name and name != "IP CAMERA": info["channel_name"] = name.strip()
                            found_ok = True
                            break
                        elif r.status_code in [401, 403]:
                            info["reachable"] = True
                    except requests.exceptions.RequestException as exc:
                        err_str = str(exc).lower()
                        if "113" in err_str or "no route to host" in err_str or "network is unreachable" in err_str:
                            # Tarmoqda bu IP yo'q bo'lsa, keyingi parollarni sinab vaqt o'tkazmaymiz
                            return info
                if found_ok:
                    break

    if not info["mac_address"]:
        try:
            arp_res = subprocess.run(["arp", "-n", ip], stdout=subprocess.PIPE, text=True, timeout=1.5)
            for line in arp_res.stdout.splitlines():
                if ip in line:
                    for p in line.split():
                        if re.match(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$', p):
                            info["mac_address"] = p.lower()
                            break
        except Exception:
            pass

    return info


@csrf_exempt
@login_required(login_url='login')
@require_POST
def go2rtc_api_add_proxy(request):
    """RTSP oqim proksini qo'lda ZLMediaKit-ga qo'shish va tekshirish API-si."""
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    stream_name = data.get("stream_name", "").strip()
    rtsp_url = data.get("rtsp_url", "").strip()
    
    # 1. Agar to'liq RTSP URL berilgan bo'lsa (mavjud kameradan chaqirilganda)
    if rtsp_url:
        if not stream_name:
            return JsonResponse({"success": False, "message": "Stream ID talab qilinadi"}, status=400)
        client = Go2RTCClient()
        success = client.add_stream(stream_name, rtsp_url)
        if success:
            return JsonResponse({"success": True, "message": f"Oqim '{stream_name}' muvaffaqiyatli ro'yxatdan o'tkazildi"})
        return JsonResponse({"success": False, "message": "go2rtc ga oqim qo'shib bo'lmadi"})

    # 2. Agar IP, login va parol berilgan bo'lsa (yangi formadan chaqirilganda)
    ip = data.get("ip", "").strip()
    username = data.get("username", "admin").strip()
    password = data.get("password", "").strip()
    
    if not stream_name or not ip or not password:
        return JsonResponse({
            "success": False, 
            "message": "Oqim nomi, Kamera IP manzili va Parol talab qilinadi!"
        }, status=400)
        
    # Nomzod RTSP manzillarini tuzish (Hikvision, Dahua va Generic variantlar)
    pwd_quoted = quote(password, safe="")
    candidates = [
        f"rtsp://{username}:{pwd_quoted}@{ip}:554/Streaming/Channels/101",
        f"rtsp://{username}:{pwd_quoted}@{ip}:554/Streaming/Channels/102",
        f"rtsp://{username}:{pwd_quoted}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{username}:{pwd_quoted}@{ip}:554/live/ch00_0",
        f"rtsp://{username}:{pwd_quoted}@{ip}:554",
    ]
    
    import subprocess
    successful_url = None
    last_error_msg = "Kamera bilan aloqa o'rnatib bo'lmadi"
    
    for url in candidates:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-rtsp_transport", "tcp",
            "-timeout", "2500000",  # 2.5 seconds timeout
            "-i", url,
            "-frames:v", "1",
            "-f", "null",
            "-"
        ]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=4.0
            )
            if res.returncode == 0:
                successful_url = url
                break
            
            stderr_output = res.stderr or ""
            if "401" in stderr_output or "unauthorized" in stderr_output.lower() or "authentication" in stderr_output.lower():
                last_error_msg = "Kamera paroli yoki logini noto'g'ri (401 Unauthorized)!"
            elif "connection refused" in stderr_output.lower():
                last_error_msg = "Ulanish rad etildi (RTSP port yopiq yoki tarmoq xatosi)!"
            elif "timeout" in stderr_output.lower() or "timed out" in stderr_output.lower():
                last_error_msg = "Ulanish vaqti tugadi (Kamera o'chiq bo'lishi mumkin)!"
            else:
                last_error_msg = "Aloqa xatoligi yoki IP manzil noto'g'ri!"
        except subprocess.TimeoutExpired:
            last_error_msg = "Kamera ulanish vaqti tugadi (Timeout)!"
        except Exception as e:
            last_error_msg = f"Tizim xatoligi: {str(e)}"
            
    if not successful_url:
        return JsonResponse({"success": False, "message": last_error_msg})
        
    # go2rtc ga oqim qo'shish
    client = Go2RTCClient()
    go2rtc_success = client.add_stream(stream_name, successful_url)

    if not go2rtc_success:
        return JsonResponse({
            "success": False,
            "message": "Ulanish muvaffaqiyatli bo'ldi, lekin go2rtc ga oqimni qo'shib bo'lmadi."
        })

    # go2rtc da muvaffaqiyatli ro'yxatdan o'tgan bo'lsa, ma'lumotlarni bazaga saqlaymiz
    from camera.models import Camera
    
    # 📌 Avtomatik MAC manzil, Seriya raqami va Modelni aniqlash
    dev_info = fetch_camera_device_info(ip, 80, username, password)
    mac_addr = dev_info.get("mac_address")
    sn = dev_info.get("serial_number")
    model = dev_info.get("device_model")

    is_entry = bool(data.get("is_entry_camera", False))
    is_exit = bool(data.get("is_exit_camera", False))
    is_lesson = bool(data.get("is_lesson_camera", False))
    enable_face = bool(data.get("enable_face_detection", is_entry or is_exit))
    enable_infraction = bool(data.get("enable_infraction_detection", False))

    try:
        defaults_payload = {
            "username": username,
            "password": password,
            "rtsp_url": successful_url,
            "name": stream_name,
            "is_active": True,
            "port": 80,
            "enable_face_detection": enable_face,
            "is_entry_camera": is_entry,
            "is_exit_camera": is_exit,
            "is_lesson_camera": is_lesson,
            "enable_infraction_detection": enable_infraction,
        }
        if mac_addr:
            defaults_payload["mac_address"] = mac_addr
        if sn:
            defaults_payload["serial_number"] = sn
        if model:
            defaults_payload["device_model"] = model

        camera, created = Camera.objects.update_or_create(
            ip=ip,
            defaults=defaults_payload
        )
        mac_display = f" (MAC: {mac_addr})" if mac_addr else ""
        msg = f"Kamera{mac_display} muvaffaqiyatli saqlandi va go2rtc ga qo'shildi!"
        if not created:
            msg = f"Kamera{mac_display} ma'lumotlari yangilandi va go2rtc ga qo'shildi!"
        return JsonResponse({"success": True, "message": msg, "mac_address": mac_addr})
    except Exception as db_err:
        logger.error("[ADD PROXY] DB save failed: %s", db_err)
        return JsonResponse({
            "success": True,
            "message": "go2rtc ga qo'shildi, lekin bazaga saqlashda xatolik yuz berdi: " + str(db_err)
        })


@csrf_exempt
@login_required(login_url='login')
@require_POST
def go2rtc_api_del_proxy(request):
    """go2rtc dan oqimni o'chirish API-si."""
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    stream_name = data.get("stream_name")
    if not stream_name:
        return JsonResponse({"success": False, "message": "stream_name talab qilinadi"}, status=400)

    client = Go2RTCClient()
    success = client.del_stream(stream_name)

    # Bazadan ham kamerani o'chirish
    from camera.models import Camera
    if stream_name.startswith("camera_"):
        try:
            cam_id = int(stream_name.replace("camera_", ""))
            Camera.objects.filter(id=cam_id).delete()
        except Exception:
            pass
    Camera.objects.filter(name=stream_name).delete()

    return JsonResponse({"success": True, "message": f"Oqim '{stream_name}' tizimdan muvaffaqiyatli o'chirildi"})


@csrf_exempt
@login_required(login_url='login')
def api_go2rtc_webrtc_sdp(request, stream_name: str):
    """go2rtc WebRTC SDP negotiation proxy with fast offline cache."""
    import requests
    from urllib.parse import quote
    from django.http import HttpResponse
    from django.core.cache import cache

    clean_stream = quote(stream_name.strip())
    
    # Agar kamera yaqinda offline deb topilgan bo'lsa, tezkor offline status qaytarish
    if cache.get(f"camera_offline_cooldown_{clean_stream}"):
        return HttpResponse('{"offline": true, "message": "Camera is offline"}', status=503, content_type="application/json")

    target_url = f"http://127.0.0.1:1984/api/webrtc?src={clean_stream}"
    
    if request.method == "POST":
        try:
            r = requests.post(
                target_url, 
                data=request.body, 
                headers={"Content-Type": request.headers.get("Content-Type", "application/sdp")}, 
                timeout=4.0
            )
            if r.status_code == 201:
                cache.delete(f"camera_offline_cooldown_{clean_stream}")
            else:
                cache.set(f"camera_offline_cooldown_{clean_stream}", True, timeout=15)
            return HttpResponse(r.content, status=r.status_code, content_type=r.headers.get("Content-Type", "application/sdp"))
        except Exception:
            cache.set(f"camera_offline_cooldown_{clean_stream}", True, timeout=15)
            return HttpResponse('{"offline": true, "error": "Camera unreachable"}', status=503, content_type="application/json")
    return HttpResponse(status=405)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def go2rtc_api_restart(request):
    """go2rtc Docker containerini qayta yuklash."""
    import subprocess
    try:
        subprocess.run(["docker", "restart", "go2rtc"], timeout=10, check=True)
        return JsonResponse({"success": True, "message": "go2rtc muvaffaqiyatli qayta ishga tushirildi!"})
    except Exception as e:
        return JsonResponse({"success": False, "message": f"go2rtc ni qayta ishga tushirib bo'lmadi: {e}"})



@login_required(login_url='login')
def auditorium_list_view(request):
    from camera.models import Auditorium, Camera, Building
    auditoriums = Auditorium.objects.all().select_related('camera', 'building')
    cameras = Camera.objects.all()
    buildings = Building.objects.all()
    
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Auditoriyalar', 'url': None},
    ]
    
    stats = {
        "total": auditoriums.count(),
        "active": auditoriums.filter(is_active=True).count(),
        "inactive": auditoriums.filter(is_active=False).count(),
    }
    
    context = {
        'breadcrumbs': breadcrumbs,
        'auditoriums': auditoriums,
        'cameras': cameras,
        'buildings': buildings,
        'stats': stats,
    }
    return render(request, 'cameras/auditorium_list.html', context)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_auditorium(request):
    from camera.models import Auditorium, Camera, Building
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    name = data.get("name", "").strip()
    building_id = data.get("building_id")
    camera_id = data.get("camera_id")
    description = data.get("description", "").strip()
    capacity_raw = data.get("capacity", 30)
    is_active = data.get("is_active", True)

    if not name:
        return JsonResponse({"success": False, "message": "Auditoriya nomi majburiy"}, status=400)

    try:
        capacity = int(capacity_raw)
    except (TypeError, ValueError):
        capacity = 30

    building = None
    if building_id:
        try:
            building = Building.objects.get(id=building_id)
        except Building.DoesNotExist:
            pass

    camera = None
    if camera_id:
        try:
            camera = Camera.objects.get(id=camera_id)
        except Camera.DoesNotExist:
            pass

    if Auditorium.objects.filter(name=name).exists():
        return JsonResponse({"success": False, "message": "Bunday nomli auditoriya allaqachon mavjud"}, status=400)

    auditorium = Auditorium.objects.create(
        name=name,
        building=building,
        camera=camera,
        description=description,
        capacity=capacity,
        is_active=bool(is_active)
    )

    return JsonResponse({"success": True, "message": f"'{auditorium.name}' auditoriyasi muvaffaqiyatli qo'shildi"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_auditorium(request, pk):
    from camera.models import Auditorium, Camera, Building
    try:
        auditorium = Auditorium.objects.get(pk=pk)
    except Auditorium.DoesNotExist:
        return JsonResponse({"success": False, "message": "Auditoriya topilmadi"}, status=404)

    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    name = data.get("name", "").strip()
    building_id = data.get("building_id")
    camera_id = data.get("camera_id")
    description = data.get("description", "").strip()
    capacity_raw = data.get("capacity", 30)
    is_active = data.get("is_active", True)

    if not name:
        return JsonResponse({"success": False, "message": "Auditoriya nomi majburiy"}, status=400)

    try:
        capacity = int(capacity_raw)
    except (TypeError, ValueError):
        capacity = 30

    building = None
    if building_id:
        try:
            building = Building.objects.get(id=building_id)
        except Building.DoesNotExist:
            pass

    camera = None
    if camera_id:
        try:
            camera = Camera.objects.get(id=camera_id)
        except Camera.DoesNotExist:
            pass

    if Auditorium.objects.filter(name=name).exclude(pk=pk).exists():
        return JsonResponse({"success": False, "message": "Bunday nomli auditoriya allaqachon mavjud"}, status=400)

    auditorium.name = name
    auditorium.building = building
    auditorium.camera = camera
    auditorium.description = description
    auditorium.capacity = capacity
    auditorium.is_active = bool(is_active)
    auditorium.save()

    return JsonResponse({"success": True, "message": "Auditoriya muvaffaqiyatli tahrirlandi"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_delete_auditorium(request, pk):
    from camera.models import Auditorium
    try:
        auditorium = Auditorium.objects.get(pk=pk)
    except Auditorium.DoesNotExist:
        return JsonResponse({"success": False, "message": "Auditoriya topilmadi"}, status=404)

    name = auditorium.name
    auditorium.delete()
    return JsonResponse({"success": True, "message": f"'{name}' auditoriyasi muvaffaqiyatli o'chirildi"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_building(request):
    from camera.models import Building
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name:
        return JsonResponse({"success": False, "message": "Bino nomi majburiy"}, status=400)

    building, created = Building.objects.get_or_create(
        name=name,
        defaults={"description": description}
    )

    if not created:
        return JsonResponse({"success": False, "message": "Bunday nomli bino allaqachon mavjud"}, status=400)

    return JsonResponse({
        "success": True,
        "id": building.id,
        "name": building.name,
        "message": f"'{building.name}' binosi muvaffaqiyatli qo'shildi"
    })



@login_required(login_url='login')
def subject_list_view(request):
    from camera.models import Subject
    subjects = Subject.objects.all()
    
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Fanlar', 'url': None},
    ]
    
    stats = {
        "total": subjects.count(),
        "active": subjects.filter(is_active=True).count(),
        "inactive": subjects.filter(is_active=False).count(),
    }
    
    context = {
        'breadcrumbs': breadcrumbs,
        'subjects': subjects,
        'stats': stats,
        'degree_choices': Subject.DEGREE_LEVEL_CHOICES,
    }
    return render(request, 'cameras/subject_list.html', context)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_subject(request):
    from camera.models import Subject
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    name = data.get("name", "").strip().replace("‘", "'").replace("’", "'").replace("`", "'").replace("'", "'").strip()
    code = data.get("code", "").strip()
    degree_level = data.get("degree_level", "bachelor").strip()
    description = data.get("description", "").strip()
    is_active = data.get("is_active", True)

    if not name:
        return JsonResponse({"success": False, "message": "Fan nomi majburiy"}, status=400)

    if degree_level not in dict(Subject.DEGREE_LEVEL_CHOICES):
        degree_level = 'bachelor'

    if Subject.objects.filter(name=name).exists():
        return JsonResponse({"success": False, "message": "Bunday nomli fan allaqachon mavjud"}, status=400)

    subject = Subject.objects.create(
        name=name,
        code=code,
        degree_level=degree_level,
        description=description,
        is_active=bool(is_active)
    )

    return JsonResponse({"success": True, "message": f"'{subject.name}' fani muvaffaqiyatli qo'shildi"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_subject(request, pk):
    from camera.models import Subject
    try:
        subject = Subject.objects.get(pk=pk)
    except Subject.DoesNotExist:
        return JsonResponse({"success": False, "message": "Fan topilmadi"}, status=404)

    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    name = data.get("name", "").strip().replace("‘", "'").replace("’", "'").replace("`", "'").replace("'", "'").strip()
    code = data.get("code", "").strip()
    degree_level = data.get("degree_level", "bachelor").strip()
    description = data.get("description", "").strip()
    is_active = data.get("is_active", True)

    if not name:
        return JsonResponse({"success": False, "message": "Fan nomi majburiy"}, status=400)

    if degree_level not in dict(Subject.DEGREE_LEVEL_CHOICES):
        degree_level = 'bachelor'

    if Subject.objects.filter(name=name).exclude(pk=pk).exists():
        return JsonResponse({"success": False, "message": "Bunday nomli fan allaqachon mavjud"}, status=400)

    subject.name = name
    subject.code = code
    subject.degree_level = degree_level
    subject.description = description
    subject.is_active = bool(is_active)
    subject.save()

    return JsonResponse({"success": True, "message": "Fan muvaffaqiyatli tahrirlandi"})



@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_delete_subject(request, pk):
    from camera.models import Subject
    try:
        subject = Subject.objects.get(pk=pk)
    except Subject.DoesNotExist:
        return JsonResponse({"success": False, "message": "Fan topilmadi"}, status=404)

    name = subject.name
    subject.delete()
    return JsonResponse({"success": True, "message": f"'{name}' fani muvaffaqiyatli o'chirildi"})


# ==========================================
# 📅 Lesson Schedules (Dars jadvallari) Views & APIs
# ==========================================

@login_required(login_url='login')
def lesson_schedule_list_view(request):
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    from users.models import AcademicGroup, Faculty

    # 1. Fetch faculties that have at least one active schedule
    active_faculty_ids = AcademicGroup.objects.filter(schedules__isnull=False).values_list('faculty_id', flat=True).distinct()
    faculties = Faculty.objects.filter(id__in=active_faculty_ids).order_by('name')

    selected_faculty_id = request.GET.get('faculty', '').strip()
    selected_group_id = request.GET.get('academic_group', '').strip()

    # 2. Fetch academic groups that have at least one active schedule
    academic_groups = AcademicGroup.objects.filter(schedules__isnull=False).distinct().order_by('name')
    if selected_faculty_id:
        academic_groups = academic_groups.filter(faculty_id=selected_faculty_id)

    # All lists for empty-slot additions and modal selectors
    subjects = Subject.objects.filter(is_active=True).order_by('name')
    auditoriums = Auditorium.objects.filter(is_active=True).order_by('name')
    lesson_pairs = LessonPair.objects.all().order_by('shift', 'pair_number')

    from django.contrib.auth import get_user_model
    User = get_user_model()
    employees = User.objects.filter(role=User.Role.EMPLOYEE).order_by('full_name')

    calendar_data = []
    selected_group = None

    if selected_group_id:
        try:
            selected_group = AcademicGroup.objects.get(id=selected_group_id)
            # If group is selected, check if its faculty is pre-selected
            if not selected_faculty_id and selected_group.faculty:
                selected_faculty_id = str(selected_group.faculty.id)
                academic_groups = AcademicGroup.objects.filter(schedules__isnull=False, faculty_id=selected_faculty_id).distinct().order_by('name')
        except AcademicGroup.DoesNotExist:
            selected_group = None

    # 3. Construct weekly calendar grid
    group_schedules = []
    if selected_faculty_id and selected_group:
        group_schedules = LessonSchedule.objects.filter(academic_group=selected_group).select_related('subject', 'auditorium', 'lesson_pair')
        for pair in lesson_pairs:
            row = {
                'pair': pair,
                'days': []
            }
            for day_val, _ in LessonSchedule.WEEKDAY_CHOICES:
                row['days'].append(group_schedules.filter(weekday=day_val, lesson_pair=pair).first())
            calendar_data.append(row)

    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Dars jadvallari', 'url': None},
    ]

    stats = {
        "total": LessonSchedule.objects.count(),
        "groups_count": LessonSchedule.objects.values('academic_group').distinct().count(),
        "auditoriums_count": LessonSchedule.objects.values('auditorium').distinct().count(),
    }

    context = {
        'breadcrumbs': breadcrumbs,
        'faculties': faculties,
        'academic_groups': academic_groups,
        'subjects': subjects,
        'auditoriums': auditoriums,
        'employees': employees,
        'weekdays': LessonSchedule.WEEKDAY_CHOICES,
        'lesson_pairs': lesson_pairs,
        'selected_faculty_id': selected_faculty_id,
        'selected_group_id': selected_group_id,
        'selected_group': selected_group,
        'calendar_data': calendar_data,
        'group_schedules': group_schedules,
        'stats': stats,
    }
    return render(request, 'cameras/lesson_schedule_list.html', context)



@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_lesson_schedule(request):
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    from users.models import AcademicGroup
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    group_id = data.get("academic_group_id")
    subject_id = data.get("subject_id")
    auditorium_id = data.get("auditorium_id")
    teacher_name = data.get("teacher_name", "").strip()
    weekday_raw = data.get("weekday")
    lesson_pair_id = data.get("lesson_pair_id") or data.get("pair_number")
    lesson_type = data.get("lesson_type") or "lecture"

    if not group_id or not subject_id or not auditorium_id or not weekday_raw or not lesson_pair_id:
        return JsonResponse({"success": False, "message": "Barcha maydonlarni to'ldirish majburiy"}, status=400)

    try:
        weekday = int(weekday_raw)
        lesson_pair_id = int(lesson_pair_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Hafta kuni va Para raqami butun son bo'lishi kerak"}, status=400)

    try:
        group = AcademicGroup.objects.get(id=group_id)
        subject = Subject.objects.get(id=subject_id)
        auditorium = Auditorium.objects.get(id=auditorium_id)
        lesson_pair = LessonPair.objects.get(id=lesson_pair_id)
    except (AcademicGroup.DoesNotExist, Subject.DoesNotExist, Auditorium.DoesNotExist, LessonPair.DoesNotExist):
        return JsonResponse({"success": False, "message": "Bog'langan ma'lumotlar topilmadi"}, status=400)

    # Guruhning o'sha kungi o'sha parasida darsi bormi
    if LessonSchedule.objects.filter(academic_group=group, weekday=weekday, lesson_pair=lesson_pair).exists():
        return JsonResponse({"success": False, "message": f"{group.name} uchun ushbu vaqtda dars allaqachon belgilangan"}, status=400)

    # Auditoriyaning o'sha kungi o'sha parasida darsi bormi
    if LessonSchedule.objects.filter(auditorium=auditorium, weekday=weekday, lesson_pair=lesson_pair).exists():
        conflicting_schedule = LessonSchedule.objects.filter(auditorium=auditorium, weekday=weekday, lesson_pair=lesson_pair).first()
        return JsonResponse({
            "success": False, 
            "message": f"Ushbu auditoriya band! {conflicting_schedule.academic_group.name} guruhi darsi bor."
        }, status=400)

    schedule = LessonSchedule.objects.create(
        academic_group=group,
        subject=subject,
        auditorium=auditorium,
        teacher_name=teacher_name,
        weekday=weekday,
        lesson_pair=lesson_pair,
        lesson_type=lesson_type
    )

    return JsonResponse({"success": True, "message": "Dars jadvali muvaffaqiyatli qo'shildi!"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_lesson_schedule(request, pk):
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    from users.models import AcademicGroup
    try:
        schedule = LessonSchedule.objects.get(pk=pk)
    except LessonSchedule.DoesNotExist:
        return JsonResponse({"success": False, "message": "Dars jadvali topilmadi"}, status=404)

    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    group_id = data.get("academic_group_id")
    subject_id = data.get("subject_id")
    auditorium_id = data.get("auditorium_id")
    teacher_name = data.get("teacher_name", "").strip()
    weekday_raw = data.get("weekday")
    lesson_pair_id = data.get("lesson_pair_id") or data.get("pair_number")
    lesson_type = data.get("lesson_type") or "lecture"

    if not group_id or not subject_id or not auditorium_id or not weekday_raw or not lesson_pair_id:
        return JsonResponse({"success": False, "message": "Barcha maydonlarni to'ldirish majburiy"}, status=400)

    try:
        weekday = int(weekday_raw)
        lesson_pair_id = int(lesson_pair_id)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Hafta kuni va Para raqami butun son bo'lishi kerak"}, status=400)

    try:
        group = AcademicGroup.objects.get(id=group_id)
        subject = Subject.objects.get(id=subject_id)
        auditorium = Auditorium.objects.get(id=auditorium_id)
        lesson_pair = LessonPair.objects.get(id=lesson_pair_id)
    except (AcademicGroup.DoesNotExist, Subject.DoesNotExist, Auditorium.DoesNotExist, LessonPair.DoesNotExist):
        return JsonResponse({"success": False, "message": "Bog'langan ma'lumotlar topilmadi"}, status=400)

    # Guruhning boshqa darsi bormi
    if LessonSchedule.objects.filter(academic_group=group, weekday=weekday, lesson_pair=lesson_pair).exclude(pk=pk).exists():
        return JsonResponse({"success": False, "message": f"{group.name} uchun ushbu vaqtda dars allaqachon belgilangan"}, status=400)

    # Auditoriya bandmi
    if LessonSchedule.objects.filter(auditorium=auditorium, weekday=weekday, lesson_pair=lesson_pair).exclude(pk=pk).exists():
        conflicting_schedule = LessonSchedule.objects.filter(auditorium=auditorium, weekday=weekday, lesson_pair=lesson_pair).exclude(pk=pk).first()
        return JsonResponse({
            "success": False, 
            "message": f"Ushbu auditoriya band! {conflicting_schedule.academic_group.name} guruhi darsi bor."
        }, status=400)

    schedule.academic_group = group
    schedule.subject = subject
    schedule.auditorium = auditorium
    schedule.teacher_name = teacher_name
    schedule.weekday = weekday
    schedule.lesson_pair = lesson_pair
    if lesson_type in ['lecture', 'seminar', 'lab']:
        schedule.lesson_type = lesson_type
    schedule.save()

    return JsonResponse({"success": True, "message": "Dars jadvali muvaffaqiyatli tahrirlandi!"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_delete_lesson_schedule(request, pk):
    from camera.models import LessonSchedule
    try:
        schedule = LessonSchedule.objects.get(pk=pk)
    except LessonSchedule.DoesNotExist:
        return JsonResponse({"success": False, "message": "Dars jadvali topilmadi"}, status=404)

    group_name = schedule.academic_group.name
    subject_name = schedule.subject.name
    schedule.delete()
    return JsonResponse({"success": True, "message": f"'{group_name} - {subject_name}' dars jadvali muvaffaqiyatli o'chirildi"})


@login_required(login_url='login')
def lesson_schedule_add_view(request):
    from camera.models import Subject, Auditorium, LessonSchedule, LessonPair
    from users.models import Faculty

    faculties = Faculty.objects.all().order_by('name')
    subjects = Subject.objects.filter(is_active=True).order_by('name')
    auditoriums = Auditorium.objects.filter(is_active=True).order_by('name')
    lesson_pairs = LessonPair.objects.all().order_by('shift', 'pair_number')

    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Dars jadvallari', 'url': '/schedules/'},
        {'name': 'Yangi dars qo\'shish', 'url': None},
    ]

    context = {
        'breadcrumbs': breadcrumbs,
        'faculties': faculties,
        'subjects': subjects,
        'auditoriums': auditoriums,
        'weekdays': LessonSchedule.WEEKDAY_CHOICES,
        'lesson_pairs': lesson_pairs,
    }
    return render(request, 'cameras/lesson_schedule_add.html', context)


@login_required(login_url='login')
def api_get_faculty_groups(request, faculty_id):
    from users.models import AcademicGroup
    groups = AcademicGroup.objects.filter(faculty_id=faculty_id).order_by('name')
    if request.GET.get('has_schedules') == 'true':
        groups = groups.filter(schedules__isnull=False).distinct()
    data = [{"id": g.id, "name": g.name} for g in groups]
    return JsonResponse({"success": True, "groups": data})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_add_lesson_pair(request):
    from camera.models import LessonPair
    from datetime import datetime
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)

    shift_raw = data.get("shift")
    pair_number_raw = data.get("pair_number")
    start_time_str = data.get("start_time", "").strip()
    end_time_str = data.get("end_time", "").strip()

    if not shift_raw or not pair_number_raw or not start_time_str or not end_time_str:
        return JsonResponse({"success": False, "message": "Barcha maydonlarni to'ldirish majburiy"}, status=400)

    try:
        shift = int(shift_raw)
        pair_number = int(pair_number_raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Smena va Para raqami butun son bo'lishi kerak"}, status=400)

    try:
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        end_time = datetime.strptime(end_time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"success": False, "message": "Vaqt formati xato (masalan: 08:30)"}, status=400)

    if LessonPair.objects.filter(shift=shift, pair_number=pair_number).exists():
        return JsonResponse({"success": False, "message": f"{shift}-smenada {pair_number}-para allaqachon mavjud"}, status=400)

    pair = LessonPair.objects.create(
        shift=shift,
        pair_number=pair_number,
        start_time=start_time,
        end_time=end_time
    )

    return JsonResponse({
        "success": True, 
        "message": f"{pair.get_shift_display()} - {pair.pair_number}-para muvaffaqiyatli qo'shildi!"
    })


@login_required(login_url='login')
def lesson_pair_list_view(request):
    from camera.models import LessonPair
    pairs = LessonPair.objects.all().order_by('shift', 'pair_number')
    
    pairs_data = []
    for p in pairs:
        is_used = p.schedules.exists()
        pairs_data.append({
            'pair': p,
            'is_used': is_used
        })
        
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Dars jadvallari', 'url': '/schedules/'},
        {'name': 'Dars vaqtlari (Paralar)', 'url': None},
    ]
    
    context = {
        'breadcrumbs': breadcrumbs,
        'pairs_data': pairs_data,
    }
    return render(request, 'cameras/lesson_pair_list.html', context)


@login_required(login_url='login')
def lesson_pair_add_view(request):
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Dars jadvallari', 'url': '/schedules/'},
        {'name': 'Dars vaqtlari', 'url': '/schedules/pairs/'},
        {'name': 'Yangi dars vaqti qo\'shish', 'url': None},
    ]
    context = {
        'breadcrumbs': breadcrumbs,
    }
    return render(request, 'cameras/lesson_pair_add.html', context)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_lesson_pair(request, pk):
    from camera.models import LessonPair
    from datetime import datetime
    try:
        pair = LessonPair.objects.get(pk=pk)
    except LessonPair.DoesNotExist:
        return JsonResponse({"success": False, "message": "Dars vaqti topilmadi"}, status=404)
        
    if pair.schedules.exists():
        return JsonResponse({"success": False, "message": "Ushbu dars vaqti (para) dars jadvaliga biriktirilgan! Uni o'zgartirish mumkin emas."}, status=400)
        
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    shift_raw = data.get("shift")
    pair_number_raw = data.get("pair_number")
    start_time_str = data.get("start_time", "").strip()
    end_time_str = data.get("end_time", "").strip()
    
    if not shift_raw or not pair_number_raw or not start_time_str or not end_time_str:
        return JsonResponse({"success": False, "message": "Barcha maydonlarni to'ldirish majburiy"}, status=400)
        
    try:
        shift = int(shift_raw)
        pair_number = int(pair_number_raw)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Smena va Para raqami butun son bo'lishi kerak"}, status=400)
        
    try:
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        end_time = datetime.strptime(end_time_str, "%H:%M").time()
    except ValueError:
        return JsonResponse({"success": False, "message": "Vaqt formati xato (masalan: 08:30)"}, status=400)
        
    if LessonPair.objects.filter(shift=shift, pair_number=pair_number).exclude(pk=pk).exists():
        return JsonResponse({"success": False, "message": f"{shift}-smenada {pair_number}-para allaqachon mavjud"}, status=400)
        
    pair.shift = shift
    pair.pair_number = pair_number
    pair.start_time = start_time
    pair.end_time = end_time
    pair.save()
    
    return JsonResponse({"success": True, "message": "Dars vaqti muvaffaqiyatli tahrirlandi!"})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_delete_lesson_pair(request, pk):
    from camera.models import LessonPair
    try:
        pair = LessonPair.objects.get(pk=pk)
    except LessonPair.DoesNotExist:
        return JsonResponse({"success": False, "message": "Dars vaqti topilmadi"}, status=404)
        
    if pair.schedules.exists():
        return JsonResponse({"success": False, "message": "Ushbu dars vaqti (para) dars jadvaliga biriktirilgan! Uni o'chirish mumkin emas."}, status=400)
        
    pair.delete()
    return JsonResponse({"success": True, "message": "Dars vaqti muvaffaqiyatli o'chirildi!"})


@login_required(login_url='login')
def api_search_employees(request):
    from users.models import CustomUser
    from django.db.models import Q
    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({"success": True, "results": []})
        
    words = query.split()
    q_objects = Q(role=CustomUser.Role.EMPLOYEE)
    for word in words:
        q_objects &= Q(full_name__icontains=word)
        
    employees = CustomUser.objects.filter(q_objects).only('full_name', 'position', 'department_name')[:15]
    
    results = []
    for emp in employees:
        results.append({
            'full_name': emp.full_name,
            'position': emp.position or '',
            'department': emp.department_name or ''
        })
        
    return JsonResponse({"success": True, "results": results})


@login_required(login_url='login')
def lesson_process_view(request):
    from django.shortcuts import redirect
    return redirect('/lesson/v1/')


@login_required(login_url='login')
def live_lesson_view(request, schedule_id):
    from django.shortcuts import redirect
    return redirect('/lesson/v1/')


def _get_sentence_embedding(text):
    import numpy as np
    try:
        from transformers import AutoTokenizer, AutoModel
        import torch
        import os
        
        model_name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        cache_dir = "/tmp/embedding_cache"
        os.makedirs(cache_dir, exist_ok=True)
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(model_name, cache_dir=cache_dir)
        model = AutoModel.from_pretrained(model_name, cache_dir=cache_dir).to(device)
        
        inputs = tokenizer(text, padding=True, truncation=True, max_length=512, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            outputs = model(**inputs)
            embeddings = outputs.last_hidden_state.mean(dim=1)
        emb = embeddings[0].cpu().numpy()
        return emb / (np.linalg.norm(emb) + 1e-10)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"[Embedding] Failed to load offline sentence transformer: {e}. Falling back to token similarity.")
        return None

def calculate_semantic_similarity(topic, text):
    """Calculate cosine similarity between topic and teacher speech. Returns 0-100."""
    import numpy as np
    if not topic or not text:
        return 0
    if len(text.strip().split()) < 5:  # Too few words to judge
        return 0
        
    emb_t = _get_sentence_embedding(topic)
    emb_s = _get_sentence_embedding(text)
    
    if emb_t is not None and emb_s is not None:
        # Cosine similarity is already normalized since embeddings are unit vectors
        similarity = float(np.dot(emb_t, emb_s))  # Range: -1 to 1
        # Map from [0.05, 0.75] → [0, 100] realistically for multilingual contexts
        score = int(max(0, min(100, (similarity - 0.05) / 0.70 * 100)))
        return score
        
    # Fallback to keyword Jaccard similarity — realistic scoring
    topic_words = set(topic.lower().split())
    text_words = set(text.lower().split())
    if not topic_words:
        return 0
    # What fraction of topic keywords appear in the teacher's speech?
    matches = topic_words.intersection(text_words)
    keyword_coverage = len(matches) / len(topic_words)
    # Also check reverse: how much of text is related to topic
    if text_words:
        text_topic_ratio = len(matches) / max(1, len(text_words)) * 5  # scale up
    else:
        text_topic_ratio = 0
    score = int(max(0, min(100, (keyword_coverage * 0.7 + text_topic_ratio * 0.3) * 100)))
    return score

def analyze_lesson_llm(topic, text, relevance_score):
    """Analyze teacher's speech using OpenRouter LLM.
    Returns (relevance%, distractions_count, professionalism_str, description_str, portrait_dict, teacher_activity, student_spoken_pct).
    """
    import json
    import requests
    import logging
    logger = logging.getLogger(__name__)

    text_for_llm = text[:3000] + ("..." if len(text) > 3000 else "")

    prompt = f"""Siz ta'lim sifatini baholovchi mutaxasssissiz. Quyidagi ma'lumotlarni diqqat bilan tahlil qiling.

Dars mavzusi: {topic}

O'qituvchi nutqi:
\"\"\"{text_for_llm}\"\"\"

Quyidagi mezonlar bo'yicha baholang va faqat JSON formatida javob bering:
1. mavzu_amal_qilish_foiz: 0-100 oraliq son. Dars mavzusiga oid dasturlash tillari, algoritmlar, AKT tushunchalari mavzuga oid deb hisoblang. Faqat shaxsiy/maishiy mavzular chetlashish.
2. shaxsiy_hayot_chalgish_soni: maishiy mavzular necha marta eslatildi.
3. metodika_professionalmi: true yoki false.
4. oqituvchi_faollik_balli: 0-100 oraliq son. O'qituvchi qanchalik faol, izchil va aniq gapirdi.
5. tushuntirish: O'zbek tilida 2-4 jumladan iborat professional xulosa.
6. pedagogik_portret: har bir juftdan BITTA tanlab ber:
   - yonalish: "Talabaga yo'naltirilgan" yoki "Ma'ruzaga yo'naltirilgan"
   - markaz: "Talaba markazda" yoki "O'qituvchi markazda"
   - uslub: "Interaktiv" yoki "Passiv"
   - mavzu_sifat: "Mavzuga yuqori darajada mos" yoki "Mavzuni yoritish sifati past"

FAQAT quyidagi JSON formatida javob bering (boshqa matn bo'lmasin):
{{"mavzu_amal_qilish_foiz": 85, "shaxsiy_hayot_chalgish_soni": 1, "metodika_professionalmi": true, "oqituvchi_faollik_balli": 78, "tushuntirish": "xulosa", "pedagogik_portret": {{"yonalish": "Talabaga yo'naltirilgan", "markaz": "Talaba markazda", "uslub": "Interaktiv", "mavzu_sifat": "Mavzuga yuqori darajada mos"}}}}"""

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        try:
            payload = {
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.15,
                "response_format": {"type": "json_object"}
            }
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://smartgate.uz",
                "X-Title": "Smartgate CAMERA Lesson Analysis"
            }
            response = requests.post(url, json=payload, headers=headers, timeout=20.0)
            if response.status_code == 200:
                response_text = response.json()["choices"][0]["message"]["content"].strip()
                parsed = json.loads(response_text)

                foiz = parsed.get("mavzu_amal_qilish_foiz")
                if foiz is None:
                    foiz = relevance_score if relevance_score > 0 else (80 if len(text.strip().split()) > 15 else 50)
                else:
                    foiz = int(max(0, min(100, foiz)))

                teacher_activity = int(max(0, min(100, parsed.get("oqituvchi_faollik_balli", 75))))

                tushun = parsed.get("tushuntirish", "").strip()
                if len(tushun) < 5:
                    tushun = f"O'qituvchi dars mavzusiga {foiz}% darajada amal qildi."

                portret = parsed.get("pedagogik_portret", {})
                portrait = {
                    "yonalish": portret.get("yonalish", "Talabaga yo'naltirilgan"),
                    "markaz": portret.get("markaz", "Talaba markazda"),
                    "uslub": portret.get("uslub", "Interaktiv"),
                    "mavzu_sifat": portret.get("mavzu_sifat", "Mavzuga yuqori darajada mos"),
                }

                return (
                    foiz,
                    int(max(0, parsed.get("shaxsiy_hayot_chalgish_soni", 0))),
                    "Professional" if parsed.get("metodika_professionalmi", True) else "Qoniqarsiz",
                    tushun,
                    portrait,
                    teacher_activity,
                )
            else:
                logger.warning(f"[LLM] OpenRouter status {response.status_code}: {response.text[:200]}")
        except Exception as err:
            logger.warning(f"[LLM] OpenRouter API call failed: {err}")

    # Rule-based fallback
    distraction_keywords = [
        "hayotimda", "uyda", "oilam", "o'g'lim", "qizim", "mashinam", "xotinim",
        "erim", "bolalarim", "mashina oldim", "uy qurdik", "bozorda", "dam oldik",
        "to'yda", "chalg'ib",
    ]
    distractions = sum(text.lower().count(kw) for kw in distraction_keywords)
    distractions = min(5, distractions)
    is_professional = distractions < 3 and relevance_score >= 60
    professionalism = "Professional" if is_professional else "Qoniqarsiz"

    if relevance_score >= 85:
        tushuntirish = f"O'qituvchi dars mavzusiga yuqori darajada ({relevance_score}%) amal qildi."
    elif relevance_score >= 65:
        tushuntirish = f"O'qituvchi dars mavzusiga asosan amal qildi ({relevance_score}%)."
    else:
        tushuntirish = f"O'qituvchi nutqida dars mavzusidan chetlashishlar kuzatildi ({relevance_score}%)."

    portrait_fallback = {
        "yonalish": "Talabaga yo'naltirilgan" if relevance_score >= 70 else "Ma'ruzaga yo'naltirilgan",
        "markaz": "Talaba markazda" if relevance_score >= 70 else "O'qituvchi markazda",
        "uslub": "Interaktiv" if distractions < 2 else "Passiv",
        "mavzu_sifat": "Mavzuga yuqori darajada mos" if relevance_score >= 75 else "Mavzuni yoritish sifati past",
    }
    return relevance_score, distractions, professionalism, tushuntirish, portrait_fallback, 70


def _analyze_lesson_no_speech_llm(teacher_name, lesson_topic, present_cnt, absent_cnt, total_students):
    """
    Nutq transkripsiyasi mavjud bo'lmaganda ham LLM orqali dars xulosasini tuzadi.
    Davomat va dars mavzusi ma'lumotlari asosida.
    """
    import requests
    import logging
    logger = logging.getLogger(__name__)

    attendance_rate = int(present_cnt / total_students * 100) if total_students > 0 else 0
    prompt = f"""Siz ta'lim sifatini baholovchi ekspertsiz. Quyidagi ma'lumotlar asosida O'zbek tilida qisqa va professional xulosa yozing.

O'qituvchi: {teacher_name}
Fan / Dars mavzusi: {lesson_topic}
Guruhda jami talabalar: {total_students} nafar
Darsga kelgan: {present_cnt} nafar ({attendance_rate}%)
Darsga kelmagan: {absent_cnt} nafar

Eslatma: Bu darsda mikrofon yozuvi bo'lmadi — faqat davomat ma'lumotlari mavjud.

Xulosa qisqa va faktlarga asoslangan bo'lsin. Talabalar davomati, dars saviyasi haqida fikr bildiring. 2-4 jumladan iborat bo'lsin. JSON format shart emas — oddiy matn yozing."""

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    try:
        payload = {
            "model": "google/gemini-2.5-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://smartgate.uz",
            "X-Title": "Smartgate Lesson No-Speech Analysis"
        }
        response = requests.post(url, json=payload, headers=headers, timeout=12.0)
        if response.status_code == 200:
            result = response.json()["choices"][0]["message"]["content"].strip()
            return result
        else:
            logger.warning(f"[NoSpeechLLM] OpenRouter status {response.status_code}")
    except Exception as err:
        logger.warning(f"[NoSpeechLLM] OpenRouter failed: {err}")

    # Fallback
    if attendance_rate >= 80:
        return (f"{teacher_name} dars o'tkazdi. Guruhning {attendance_rate}% ({present_cnt}/{total_students}) talaba darsga keldi — bu yaxshi davomat ko'rsatkichi. "
                f"Mikrofon yozuvi bo'lmagani sababli nutq tahlili amalga oshirilmadi.")
    else:
        return (f"{teacher_name} dars o'tkazdi. Guruhning atigi {attendance_rate}% ({present_cnt}/{total_students}) talaba darsga keldi — "
                f"davomat ko'rsatkichi past. Talabalar kelmasligi sabablarini aniqlash va choralar ko'rish tavsiya etiladi. "
                f"Mikrofon yozuvi bo'lmagani sababli nutq tahlili amalga oshirilmadi.")


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_save_lesson_summary(request, schedule_id):
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from camera.models import LessonSchedule, LessonSummary
    from django.utils import timezone
    import json
    import os
    import glob
    
    schedule = get_object_or_404(LessonSchedule, pk=schedule_id)
    
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    summary_text = data.get("summary_text", "").strip()
    session_id = data.get("session_id")
    
    if not summary_text:
        return JsonResponse({"success": False, "message": "Xulosa matnini yozish majburiy"}, status=400)
        
    today = timezone.localdate()
    
    # AI va RAG/LLM dars mavzusi tahlili
    relevance_score = 100
    distractions_count = 0
    professionalism = "Professional"
    segments_list = []
    
    # STT matnini yuklash va tahlil qilish
    segments_path = None
    if session_id:
        session_id = "".join([c for c in session_id if c.isalnum() or c in ('_', '-')])
        path_candidate = f"/tmp/stt_{session_id}_segments.json"
        if os.path.exists(path_candidate):
            segments_path = path_candidate
            
    if not segments_path:
        # Fallback: schedule_id orqali oxirgi faol sessionni qidirish
        candidates = glob.glob(f"/tmp/stt_stt_{schedule_id}_*_segments.json")
        if candidates:
            segments_path = max(candidates, key=os.path.getmtime)
            
    teacher_phrases = []
    if segments_path and os.path.exists(segments_path):
        try:
            with open(segments_path, "r") as f:
                segments_list = json.load(f)
                
            for seg in segments_list:
                # O'qituvchi gapirgan qismlarni yig'amiz
                speaker_tag = seg.get('speaker', '')
                teacher_name_check = schedule.teacher_name or ""
                if ("O'qituvchi" in speaker_tag or "Spiker 0" in speaker_tag or
                        (teacher_name_check and teacher_name_check.lower() in speaker_tag.lower())):
                    teacher_phrases.append(seg.get('text', ''))
        except Exception as file_err:
            logger.error(f"[Summary AI] Segments file read failed: {file_err}")
            
    teacher_text = " ".join(teacher_phrases).strip()
    lesson_topic = schedule.subject.description or schedule.subject.name
    
    if teacher_text:
        # 1. Cosine similarity calculated with sentence-transformers
        relevance_score = calculate_semantic_similarity(lesson_topic, teacher_text)
        # 2. Llama 3 / Mistral-7B semantic analyzer
        relevance_score, distractions_count, professionalism, ai_desc = analyze_lesson_llm(
            lesson_topic, teacher_text, relevance_score
        )
        # Append AI analysis to summary text
        summary_text += f"\n\n--- 🤖 Sun'iy Intellekt va RAG Tahlili ---\n" \
                        f"• Mavzuga aloqadorlik darajasi: {relevance_score}%\n" \
                        f"• Shaxsiy hayotga chalg'ish: {distractions_count} marta\n" \
                        f"• Metodika professionalizmi: {professionalism}\n" \
                        f"• Tahlil xulosasi: {ai_desc}"
                        
    summary, created = LessonSummary.objects.update_or_create(
        schedule=schedule,
        date=today,
        defaults={
            'summary_text': summary_text,
            'relevance_score': relevance_score,
            'personal_life_distractions': distractions_count,
            'professionalism_rating': professionalism,
            'diarized_transcript': segments_list
        }
    )
    
    return JsonResponse({
        "success": True, 
        "message": "Dars xulosasi va AI tahlili muvaffaqiyatli saqlandi! Dars yakunlandi."
    })


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_get_lesson_analysis(request, schedule_id):
    """Professional lesson analysis API — returns full per-speaker stats, camera visibility, LLM scoring."""
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from camera.live_attendance import get_live_attendance
    from camera.models import LessonSchedule, LessonSession
    from users.models import CustomUser
    from attendance.models import Attendance
    from django.utils import timezone
    import json, os, glob, time, logging

    logger = logging.getLogger(__name__)
    schedule = get_object_or_404(LessonSchedule, pk=schedule_id)
    today = timezone.localdate()

    # ── 1. Find segments file ──────────────────────────────────────────────────
    # Pattern: /tmp/stt_stt_7_1780377..._segments.json
    candidates = glob.glob(f"/tmp/stt_stt_{schedule_id}_*_segments.json")
    # Also try plain session pattern
    candidates += glob.glob(f"/tmp/stt_{schedule_id}_*_segments.json")
    segments_path = max(candidates, key=os.path.getmtime) if candidates else None

    segments_list = []
    if segments_path and os.path.exists(segments_path):
        try:
            with open(segments_path, "r") as f:
                segments_list = json.load(f)
        except Exception as file_err:
            logger.error(f"[Analysis AI] Segments file read failed: {file_err}")

    # ── 2. Per-speaker breakdown ───────────────────────────────────────────────
    teacher_name = schedule.teacher_name or "O'qituvchi"
    teacher_segments = []
    student_segments = []
    speaker_word_counts = {}  # {speaker_name: word_count}

    for seg in segments_list:
        spk = seg.get('speaker', '').strip()
        txt = seg.get('text', '').strip()
        wc = len(txt.split()) if txt else 0
        speaker_word_counts[spk] = speaker_word_counts.get(spk, 0) + wc

        is_teacher = (
            "O'qituvchi" in spk or
            "Spiker 0" in spk or
            (teacher_name and teacher_name.lower() in spk.lower())
        )
        if is_teacher:
            teacher_segments.append(seg)
        else:
            student_segments.append(seg)

    teacher_text = " ".join(s.get('text', '') for s in teacher_segments).strip()
    total_words = sum(speaker_word_counts.values())
    teacher_word_count = sum(v for k, v in speaker_word_counts.items()
                             if "O'qituvchi" in k or "Spiker 0" in k or (teacher_name and teacher_name.lower() in k.lower()))
    student_word_count = total_words - teacher_word_count

    # Calculate talk-time ratio
    if total_words > 0:
        teacher_talk_pct = int(teacher_word_count / total_words * 100)
        student_talk_pct = 100 - teacher_talk_pct
    else:
        teacher_talk_pct = 0
        student_talk_pct = 0

    # Unique speaker names
    unique_speakers = list(speaker_word_counts.keys())

    # ── 3. Student attendance & camera visibility ──────────────────────────────
    students_qs = CustomUser.objects.filter(
        academic_group=schedule.academic_group,
        role=CustomUser.Role.STUDENT,
        is_superuser=False
    ).order_by('full_name')

    live_session = LessonSession.objects.filter(schedule=schedule, date=today).first()
    live_present_ids = get_live_attendance(live_session)["present_student_ids"]
    attendance_qs = Attendance.objects.filter(date=today, user__in=students_qs)
    attendance_map = {att.user_id: att for att in attendance_qs}

    # Who spoke (from STT segments)
    spoke_names = set()
    for seg in student_segments:
        spk = seg.get('speaker', '')
        if spk and spk not in ('Talaba', 'Spiker 1'):
            spoke_names.add(spk)

    students_report = []
    present_cnt = 0
    for std in students_qs:
        att = attendance_map.get(std.id)
        is_present = std.id in live_present_ids
        if is_present:
            present_cnt += 1
        full_name = std.full_name or std.username
        # Check if they spoke on mic
        spoke = any(
            full_name.lower() in s or (std.full_name and std.full_name.split()[0].lower() in s)
            for s in [sp.lower() for sp in spoke_names]
        )
        emotion = ""
        if att and hasattr(att, 'psychology') and att.psychology:
            emotion = att.psychology.dominant_emotion or ""
        students_report.append({
            "name": full_name,
            "present": is_present,
            "spoke": spoke,
            "emotion": emotion
        })

    absent_cnt = len(students_qs) - present_cnt
    total_students = len(students_qs)
    attendance_rate = int(present_cnt / total_students * 100) if total_students > 0 else 0

    # How many students spoke on mic
    spoke_cnt = sum(1 for s in students_report if s.get("spoke"))

    # ── 4. LLM-based semantic analysis ────────────────────────────────────────
    lesson_topic = schedule.subject.description or schedule.subject.name
    lesson_name = schedule.subject.name if schedule.subject else "Noma'lum fan"
    relevance_score = 0
    distractions_count = 0
    professionalism = "Ma'lumot yetarli emas"
    ai_desc = ""
    pedagogical_portrait = None
    teacher_activity_score = 0  # 0-100

    if teacher_text:
        relevance_score = calculate_semantic_similarity(lesson_topic, teacher_text)
        result = analyze_lesson_llm(lesson_topic, teacher_text, relevance_score)
        # New return: (foiz, distractions, professionalism, desc, portrait, teacher_activity)
        if len(result) == 6:
            relevance_score, distractions_count, professionalism, ai_desc, pedagogical_portrait, teacher_activity_score = result
        else:
            relevance_score, distractions_count, professionalism, ai_desc = result[:4]
    else:
        ai_desc = _analyze_lesson_no_speech_llm(
            teacher_name=teacher_name,
            lesson_topic=lesson_name,
            present_cnt=present_cnt,
            absent_cnt=absent_cnt,
            total_students=total_students,
        )
        professionalism = "Mikrofon tahlili mavjud emas"

    # ── 5. Speaker breakdown for frontend ─────────────────────────────────────
    speakers_detail = []
    for spk_name, wc in sorted(speaker_word_counts.items(), key=lambda x: -x[1]):
        is_t = (
            "O'qituvchi" in spk_name or "Spiker 0" in spk_name or
            (teacher_name and teacher_name.lower() in spk_name.lower())
        )
        speakers_detail.append({
            "name": spk_name,
            "role": "O'qituvchi" if is_t else "Talaba",
            "word_count": wc,
            "pct": int(wc / total_words * 100) if total_words > 0 else 0
        })

    # ── Time Management Analysis ─────────────────────────────────────────────
    import datetime as dt_mod
    teacher_planned_start = None
    teacher_actual_start = None
    teacher_actual_end = None
    teacher_late_minutes = 0
    end_status = "O'z vaqtida yakunlandi"
    end_diff_minutes = 0
    duration_minutes = None
    session_obj = None

    if schedule.lesson_pair and schedule.lesson_pair.start_time:
        teacher_planned_start = schedule.lesson_pair.start_time.strftime('%H:%M')
    planned_end_time = schedule.lesson_pair.end_time if schedule.lesson_pair else None
    planned_end_str = planned_end_time.strftime('%H:%M') if planned_end_time else None

    from camera.models import LessonSession
    try:
        session_obj = LessonSession.objects.get(schedule=schedule, date=today)
        if session_obj.teacher_started_at:
            teacher_actual_start = timezone.localtime(session_obj.teacher_started_at).strftime('%H:%M:%S')
        if session_obj.teacher_ended_at:
            teacher_actual_end = timezone.localtime(session_obj.teacher_ended_at).strftime('%H:%M:%S')
        late = session_obj.teacher_late_minutes
        teacher_late_minutes = late if late is not None else 0
        duration_minutes = session_obj.lesson_duration_minutes

        if session_obj.teacher_ended_at and planned_end_time:
            actual_end_time = timezone.localtime(session_obj.teacher_ended_at).time()
            diff_sec = (
                dt_mod.datetime.combine(today, planned_end_time) -
                dt_mod.datetime.combine(today, actual_end_time)
            ).total_seconds()
            if diff_sec > 120:
                end_status = "Dars vaqtidan oldin yakunlandi"
                end_diff_minutes = int(diff_sec // 60)
            elif diff_sec < -300:
                end_status = "Dars belgilangan vaqtdan ko'p davom etdi"
                end_diff_minutes = int(abs(diff_sec) // 60)
        elif not session_obj.teacher_ended_at:
            actual_end_time = timezone.localtime().time()
            if planned_end_time:
                diff_sec = (
                    dt_mod.datetime.combine(today, planned_end_time) -
                    dt_mod.datetime.combine(today, actual_end_time)
                ).total_seconds()
                if diff_sec < -300:
                    end_status = "Dars belgilangan vaqtdan ko'p davom etdi"
                    end_diff_minutes = int(abs(diff_sec) // 60)

    except LessonSession.DoesNotExist:
        teacher_user_obj = None
        if schedule.teacher_name:
            from users.models import CustomUser as CU2
            teacher_user_obj = CU2.objects.filter(
                full_name__icontains=schedule.teacher_name,
                role=CU2.Role.EMPLOYEE
            ).first()
        if teacher_user_obj:
            from attendance.models import Attendance as Att2
            teacher_att = Att2.objects.filter(date=today, user=teacher_user_obj).first()
            if teacher_att and teacher_att.entry_time and schedule.lesson_pair and schedule.lesson_pair.start_time:
                actual_start_dt = timezone.localtime(teacher_att.entry_time)
                teacher_actual_start = actual_start_dt.strftime('%H:%M:%S')
                planned_start_t = schedule.lesson_pair.start_time
                diff_sec = (
                    dt_mod.datetime.combine(today, actual_start_dt.time()) -
                    dt_mod.datetime.combine(today, planned_start_t)
                ).total_seconds()
                teacher_late_minutes = max(0, int(diff_sec // 60))
        if planned_end_time:
            actual_end_time = timezone.localtime().time()
            diff_sec = (
                dt_mod.datetime.combine(today, planned_end_time) -
                dt_mod.datetime.combine(today, actual_end_time)
            ).total_seconds()
            if diff_sec > 120:
                end_status = "Dars vaqtidan oldin yakunlandi"
                end_diff_minutes = int(diff_sec // 60)
            elif diff_sec < -300:
                end_status = "Dars belgilangan vaqtdan ko'p davom etdi"
                end_diff_minutes = int(abs(diff_sec) // 60)

    # ── 6. Composite scores ──────────────────────────────────────────────────
    # Talabalar faolligi: davomat + mikrofonda gapirish
    student_activity_score = int(
        attendance_rate * 0.7 +
        (spoke_cnt / total_students * 100 if total_students > 0 else 0) * 0.3
    )
    student_activity_score = min(100, max(0, student_activity_score))

    # Vaqtdan samarali foydalanish: kechikish va erta/kech yakunlash
    time_efficiency_score = 100
    time_efficiency_score -= min(50, teacher_late_minutes * 3)   # har 1 daqiqa kechikish → -3
    time_efficiency_score -= min(30, end_diff_minutes * 2)        # erta/kech yakunlash → -2/daqiqa
    time_efficiency_score = max(0, time_efficiency_score)

    # Agar nutq bo'lmasa teacher_activity_score davomat asosida
    if teacher_activity_score == 0 and total_words == 0:
        teacher_activity_score = max(0, 100 - teacher_late_minutes * 3)

    # Umumiy sifat balli (weighted)
    if total_words > 0:
        overall_quality = int(
            relevance_score * 0.35 +
            teacher_activity_score * 0.20 +
            student_activity_score * 0.25 +
            time_efficiency_score * 0.20
        )
    else:
        # Nutq yo'q — davomat va vaqt asosida
        overall_quality = int(
            student_activity_score * 0.60 +
            time_efficiency_score * 0.40
        )
    overall_quality = min(100, max(0, overall_quality))

    # Sifat yorlig'i
    if overall_quality >= 85:
        quality_label = "Yuqori sifat"
        quality_color = "success"
    elif overall_quality >= 70:
        quality_label = "Qoniqarli"
        quality_color = "info"
    elif overall_quality >= 50:
        quality_label = "O'rtacha"
        quality_color = "warning"
    else:
        quality_label = "Past sifat"
        quality_color = "error"

    # Pedagogical portrait default (nutq yo'q bo'lsa)
    if not pedagogical_portrait:
        pedagogical_portrait = {
            "yonalish": "Ma'ruzaga yo'naltirilgan",
            "markaz": "O'qituvchi markazda",
            "uslub": "Passiv",
            "mavzu_sifat": "Mavzuga yuqori darajada mos" if relevance_score >= 75 else "Mavzuni yoritish sifati past",
        }

    # Lesson type uchun o'zbek nomi
    lesson_type_labels = {'lecture': "Ma'ruza", 'seminar': 'Seminar', 'lab': 'Laboratoriya'}

    # To'liq response
    full_response = {
        "success": True,
        # ── Dars ma'lumoti ──
        "lesson_info": {
            "subject": lesson_name,
            "topic": schedule.topic or "Mavzu kiritilmagan",
            "teacher": teacher_name,
            "lesson_type": lesson_type_labels.get(schedule.lesson_type, schedule.lesson_type or "Ma'ruza"),
            "date": str(today),
            "duration_minutes": duration_minutes,
            "planned_start": teacher_planned_start,
            "planned_end": planned_end_str,
            "teacher_actual_start": teacher_actual_start,
            "teacher_actual_end": teacher_actual_end,
            "teacher_late_minutes": teacher_late_minutes,
            "end_status": end_status,
            "end_diff_minutes": end_diff_minutes,
            "present_count": present_cnt,
            "absent_count": absent_cnt,
            "total_students": total_students,
            "attendance_rate": attendance_rate,
        },
        # ── Umumiy natija ──
        "overall_quality": overall_quality,
        "quality_label": quality_label,
        "quality_color": quality_color,
        # ── Dars sifati 4 ko'rsatkich ──
        "relevance_score": relevance_score,
        "teacher_activity_score": teacher_activity_score,
        "student_activity_score": student_activity_score,
        "time_efficiency_score": time_efficiency_score,
        # ── Pedagogik portret ──
        "pedagogical_portrait": pedagogical_portrait,
        # ── AI Xulosa ──
        "ai_description": ai_desc,
        # ── Talk-time ──
        "teacher_talk_pct": teacher_talk_pct,
        "student_talk_pct": student_talk_pct,
        "teacher_word_count": teacher_word_count,
        "student_word_count": student_word_count,
        "total_words": total_words,
        # ── Batafsil ──
        "students_report": students_report,
        "speakers_detail": speakers_detail,
        "teacher_segment_count": len(teacher_segments),
        "student_segment_count": len(student_segments),
        "segments_file_found": segments_path is not None,
        # ── Eski maydonlar (backward compat) ──
        "distractions_count": distractions_count,
        "professionalism": professionalism,
        "lesson_topic": lesson_topic,
        "teacher_name": teacher_name,
        "teacher_planned_start": teacher_planned_start,
        "teacher_actual_start": teacher_actual_start,
        "teacher_actual_end": teacher_actual_end,
        "teacher_late_minutes": teacher_late_minutes,
        "duration_minutes": duration_minutes,
        "planned_end": planned_end_str,
        "end_status": end_status,
        "end_diff_minutes": end_diff_minutes,
    }

    # LessonSession ga saqlash
    if session_obj:
        try:
            session_obj.analysis_json = full_response
            session_obj.save(update_fields=['analysis_json', 'updated_at'])
        except Exception as e:
            logger.warning(f"[Analysis] Could not save to session: {e}")

    return JsonResponse(full_response)



def make_wav_header(pcm_bytes_len, sample_rate=16000, num_channels=1, bits_per_sample=16):
    import struct
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    
    header = bytearray(44)
    # RIFF header
    header[0:4] = b'RIFF'
    struct.pack_into('<I', header, 4, pcm_bytes_len + 36)
    header[8:12] = b'WAVE'
    # fmt chunk
    header[12:16] = b'fmt '
    struct.pack_into('<I', header, 16, 16) # Subchunk1Size
    struct.pack_into('<H', header, 20, 1)  # AudioFormat (1 = PCM)
    struct.pack_into('<H', header, 22, num_channels)
    struct.pack_into('<I', header, 24, sample_rate)
    struct.pack_into('<I', header, 28, byte_rate)
    struct.pack_into('<H', header, 32, block_align)
    struct.pack_into('<H', header, 34, bits_per_sample)
    # data chunk
    header[36:40] = b'data'
    struct.pack_into('<I', header, 40, pcm_bytes_len)
    
    return bytes(header)


def is_valid_language_text(text):
    import re
    text = text.strip()
    if not text:
        return False
    # Faqat Lotin, Kirill, raqamlar, probellar va darsda ishlatiladigan o'zbek, rus, ingliz tinish belgilariga ruxsat beramiz.
    allowed_chars = len(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9\s.,!?;:\-_()\"'`ʻʼ’‘“”]", text))
    if len(text) > 0 and (allowed_chars / len(text)) > 0.85:
        return True
    return False


def clean_hallucinations(text):
    import re
    if not text:
        return ""
        
    # 1. Clean standalone filler words and background noise markers case-sensitively
    # Clean music/noise brackets
    text = re.sub(r'[\(\[\{](musiqa|music|tovush|sound|singing|shovqin|laughter|applause|giggle|kulgi|qarsak)[\)\]\}]', '', text, flags=re.IGNORECASE)
    # Clean standalone music/filler words
    text = re.sub(r'\b(musiqa|music|mhm|ehm|e-e|uh-huh|uh|um|e-e|ah|oh|eh|xo\'p, ehm|xop, ehm|xo\'p ehm)\b[.,!?]*', '', text, flags=re.IGNORECASE)
    
    # Clean multiple spaces and strip
    text = re.sub(r'\s+', ' ', text).strip()
    if not text or len(text) < 2:
        return ""
        
    # Remove any stray leading/trailing dots/commas
    text = text.lstrip(".,!? ")
    
    # 3. Check for and eliminate consecutive repetitive phrase sequences (Whisper loop prevention)
    words = text.split()
    n = len(words)
    if n > 6:
        for length in range(3, min(15, n // 2 + 1)):
            for i in range(n - 2 * length + 1):
                chunk1 = words[i:i+length]
                chunk2 = words[i+length:i+2*length]
                if chunk1 == chunk2:
                    # Remove consecutive duplicate chunk
                    cleaned_words = words[:i+length] + words[i+2*length:]
                    text = " ".join(cleaned_words)
                    return clean_hallucinations(text) # Recursive call to clear multiple repeats
                    
    # 4. Blacklist checks (hallucinations and loop markers)
    t_clean = re.sub(r'[^\w\s]', '', text.lower()).strip()
    
    # Strict substring matching for common Whisper Uzbek hallucinations
    hallucination_phrases = [
        "oyinni bilmayapman", "o'yinni bilmayapman",
        "men ham aytmoqchiman", "men ham aytmoqchiman da",
        "boya aytib", "boya aytib qo'yaylik", "boya aytib qoyaylik",
        "hozir dayapman", "hozir dayapman ettiravering",
        "ettiravering", "nima deydi qarang", "aytmoqchiman da",
        "obuna bo'ling", "obuna boling", "layk bosing", "like bosing",
        "rahmat tomosha qilganingiz", "rahmat tomosha", "subtitrlar"
    ]
    for phrase in hallucination_phrases:
        if phrase in t_clean:
            return ""
            
    blacklist = {
        "subtitrlar", "subtitr", "tarjimon", "tarjima",
        "obuna boling", "obuna", "like bosing", "tashakkur", "katta rahmat", "sog boling",
        "prosmotr", "podpisivaytes", "spasibo", "thank you", "subscribe", "please subscribe", "thanks for watching"
    }
    if t_clean in blacklist:
        return ""
        
    return text


def cyrillic_to_latin(text: str) -> str:
    mapping = {
        'Ш': 'Sh', 'ш': 'sh',
        'Ч': 'Ch', 'ч': 'ch',
        'Ю': 'Yu', 'ю': 'yu',
        'Я': 'Ya', 'я': 'ya',
        'Ё': 'Yo', 'ё': 'yo',
        'Ў': "O'", 'ў': "o'",
        'Қ': 'Q', 'қ': 'q',
        'Ғ': "G'", 'ғ': "g'",
        'Ҳ': 'H', 'ҳ': 'h',
        'Ц': 'Ts', 'ц': 'ts',
        'Ж': 'J', 'ж': 'j',
        'А': 'A', 'а': 'a',
        'Б': 'B', 'б': 'b',
        'В': 'V', 'в': 'v',
        'Г': 'G', 'г': 'g',
        'Д': 'D', 'д': 'd',
        'З': 'Z', 'з': 'z',
        'И': 'I', 'и': 'i',
        'Й': 'Y', 'й': 'y',
        'К': 'K', 'к': 'k',
        'Л': 'L', 'л': 'l',
        'М': 'M', 'м': 'm',
        'Н': 'N', 'н': 'n',
        'О': 'O', 'о': 'o',
        'П': 'P', 'п': 'p',
        'Р': 'R', 'р': 'r',
        'С': 'S', 'с': 's',
        'Т': 'T', 'т': 't',
        'У': 'U', 'у': 'u',
        'Ф': 'F', 'ф': 'f',
        'Х': 'X', 'х': 'x',
        'Ъ': "'", 'ъ': "'",
        'Э': 'E', 'э': 'e',
        'Ы': 'I', 'ы': 'i',
        'Ь': '', 'ь': '',
    }
    
    vowels = set("АаОоУуИиЭэЎўЮюЯяЕеЁё")
    result = []
    
    for i, char in enumerate(text):
        if char == 'Е' or char == 'е':
            is_ye = False
            if i == 0:
                is_ye = True
            else:
                prev_char = text[i-1]
                if prev_char.isspace() or prev_char in vowels or prev_char in ".,!?;:-_()[]{}'\"":
                    is_ye = True
            
            if is_ye:
                result.append("Ye" if char == 'Е' else "ye")
            else:
                result.append("E" if char == 'Е' else "e")
        elif char in mapping:
            result.append(mapping[char])
        else:
            result.append(char)
            
    return "".join(result)


_speechbrain_model = None
_speechbrain_lock = threading.Lock()

_silero_vad_model = None
_silero_vad_utils = None
_silero_vad_lock = threading.Lock()

def _get_silero_vad():
    global _silero_vad_model, _silero_vad_utils
    if _silero_vad_model is not None:
        return _silero_vad_model, _silero_vad_utils
    with _silero_vad_lock:
        if _silero_vad_model is not None:
            return _silero_vad_model, _silero_vad_utils
        try:
            import torch
            model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                force_reload=False,
                onnx=False,
                trust_repo=True
            )
            if torch.cuda.is_available():
                model = model.to('cuda')
            _silero_vad_model = model
            _silero_vad_utils = utils
            import logging
            logger = logging.getLogger(__name__)
            logger.info("[STT VAD] PyTorch Silero VAD model loaded successfully on CUDA GPU.")
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error("[STT VAD] Failed to load PyTorch Silero VAD model: %s", e)
            _silero_vad_model = None
            _silero_vad_utils = None
    return _silero_vad_model, _silero_vad_utils


def _get_speechbrain_model():
    global _speechbrain_model
    if _speechbrain_model is not None:
        return _speechbrain_model
    with _speechbrain_lock:
        if _speechbrain_model is not None:
            return _speechbrain_model
        try:
            from speechbrain.inference.speaker import SpeakerRecognition
            import torch
            source_dir = os.path.join(os.path.dirname(__file__), "..", "speechbrain_models", "spkrec-ecapa-voxceleb")
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _speechbrain_model = SpeakerRecognition.from_hparams(source=source_dir, savedir=source_dir, run_opts={"device": device})
            logger.info(f"[STT Diarization] SpeechBrain ECAPA model loaded successfully on device={device}")
        except Exception as e:
            logger.error("[STT Diarization] Failed to load SpeechBrain ECAPA model: %s", e)
            _speechbrain_model = None
    return _speechbrain_model


def cluster_speakers(segments, teacher_name="O'qituvchi", teacher_visible=True):
    import numpy as np
    
    target_teacher_name = teacher_name if teacher_visible else "O'qituvchi"
    
    GENERIC_NAMES = {
        "spiker 0 (oʻqituvchi)", "spiker 0 (o'qituvchi)", 
        "spiker 1 (talaba)", "talaba", "o'qituvchi", "oʻqituvchi", 
        "spiker 0", "spiker 1", "yuz", ""
    }
    
    def is_generic(name):
        if not name:
            return True
        return name.lower().strip() in GENERIC_NAMES

    valid_segs = [s for s in segments if s.get('embedding') and len(s['embedding']) == 192]
    if len(valid_segs) < 2:
        for s in segments:
            curr_sp = s.get('speaker', '')
            if is_generic(curr_sp):
                s['speaker'] = target_teacher_name
        return segments
        
    embs = np.array([s['embedding'] for s in valid_segs])
    
    lengths = [s['end'] - s['start'] for s in valid_segs]
    idx_a = int(np.argmax(lengths))
    centroid_a = embs[idx_a]
    
    similarities = np.dot(embs, centroid_a)
    idx_b = int(np.argmin(similarities))
    
    # If all speakers sound almost identical, we default to 1 speaker
    if similarities[idx_b] > 0.85:
        for s in segments:
            curr_sp = s.get('speaker', '')
            if is_generic(curr_sp):
                s['speaker'] = target_teacher_name
        return segments
        
    centroid_b = embs[idx_b]
    
    # K-Means iterations
    for _ in range(5):
        sim_a = np.dot(embs, centroid_a)
        sim_b = np.dot(embs, centroid_b)
        labels = np.where(sim_a >= sim_b, 0, 1)
        
        cluster_a_embs = embs[labels == 0]
        cluster_b_embs = embs[labels == 1]
        
        if len(cluster_a_embs) > 0:
            mean_a = np.mean(cluster_a_embs, axis=0)
            centroid_a = mean_a / (np.linalg.norm(mean_a) + 1e-10)
        if len(cluster_b_embs) > 0:
            mean_b = np.mean(cluster_b_embs, axis=0)
            centroid_b = mean_b / (np.linalg.norm(mean_b) + 1e-10)
            
    duration_0 = sum(lengths[i] for i in range(len(valid_segs)) if labels[i] == 0)
    duration_1 = sum(lengths[i] for i in range(len(valid_segs)) if labels[i] == 1)
    
    teacher_cluster = 0 if duration_0 >= duration_1 else 1
    
    # Map label to speaker name while preserving non-generic recognized names
    for i, s in enumerate(valid_segs):
        curr_sp = s.get('speaker', '')
        if labels[i] == teacher_cluster:
            s['speaker'] = target_teacher_name
        else:
            if is_generic(curr_sp):
                s['speaker'] = "Talaba"
            # If s['speaker'] is a non-generic name, we preserve it!
            
    # Assign same labels to non-embedded short segments based on volume RMS
    for s in segments:
        if not s.get('embedding') or len(s['embedding']) != 192:
            curr_sp = s.get('speaker', '')
            if is_generic(curr_sp):
                rms = s.get('rms', 0.0)
                s['speaker'] = target_teacher_name if rms >= 0.025 else "Talaba"
            
    return segments


def clean_text_with_llama(text: str, lang: str = 'uz') -> str:
    if not text or len(text.strip()) < 2:
        return text
        
    import re
    cleaned = text.strip()
    
    if lang == 'uz':
        # 1. Clean contiguous repeating words (case-insensitive)
        # e.g., "talabalar lar" -> "talabalar", "salom salom" -> "salom"
        words = cleaned.split()
        cleaned_words = []
        for w in words:
            w_clean = re.sub(r'[^\w]', '', w).lower()
            if cleaned_words:
                last_clean = re.sub(r'[^\w]', '', cleaned_words[-1]).lower()
                # If current word is same as last word
                if w_clean == last_clean and len(w_clean) > 0:
                    continue
                # Specific check for common Uzbek stutters/reps like "lar" following "talabalar"
                if w_clean == "lar" and last_clean.endswith("lar"):
                    continue
            cleaned_words.append(w)
        cleaned = " ".join(cleaned_words)

        # 2. Advanced Dialect and Accent smoothing (toza adabiy o'zbek tiliga o'girish)
        # Suffix contractions (e.g. kelvotti -> kelyapti)
        cleaned = re.sub(r'\b(\w+)(?:votti|voti)\b', r'\1yapti', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(\w+)(?:vomiza|vomiz)\b', r'\1yapmiz', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(\w+)(?:vomman|vom)\b', r'\1yapman', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(\w+)(?:vossila|vossilar)\b', r'\1yapsizlar', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(\w+)(?:votti-da)\b', r'\1yapti-da', cleaned, flags=re.IGNORECASE)
        
        # Dialect word mapping
        dialect_map = {
            r'\bkelvotti\b': 'kelyapti',
            r'\bketvotti\b': 'ketyapti',
            r'\byozvoti\b': 'yozyapti',
            r'\bqilvoti\b': 'qilyapti',
            r'\bopti\b': 'olibdi',
            r'\bbopti\b': 'bo\'libdi',
            r'\bkepti\b': 'kelibdi',
            r'\bketipti\b': 'ketibdi',
            r'\bqip\b': 'qilib',
            r'\bberip\b': 'berib',
            r'\byozip\b': 'yozib',
            r'\bkelip\b': 'kelib',
            r'\bakajon\b': 'aka',
            r'\bukajon\b': 'uka',
            r'\bopajon\b': 'opa',
            r'\bsalomu\b': 'salom',
            r'\boptila\b': 'olibdilar',
            r'\bboptila\b': 'bo\'libdilar',
            r'\bkelvossila\b': 'kelyapsizlar',
            r'\bketvomman\b': 'ketyapman',
            r'\bqilvomiza\b': 'qilyapmiz',
        }
        for k, v in dialect_map.items():
            cleaned = re.sub(k, v, cleaned, flags=re.IGNORECASE)
            
        # Suffix d/t normalization (e.g. yozibti -> yozibdi, qilganti -> qilgandi)
        cleaned = re.sub(r'\b(\w+)(?:ibti|ipti)\b', r'\1ibdi', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\b(\w+)(?:ganti|qanti)\b', r'\1gandi', cleaned, flags=re.IGNORECASE)

        # 3. Clean duplicate sentences / phrases
        phrases = re.split(r'([.!?,\s]+)', cleaned)
        cleaned_parts = []
        last_phrase_comp = ""
        for p in phrases:
            p_strip = p.strip()
            p_comp = re.sub(r'[^\w]', '', p_strip).lower()
            if len(p_comp) > 2:
                if last_phrase_comp:
                    from difflib import SequenceMatcher
                    ratio = SequenceMatcher(None, p_comp, last_phrase_comp).ratio()
                    if ratio > 0.8 or p_comp in last_phrase_comp or last_phrase_comp in p_comp:
                        continue
                last_phrase_comp = p_comp
            cleaned_parts.append(p)
        cleaned = "".join(cleaned_parts)

    return cleaned.strip()


def _transcribe_wav_data(wav_data: bytes, lang: str = 'uz'):
    import tempfile
    import os
    import logging
    import numpy as np
    import torch
    from camera.stt_consumer import _get_whisper_model
    
    logger = logging.getLogger(__name__)
    model = _get_whisper_model()
    if model is None:
        logger.warning("[STT API] Whisper model not loaded.")
        return []
        
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as out_file:
            out_file.write(wav_data)
            out_path = out_file.name
            
        # Analyze audio with PyTorch Silero VAD first to filter out silence/noise early
        vad_model, vad_utils = _get_silero_vad()
        speech_timestamps = []
        has_speech = True
        wav_tensor = None
        if vad_model is not None and vad_utils is not None:
            try:
                get_speech_timestamps, _, read_audio, _, _ = vad_utils
                wav_tensor = read_audio(out_path)
                with _silero_vad_lock:
                    if hasattr(vad_model, 'reset_states'):
                        try:
                            vad_model.reset_states()
                        except Exception:
                            pass
                    speech_timestamps = get_speech_timestamps(
                        wav_tensor,
                        vad_model,
                        threshold=0.4, # More sensitive to catch quiet parts
                        sampling_rate=16000,
                        min_speech_duration_ms=250,
                        min_silence_duration_ms=300
                    )
                if not speech_timestamps:
                    has_speech = False
                    logger.info("[STT VAD] PyTorch VAD: No speech detected in chunk. Skipping Whisper.")
            except Exception as vad_err:
                logger.error("[STT VAD] PyTorch VAD check failed: %s", vad_err)
                
        if not has_speech or wav_tensor is None:
            try:
                os.unlink(out_path)
            except Exception:
                pass
            return []
            
        language_code = None if lang == 'auto' else lang
        
        # Lazy load local SpeechBrain ECAPA model
        sb_model = None
        try:
            sb_model = _get_speechbrain_model()
        except Exception as sb_err:
            logger.error("[STT Diarization] SpeechBrain loading failed: %s", sb_err)
            
        result_segments = []
        
        # Transcribe each speech timestamp segment separately to prevent silence hallucination
        for ts in speech_timestamps:
            start_sec = ts['start'] / 16000.0
            end_sec = ts['end'] / 16000.0
            
            # Extract samples for this speech interval
            speech_samples = wav_tensor[ts['start']:ts['end']].numpy()
            if len(speech_samples) < 3200: # Skip extremely short voice bursts (<0.2s)
                continue
                
            try:
                # Transcribe speech samples directly (faster-whisper accepts numpy arrays)
                segments, info = model.transcribe(
                    speech_samples,
                    language=language_code,
                    beam_size=5,
                    best_of=5,
                    temperature=0.0,
                    vad_filter=False, # Already split by PyTorch VAD
                    word_timestamps=False,
                    condition_on_previous_text=False,
                    initial_prompt=None,
                    repetition_penalty=1.2,
                    no_repeat_ngram_size=4,
                )
                
                ALLOWED_LANGUAGES = {'uz', 'ru', 'en', 'tr', 'az', 'kk', 'ky', 'tg', 'uk'}
                if info.language not in ALLOWED_LANGUAGES:
                    continue
                    
                for seg in segments:
                    t = seg.text.strip()
                    if t:
                        if lang == 'uz' or (lang == 'auto' and info.language == 'uz'):
                            t = cyrillic_to_latin(t)
                        t_clean = clean_hallucinations(t)
                        if t_clean and is_valid_language_text(t_clean):
                            resolved_lang = lang if lang != 'auto' else info.language
                            t_clean = clean_text_with_llama(t_clean, lang=resolved_lang)
                            
                        if t_clean and is_valid_language_text(t_clean):
                            # Calculate exact RMS for this segment
                            seg_start_idx = int(seg.start * 16000)
                            seg_end_idx = int(seg.end * 16000)
                            seg_samples = speech_samples[seg_start_idx:seg_end_idx]
                            
                            rms = np.sqrt(np.mean(seg_samples**2)) if len(seg_samples) > 0 else 0.0
                            if rms < 0.008:
                                continue
                                
                            # Extract SpeechBrain speaker embedding
                            embedding_list = []
                            if sb_model is not None and len(seg_samples) >= 3200:
                                try:
                                    samples_tensor = torch.tensor(seg_samples).unsqueeze(0)
                                    emb = sb_model.encode_batch(samples_tensor)
                                    emb_np = emb.squeeze().cpu().numpy()
                                    emb_norm = emb_np / (np.linalg.norm(emb_np) + 1e-10)
                                    embedding_list = emb_norm.tolist()
                                except Exception:
                                    pass
                                    
                            # Default fallback speaker
                            speaker = "O'qituvchi" if rms >= 0.025 else "Talaba"
                            
                            # Add to results with corrected absolute timestamps
                            result_segments.append({
                                'start': round(start_sec + seg.start, 3),
                                'end': round(start_sec + seg.end, 3),
                                'text': t_clean,
                                'speaker': speaker,
                                'embedding': embedding_list,
                                'rms': float(rms)
                            })
            except Exception as tr_err:
                logger.error("[STT API] Error transcribing speech segment [%.2fs - %.2fs]: %s", start_sec, end_sec, tr_err)
                
        return result_segments
    except Exception as e:
        logger.error(f"[STT API] Transcribe error: {e}", exc_info=True)
        return []
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_stt_transcribe(request):
    import os
    import time
    import json
    import subprocess
    import tempfile
    import logging
    from django.http import JsonResponse
    
    logger = logging.getLogger(__name__)
    session_id = request.GET.get('session_id')
    cmd = request.GET.get('cmd', 'append')
    lang = request.GET.get('lang', 'uz')
    if lang not in ('uz', 'ru', 'en', 'auto'):
        lang = 'uz'
    
    if not session_id:
        return JsonResponse({'success': False, 'error': 'session_id is required'}, status=400)
        
    # Clean session_id for safety
    session_id = "".join([c for c in session_id if c.isalnum() or c in ('_', '-')])
    if not session_id:
        return JsonResponse({'success': False, 'error': 'Invalid session_id'}, status=400)
        
    # Extract teacher name dynamically from schedule to ensure correct styling & labeling
    schedule_id = None
    teacher_name = "O'qituvchi"
    try:
        parts = session_id.split('_')
        if len(parts) >= 2 and parts[1].isdigit():
            schedule_id = int(parts[1])
            from camera.models import LessonSchedule
            sched = LessonSchedule.objects.filter(pk=schedule_id).first()
            if sched and sched.teacher_name:
                teacher_name = sched.teacher_name
    except Exception as t_err:
        logger.warning("[STT API] Failed to extract teacher name: %s", t_err)
        
    webm_path = f"/tmp/stt_{session_id}.webm"
    wav_path = f"/tmp/stt_{session_id}.wav"
    segments_path = f"/tmp/stt_{session_id}_segments.json"
    offset_path = f"/tmp/stt_{session_id}_start_offset.txt"
    header_path = f"/tmp/stt_{session_id}_header.bin"
    
    if cmd == 'clear':
        # Cleanup session files
        for path in [webm_path, wav_path, segments_path, offset_path, header_path]:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception:
                    pass
        return JsonResponse({'success': True, 'segments': []})
        
    # Append logic
    audio_bytes = request.body
    if not audio_bytes or len(audio_bytes) < 100:
        # Just return existing segments
        existing_segments = []
        if os.path.exists(segments_path):
            try:
                with open(segments_path, "r") as f:
                    existing_segments = json.load(f)
            except Exception:
                pass
        return JsonResponse({'success': True, 'segments': existing_segments})
        
    try:
        # Read the start offset from the file
        start_offset = 0.0
        if os.path.exists(offset_path):
            try:
                with open(offset_path, "r") as f:
                    start_offset = float(f.read().strip())
            except Exception:
                pass

        # 1. Append bytes to webm
        # If the webm file is missing but we have a cached header, restore it
        if not os.path.exists(webm_path) or os.path.getsize(webm_path) == 0:
            if os.path.exists(header_path) and os.path.getsize(header_path) > 0:
                logger.info("[STT API] WebM file missing/deleted, restoring from cached header...")
                try:
                    with open(webm_path, "wb") as f:
                        with open(header_path, "rb") as hf:
                            f.write(hf.read())
                except Exception as restore_err:
                    logger.error("[STT API] Failed to restore WebM header: %s", restore_err)
            else:
                # Save the first chunk of the session as header cache
                try:
                    with open(header_path, "wb") as hf:
                        hf.write(audio_bytes)
                except Exception as he:
                    logger.warning("[STT API] Failed to save header cache: %s", he)

        with open(webm_path, "ab") as f:
            f.write(audio_bytes)
            
        # 2. Convert entire webm to wav via ffmpeg
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", webm_path,
            "-ar", "16000",
            "-ac", "1",
            "-vn",
            "-f", "wav",
            wav_path
        ]
        result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=10)
        
        # Automatic recovery if ffmpeg fails (e.g. corruption due to lost middle chunks or invalid headers)
        if result.returncode != 0 and os.path.exists(header_path):
            logger.warning("[STT API] ffmpeg conversion failed (code %s). Attempting WebM recovery using cached header...", result.returncode)
            try:
                # Read the cached header
                with open(header_path, "rb") as hf:
                    header_bytes = hf.read()
                
                # Delete corrupted file
                if os.path.exists(webm_path):
                    os.unlink(webm_path)
                
                # Recreate WebM file using cached header + the current chunk only
                with open(webm_path, "wb") as f:
                    f.write(header_bytes)
                    f.write(audio_bytes)
                
                # Try converting again
                result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=10)
                if result.returncode == 0:
                    logger.info("[STT API] WebM file successfully recovered and converted after rebuilding with cached header!")
            except Exception as rec_err:
                logger.error("[STT API] Failed during WebM recovery: %s", rec_err)

        if result.returncode != 0:
            logger.warning("[STT API] ffmpeg conversion failed: %s", result.stderr.decode() if result.stderr else "unknown error")
            # Return current segments as fallback
            existing_segments = []
            if os.path.exists(segments_path):
                try:
                    with open(segments_path, "r") as f:
                        existing_segments = json.load(f)
                except Exception:
                    pass
            return JsonResponse({'success': True, 'segments': existing_segments})
            
        # 3. Read the converted wav and check duration
        with open(wav_path, "rb") as f:
            wav_data = f.read()
            
        header_len = 44
        pcm_data = wav_data[header_len:]
        total_duration = len(pcm_data) / 32000.0  # 16000 samples/sec * 2 bytes/sample = 32000 bytes/sec
        
        # 4. Load existing segments first to determine a stable sliding window start time
        existing_segments = []
        if os.path.exists(segments_path):
            try:
                with open(segments_path, "r") as f:
                    existing_segments = json.load(f)
            except Exception:
                pass
                
        # Target start time for the last 30 seconds
        target_start = max(0.0, total_duration - 30.0)
        
        # Find a stable boundary at a segment end in existing_segments
        window_start_abs = target_start
        if existing_segments:
            # We look for a segment that ends near target_start
            # Prefer segments ending between target_start - 15.0 and target_start + 5.0
            best_segment = None
            min_diff = 999.0
            for seg in existing_segments:
                end_time = seg['end']
                if (target_start - 15.0) <= end_time <= (target_start + 5.0):
                    diff = abs(end_time - target_start)
                    if diff < min_diff:
                        min_diff = diff
                        best_segment = seg
            
            if best_segment:
                window_start_abs = best_segment['end']
                
        # Safety bounds: keep window start at least 2 seconds before the end
        window_start_abs = max(0.0, min(window_start_abs, total_duration - 2.0))
        
        # Slice PCM data from window_start_abs to the end
        bytes_to_skip = int(window_start_abs * 32000)
        # Ensure 16-bit PCM word alignment (multiple of 2 bytes)
        bytes_to_skip = (bytes_to_skip // 2) * 2
        
        sliced_pcm = pcm_data[bytes_to_skip:]
        sliced_header = make_wav_header(len(sliced_pcm))
        sliced_wav = sliced_header + sliced_pcm
            
        # 5. Transcribe sliced audio
        new_segments = _transcribe_wav_data(sliced_wav, lang=lang)

        # Check camera active speech to filter out background noise/hallucinations
        # (Disabled/Bypassed because PyTorch Silero VAD handles speech filtering accurately,
        # and camera-based mouth movement detection is prone to false negatives discarding valid speech)
        pass
        
        # 6. Merge new segments into existing segments
        # If no new segments were transcribed, return existing segments directly to avoid deletion during silence
        if not new_segments:
            return JsonResponse({'success': True, 'segments': existing_segments})
                
        # Filter out old segments that start within the sliding window (exact boundary, shifted by start_offset)
        cutoff_time = start_offset + window_start_abs
        existing_segments = [s for s in existing_segments if s['start'] < cutoff_time]
        
        # Add new segments mapped to absolute timeline
        for seg in new_segments:
            start_abs = start_offset + window_start_abs + seg['start']
            end_abs = start_offset + window_start_abs + seg['end']
            
            if start_abs >= cutoff_time:
                text_clean = seg['text'].strip()
                if existing_segments:
                    from difflib import SequenceMatcher
                    is_duplicate = False
                    
                    # Robust duplicate checker: scan against the last 4 segments
                    for prev_seg in existing_segments[-4:]:
                        prev_text = prev_seg['text'].strip()
                        ratio = SequenceMatcher(None, text_clean.lower(), prev_text.lower()).ratio()
                        
                        # 1. Strict similarity check without simplistic substring match that cuts off expanded sentences
                        if ratio > 0.85:
                            is_duplicate = True
                            break
                            
                        # 2. Time-overlap check with moderate similarity (prevents duplicate transcription of same timeline slice)
                        overlap_start = max(start_abs, prev_seg['start'])
                        overlap_end = min(end_abs, prev_seg['end'])
                        if overlap_end > overlap_start:
                            overlap_dur = overlap_end - overlap_start
                            seg_dur = end_abs - start_abs
                            prev_dur = prev_seg['end'] - prev_seg['start']
                            
                            # If audio overlap is significant (>50%) and text matches moderately (>0.45)
                            if (overlap_dur / max(1e-5, seg_dur) > 0.50 or overlap_dur / max(1e-5, prev_dur) > 0.50) and ratio > 0.45:
                                is_duplicate = True
                                break
                                
                    if is_duplicate:
                        logger.info(f"[STT API] Skipped duplicate/repetitive/overlapping segment: {text_clean}")
                        continue
                
                existing_segments.append({
                    'start': round(start_abs, 3),
                    'end': round(end_abs, 3),
                    'text': text_clean,
                    'speaker': seg.get('speaker', "O'qituvchi"),
                    'embedding': seg.get('embedding', []),
                    'rms': seg.get('rms', 0.0)
                })
                
        # Check if the teacher has been seen recently on the active camera (using the lightweight state file)
        teacher_visible = False
        if schedule_id is not None:
            try:
                status_path = f"/tmp/teacher_active_{schedule_id}.json"
                if os.path.exists(status_path):
                    with open(status_path, "r") as tf:
                        data = json.load(tf)
                        if time.time() - data.get("last_seen", 0.0) < 60.0:
                            teacher_visible = True
            except Exception:
                pass
                
        # Perform dynamic speaker clustering over the entire session to refine diarization
        try:
            existing_segments = cluster_speakers(existing_segments, teacher_name=teacher_name, teacher_visible=teacher_visible)
        except Exception as cl_e:
            logger.error("[STT Diarization] Global speaker clustering failed: %s", cl_e)
                
        # Save updated segments
        try:
            with open(segments_path, "w") as f:
                json.dump(existing_segments, f)
        except Exception:
            pass
            
        # Periodic reset of WebM file if it exceeds 120 seconds to prevent ffmpeg CPU/IO bottlenecks
        if total_duration > 120.0:
            logger.info("[STT API] WebM file exceeds 120 seconds (%.2fs). Resetting session WebM to prevent CPU bottleneck...", total_duration)
            try:
                # Save new offset
                new_offset = start_offset + total_duration
                with open(offset_path, "w") as f:
                    f.write(str(new_offset))
                
                # Delete WebM and WAV files (the next append will start fresh from header)
                if os.path.exists(webm_path):
                    os.unlink(webm_path)
                if os.path.exists(wav_path):
                    os.unlink(wav_path)
            except Exception as reset_err:
                logger.error("[STT API] Failed during WebM session reset: %s", reset_err)
            
        # 7. Periodically clean up old /tmp/stt_ files (older than 6 hours)
        try:
            tmp_dir = "/tmp"
            now_time = time.time()
            for filename in os.listdir(tmp_dir):
                if filename.startswith("stt_") and (filename.endswith(".webm") or filename.endswith(".wav") or filename.endswith(".json") or filename.endswith(".bin")):
                    # Skip the current session's files to prevent deletion under any circumstances
                    if session_id and session_id in filename:
                        continue
                    filepath = os.path.join(tmp_dir, filename)
                    if now_time - os.path.getmtime(filepath) > 21600:  # 6 hours
                        os.unlink(filepath)
        except Exception:
            pass
            
        return JsonResponse({'success': True, 'segments': existing_segments})
        
    except Exception as e:
        logger.error("[STT API] Error during append/transcribe: %s", e, exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@login_required(login_url='login')
def api_tts_generate(request):
    from django.http import HttpResponse, JsonResponse
    from django.views.decorators.http import require_GET
    from .tts import generate_speech
    
    if request.method != 'GET':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)
        
    text = request.GET.get('text', '').strip()
    if not text:
        return JsonResponse({'success': False, 'error': 'Text is empty'}, status=400)
        
    if len(text) > 3000:
        return JsonResponse({'success': False, 'error': 'Text is too long (max 3000 chars)'}, status=400)
        
    try:
        audio_data = generate_speech(text)
        # Ovoz formatini aniqlaymiz (MP3 boshlanishi: ID3 yoki 0xFF 0xFB/0xF3/0xF2)
        if audio_data.startswith(b'ID3') or audio_data.startswith(b'\xff\xfb') or audio_data.startswith(b'\xff\xf3') or audio_data.startswith(b'\xff\xf2'):
            content_type = 'audio/mpeg'
            filename = 'speech.mp3'
        else:
            content_type = 'audio/wav'
            filename = 'speech.wav'
            
        response = HttpResponse(audio_data, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error("[TTS API] Error synthesizing speech: %s", e, exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_POST
def api_confirm_attendance(request, schedule_id):
    from attendance.models import Attendance
    from camera.live_attendance import normalize_id_set, save_live_attendance
    from camera.models import LessonSchedule, LessonSession
    from users.models import CustomUser
    from django.utils import timezone
    import re
    
    try:
        # Clear Anora chat history file for this session on dars start
        import os
        history_path = f"/tmp/anora_chat_{schedule_id}.json"
        if os.path.exists(history_path):
            try:
                os.remove(history_path)
            except Exception:
                pass
                
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            data = {}
        present_student_ids = normalize_id_set(data.get("present_student_ids"))
        present_student_names = data.get("present_students", [])
        if not isinstance(present_student_names, list):
            present_student_names = []

        def normalize_name(value):
            value = str(value or "").replace("‘", "'").replace("’", "'").replace("`", "'")
            return re.sub(r"\s+", " ", value).strip().casefold()

        present_name_set = {normalize_name(name) for name in present_student_names if name}
        present_meta = {}
        for item in data.get("present_students_meta", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                sid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            present_student_ids.add(sid)
            present_meta[str(sid)] = {
                "name": item.get("name") or "",
                "entry_time": item.get("entry_time") or "",
                "emotion": item.get("emotion") or "Neutral",
                "mood_score": item.get("mood_score"),
                "status": item.get("status") or "Darsda (Faol)",
            }
        
        schedule = LessonSchedule.objects.get(pk=schedule_id)
        today = timezone.localdate()
        
        # Get all students in the academic group
        students = list(CustomUser.objects.filter(
            academic_group=schedule.academic_group,
            role=CustomUser.Role.STUDENT,
            is_superuser=False
        ))
        
        group_student_ids = {std.id for std in students}
        for std in students:
            name = std.full_name or std.username
            if normalize_name(name) in present_name_set:
                present_student_ids.add(std.id)

        present_student_ids = present_student_ids & group_student_ids

        session, _ = LessonSession.objects.get_or_create(
            schedule=schedule,
            date=today,
            defaults={
                "planned_start": schedule.lesson_pair.start_time if schedule.lesson_pair else None,
                "planned_end": schedule.lesson_pair.end_time if schedule.lesson_pair else None,
            }
        )
        confirmed_by = request.user.id if getattr(request, "user", None) and request.user.is_authenticated else None
        save_live_attendance(
            session,
            present_student_ids=present_student_ids,
            present_student_names=[
                name for name in present_student_names
                if normalize_name(name) in present_name_set
            ],
            present_student_meta={
                sid: meta for sid, meta in present_meta.items()
                if int(sid) in present_student_ids
            },
            confirmed_by_id=confirmed_by,
        )

        for std in students:
            is_present_by_scan = std.id in present_student_ids
            
            if is_present_by_scan:
                att, created = Attendance.objects.get_or_create(
                    user=std,
                    date=today,
                    defaults={
                        "is_present": True,
                        "entry_time": timezone.now(),
                        "last_seen": timezone.now()
                    }
                )
                if not created:
                    att.is_present = True
                    if not att.entry_time:
                        att.entry_time = timezone.now()
                    att.last_seen = timezone.now()
                    att.save()
            
        return JsonResponse({
            "success": True,
            "status": "success",
            "present_count": len(present_student_ids),
            "total_students": len(students),
            "absent_count": len(students) - len(present_student_ids),
        })
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception("Attendance confirm error: %s", e)
        return JsonResponse({"success": False, "status": "error", "message": str(e)}, status=400)


def query_llm_for_anora(messages, timeout=15.0):
    """Query the LLM with full conversation history (list of message dicts).
    
    Tries OpenRouter Gemini 2.5 Flash API first, falls back to local uzbek-llama-uz.
    
    Args:
        messages: List of {"role": "system"|"user"|"assistant", "content": "..."} dicts.
                  Must include at least a system message and one user message.
        timeout: Request timeout in seconds.
    Returns:
        The assistant's response text, or empty string on failure.
    """
    import requests
    import json
    import logging
    logger = logging.getLogger(__name__)
    
    # === 1. TRY OPENROUTER API (PRIMARY) ===
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    # Model priority list - first available will be used
    openrouter_models = [
        "google/gemini-2.5-flash",
        "google/gemini-flash-1.5",
        "anthropic/claude-3-haiku",
        "openai/gpt-4o-mini",
    ]
    if openrouter_key:
        formatted_messages = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "").strip()
            if not content:
                continue
            formatted_messages.append({"role": role, "content": content})

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://smartgate.uz",
            "X-Title": "Smartgate CAMERA Anora"
        }

        for model_name in openrouter_models:
            try:
                payload = {
                    "model": model_name,
                    "messages": formatted_messages,
                    "temperature": 0.4,
                    "max_tokens": 800,
                }

                response = requests.post(url, json=payload, headers=headers, timeout=timeout)

                if response.status_code == 200:
                    res_data = response.json()
                    try:
                        text = res_data["choices"][0]["message"]["content"].strip()
                        if text:
                            logger.info(f"[Anora LLM] Successfully responded using {model_name}.")
                            return text
                    except (KeyError, IndexError):
                        logger.warning(f"[Anora LLM] Failed to parse OpenRouter response for {model_name}: {response.text[:200]}")
                else:
                    logger.warning(f"[Anora LLM] {model_name} returned {response.status_code}: {response.text[:200]}")
            except Exception as e:
                logger.warning(f"[Anora LLM] {model_name} call failed: {e}")



    # === 2. FALLBACK: Try Local Ollama Generate API with uzbek-llama-uz ===
    try:
        # Format messages list into a plain text prompt for uzbek-llama-uz
        system_content = ""
        history_lines = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "").strip()
            if not content:
                continue
            if role == "system":
                system_content = content
            elif role == "user":
                history_lines.append(f"\nSavol: {content}")
            elif role == "assistant":
                history_lines.append(f"\nAnora: {content}")
                
        # Combine system prompt with conversation QA history
        prompt_parts = []
        if system_content:
            prompt_parts.append(system_content)
        if history_lines:
            prompt_parts.extend(history_lines)
        prompt_parts.append("\nAnora:")
        
        prompt = "".join(prompt_parts)
        
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": "uzbek-llama-uz:latest",
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "num_predict": 150,
                    "top_p": 0.9,
                    "repeat_penalty": 1.2
                },
                "stop": ["\nSavol:", "\nAnora:", "\nFoydalanuvchi:"]
            },
            timeout=timeout
        )
        if response.status_code == 200:
            return response.json().get("response", "").strip()
    except Exception as e:
        logger.warning(f"[Anora LLM] Ollama port 11434 query failed: {e}")

    return ""


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_anora_query(request, schedule_id):
    """Processes user voice commands for Anora AI Assistant and returns a TTS voice response.
    
    Features:
    - Conversational memory: remembers previous Q&A within the same schedule session
    - Follow-up understanding: handles "ular qaysi?", "qilsa bo'ladimi?" type questions
    - Topic relevance: blocks off-topic questions
    - Attendance queries: real-time present/absent data
    """
    from django.http import JsonResponse
    from django.shortcuts import get_object_or_404
    from camera.live_attendance import get_live_attendance
    from camera.models import LessonSchedule, LessonSession
    from users.models import CustomUser
    from django.utils import timezone
    import json
    import os
    import logging
    from urllib.parse import quote
    
    logger = logging.getLogger(__name__)
    
    schedule = get_object_or_404(LessonSchedule, pk=schedule_id)
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    query = data.get("query", "").strip()
    query_lower = query.lower()
    topic = data.get("topic", "").strip() or schedule.topic or (schedule.subject.description if schedule.subject else "") or (schedule.subject.name if schedule.subject else "")
    
    if not query:
        return JsonResponse({"success": False, "message": "Suhbat matni bo'sh"}, status=400)

    # 1. Fetch students in this schedule's academic group and build live DB context
    students_list = list(CustomUser.objects.filter(
        academic_group=schedule.academic_group,
        role=CustomUser.Role.STUDENT,
        is_superuser=False
    ))
    
    today = timezone.localdate()
    live_session = LessonSession.objects.filter(schedule=schedule, date=today).first()
    live_present_ids = get_live_attendance(live_session)["present_student_ids"]
    
    student_status_lines = []
    for s in students_list:
        status_str = "bor (kelgan)" if s.id in live_present_ids else "yo'q (kelmagan)"
        student_status_lines.append(f"- {s.first_name} {s.last_name}: {status_str}")
    students_attendance_context = "\n".join(student_status_lines) if student_status_lines else "Guruhda talabalar topilmadi."
    
    present_count = len([s for s in students_list if s.id in live_present_ids])
    absent_count = len(students_list) - present_count
    
    topic_description = schedule.subject.description or "Ushbu mavzu doirasida amaliy va nazariy tushunchalar o'rganiladi."
    
    def _get_syllabus_text(subject):
        if not subject or not subject.syllabus:
            return ""
        try:
            import mammoth
            if os.path.exists(subject.syllabus.path):
                with open(subject.syllabus.path, 'rb') as f:
                    res = mammoth.extract_raw_text(f)
                    # Limit to 4500 characters
                    return res.value[:4500]
        except Exception as e:
            logger.warning(f"[Anora Syllabus] Error reading syllabus: {e}")
        return ""

    syllabus_text = _get_syllabus_text(schedule.subject)
    
    db_context = (
        f"Bugungi dars ma'lumotlari:\n"
        f"- Fan nomi: {schedule.subject.name}\n"
        f"- Dars mavzusi: {topic}\n"
        f"- Dars mavzusi bo'yicha qisqacha ma'lumot: {topic_description}\n"
        f"- Akademik guruh: {schedule.academic_group.name}\n"
        f"- O'qituvchi (domla/ustoz): {schedule.teacher_name}\n"
        f"- Haftaning kuni: {schedule.get_weekday_display()}\n"
        f"- Ishtirok etayotgan (kelgan) talabalar soni: {present_count} ta\n"
        f"- Kelmagan talabalar soni: {absent_count} ta\n\n"
        f"Guruhdagi talabalar ro'yxati va bugungi darsdagi ishtiroki (yo'qlama holati):\n"
        f"{students_attendance_context}\n\n"
        f"Tizim va davlat idoralari ma'lumotlari:\n"
        f"- O'zbekiston Respublikasi Maktabgacha va maktab ta'limi vaziri: E'zozxon G'opirjonovna Karimova (2025-yil 31-iyuldan boshlab). Hilola Umarova esa avvalgi vazir hisoblanadi.\n"
    )

    if syllabus_text:
        db_context += (
            f"\n--- FAN DASTURI (SYLLABUS) MA'LUMOTLARI ---\n"
            f"Ushbu darsning rasmiy o'quv dasturi (Sillabus) matni:\n"
            f"{syllabus_text}\n"
            f"Foydalanuvchi fan dasturi yoki mavzular rejasi haqida so'raganda, yuqoridagi rasmiy dastur ma'lumotlariga tayanib javob bering.\n"
            f"--- FAN DASTURI TUGADI ---\n\n"
        )

    # ===========================
    # CHAT HISTORY MANAGEMENT
    # ===========================
    MAX_HISTORY_TURNS = 3  # Keep last 3 user/assistant pairs (6 messages)

    def _history_path(sid):
        return f"/tmp/anora_chat_{sid}.json"
    
    def load_chat_history(sid):
        """Load conversation history for this schedule session."""
        path = _history_path(sid)
        try:
            if os.path.exists(path):
                file_mtime = os.path.getmtime(path)
                import time
                # If inactive for more than 45 minutes (2700 seconds), clear history
                if time.time() - file_mtime > 2700:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    return []
                    
                # Check if file is from today (reset daily for new lessons)
                from datetime import datetime
                file_date = datetime.fromtimestamp(file_mtime).date()
                today = datetime.now().date()
                if file_date != today:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    return []
                with open(path, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    if isinstance(history, list):
                        return history
        except Exception as e:
            logger.debug(f"[Anora] History load error: {e}")
        return []

    def save_chat_history(sid, history):
        """Save conversation history, keeping only last N turns."""
        path = _history_path(sid)
        try:
            # Keep only last MAX_HISTORY_TURNS * 2 messages (user + assistant pairs)
            trimmed = history[-(MAX_HISTORY_TURNS * 2):]
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(trimmed, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[Anora] History save error: {e}")

    # Load existing conversation history
    chat_history = load_chat_history(schedule_id)
    
    response_text = ""
    
    # ===========================
    # CONVERSATIONAL AI RESPONSE
    # ===========================
    
    # A. Predefined conversational helper
    def get_predefined_response(q_lower):
        # Xayrlashuv — oyna yopiladi
        farewells = [
            "yaxshi rahmat", "rahmat yaxshi", "yaxshi xayr", "xayr rahmat",
            "raxmat", "tashakkur", "sog' bo'ling", "sog' bo'l", "spasibo",
            "xayr", "ko'rishguncha", "yaxshi ko'rishguncha", "ok rahmat",
            "ok xayr", "ok sog'", "yetarli", "yetarli rahmat", "bajarildi",
            "tamom", "hammasi tushunarli", "tushundim rahmat", "rahmat xayr"
        ]
        if any(f in q_lower for f in farewells):
            return "__CLOSE__:Xayrli qoling! Savollaringiz bo'lsa, istalgan vaqt chaqiring."

        greetings = ["salom", "assalom", "qalaysiz", "hayrli kun", "hayrli tong", "salyut", "privet", "labbay"]
        if any(g in q_lower for g in greetings):
            return "Va alaykum assalom! Bugun sizga dars bo'yicha qanday yordam bera olaman?"
            
        gratitude = ["rahmat", "raxmat"]
        if any(g in q_lower for g in gratitude) and len(q_lower.split()) <= 2:
            return "__CLOSE__:Xayrli qoling! Savollaringiz bo'lsa, istalgan vaqt chaqiring."
            
        identity = ["isming nima", "ismingiz nima", "kimsan", "kimsa", "anora o'zing haqingda"]
        if any(id_word in q_lower for id_word in identity) or q_lower == "anora":
            return "Men Anoraman, sizning aqlli o'zbekona yordamchingizman. Bugungi dars mavzusini o'rganishda sizga yordam beraman."
            
        status = ["qandaysan", "ishlar qalay", "charchamayapsizmi"]
        if any(s in q_lower for s in status):
            return "Yaxshi, rahmat! Talabalarga dars mavzusini tushuntirishga tayyorman. Dars bo'yicha savolingiz bormi?"
            
        return None

    # B. Topic relevance checker helper
    def check_topic_relevance(q_lower, subject_name, topic_name):
        import re
        def get_words(text):
            text_clean = re.sub(r'[^\w\s]', ' ', text.lower())
            return [w for w in text_clean.split() if len(w) > 2]
            
        subject_words = get_words(subject_name)
        topic_words = get_words(topic_name)
        query_words = get_words(q_lower)
        
        # Allow names of students in this group (closures read students_list!)
        for s in students_list:
            s_first = s.first_name.lower()
            s_last = s.last_name.lower()
            for qw in query_words:
                if qw == s_first or qw == s_last:
                    return True
        
        # Direct overlap with subject or topic names
        for qw in query_words:
            for sw in subject_words:
                if qw.startswith(sw) or sw.startswith(qw):
                    return True
            for tw in topic_words:
                if qw.startswith(tw) or tw.startswith(qw):
                    return True
                    
        # Keywords related to technology or school environment
        tech_keywords = [
            "kod", "dastur", "variable", "o'zgaruvchi", "funksiya", "tsikl", "loop", 
            "klass", "class", "massiv", "list", "dict", "tuple", "hacker", "heker",
            "tarmoq", "shifr", "virus", "antivirus", "hujum", "attack", "security",
            "xavfsizlik", "himoya", "parol", "password", "baza", "database", "sql",
            "kutubxona", "library", "framework", "sintaks", "syntax", "xato", "error",
            "bug", "ishga tushir", "run", "terminal", "server", "ip", "port", "hackerlik",
            "kripto", "kriptografiya", "crypto", "firewall", "tarmoqlar"
        ]
        school_keywords = [
            "o'qituvchi", "oqituvchi", "domla", "ustoz", "muallim", "teacher",
            "talaba", "talabalar", "student", "guruh", "guruhda", "sinf",
            "yoqlama", "yo'qlama", "keldi", "kelmadi", "kelgan", "kelmagan",
            "bor", "yo'q", "ishtirok", "qatnash", "darsda", "kim", "kimlar"
        ]
        all_keywords = tech_keywords + school_keywords
        
        for qw in query_words:
            for tk in all_keywords:
                if qw.startswith(tk) or tk.startswith(qw):
                    return True
                    
        lesson_keywords = ["mavzu", "dars", "bugungi", "fan", "mashg'ulot", "topshiriq", "mashq", "darslik"]
        for qw in query_words:
            for lk in lesson_keywords:
                if qw == lk or (len(qw) > 4 and qw.startswith(lk)):
                    return True
                    
        return False

    # C. Sentence clean and filter helper
    def clean_llm_response(text):
        if not text:
            return ""
            
        import re
        
        # Markdown kod bloklarini o'chiramiz (``` ... ```)
        text = re.sub(r'```[\s\S]*?```', '', text)
        
        # Inline backtick `kod` ni oddiy matnga aylantiramiz (o'chirmay!)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        
        # **bold** va *italic* markdown belgilarini olib tashlaymiz
        text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
        
        lines = text.split('\n')
        cleaned_lines = []
        
        for line in lines:
            l_strip = line.strip()
            if not l_strip:
                continue
                
            # Faqat aniq dasturlash kodi bo'lgan qatorlarni o'tkazib yuboramiz
            # (def, return, print() bilan boshlanuvchi qatorlar)
            if (l_strip.startswith("def ") or l_strip.startswith("return ") or 
                l_strip.startswith("print(") or l_strip.startswith("import ") or
                l_strip.startswith("from ") or l_strip.startswith(">>>")):
                continue
                
            # Silliq o'qilishi uchun ro'yxat belgilarini (bullet points va raqamlarni) olib tashlaymiz
            l_strip = re.sub(r'^([\-\*\+]\s*|\d+[\.\)\-]\s*)', '', l_strip).strip()
            if not l_strip:
                continue
            
            cleaned_lines.append(l_strip)
            
        cleaned_text = " ".join(cleaned_lines).strip()
        
        # Tizimli ortiqcha prefikslarni tozalaymiz
        prefixes = [
            "mavzu:", "sizning javobingiz:", "javob:", "javobingiz:", 
            "tizim:", "anora:", "savol:", "talaba uchun javob:", "talabaga javob:"
        ]
        for prefix in prefixes:
            if cleaned_text.lower().startswith(prefix):
                cleaned_text = cleaned_text[len(prefix):].strip()
                
        return cleaned_text

    # D. Detect if this is a follow-up question (short, no clear subject)
    def is_followup_question(q_lower):
        """Detect follow-up questions that reference previous context."""
        followup_indicators = [
            "ular", "bular", "shular",
            "qilsa bo'ladimi", "bo'ladimi", "mumkinmi", "kerakmi",
            "yana gapirib ber", "batafsilroq", "ko'proq",
            "keyin nima", "undan keyin", "so'ng",
            "ha", "yo'q", "tushundim", "tushunarli"
        ]
        # Aniq mavzu so'zlar bo'lsa — followup emas
        clear_topic_words = [
            "haqida", "nima", "qanday", "tushuntir", "gapirib", "gapir",
            "nima uchun", "nega", "misol", "ta'rif", "tarif", "aytib",
            "o'rganamiz", "o'rgat", "bilaman", "bilmoqchi"
        ]
        words = q_lower.split()
        # Agar aniq mavzu so'z bo'lsa — followup emas
        if any(w in q_lower for w in clear_topic_words):
            return False
        # Juda qisqa (2 so'zdan kam) va tarix bo'lsa — followup
        if len(words) <= 2 and len(chat_history) > 0:
            return True
        for indicator in followup_indicators:
            if indicator in q_lower:
                return True
        return False

    # E. Execution logic
    predefined = get_predefined_response(query_lower)
    if predefined:
        response_text = predefined
        # Save predefined response to history too
        chat_history.append({"role": "user", "content": query})
        chat_history.append({"role": "assistant", "content": response_text})
        save_chat_history(schedule_id, chat_history)
    else:
        is_topic_request = any(k in query_lower for k in [
            "mavzu haqida", "bugungi mavzu", "dars mavzusi", "mavzuni tushuntir", 
            "bugun nima o'rganamiz", "darsimiz mavzusi", "mavzu nima", "dars gapir",
            "darsni mavzusi", "gapirib ber", "dars haqida ma'lumot", "dars haqida malumot"
        ])
        
        is_attendance_request = any(k in query_lower for k in [
            "keldi", "kelmadi", "kirdi", "kirmadi", "yo'qlama", "yoqlama", "ishtirok",
            "darsda bor", "darsda yo'q", "kelgan", "kelmagan", "qatnash", "yo'qlar", "borlar",
            "kimlar", "necha kishi", "kelmaganlar", "kelganlar", "absent", "present"
        ])
        
        # For follow-up questions or attendance queries, skip topic relevance check
        has_history = len(chat_history) > 0
        is_followup = is_followup_question(query_lower) and has_history
        
        if not is_topic_request and not is_attendance_request and not is_followup and not check_topic_relevance(query_lower, schedule.subject.name, topic):
            response_text = "Eslatib o'taman, bu savol bugungi darsimiz mavzusidan tashqaridadir. Iltimos, darsimizga doir savol bering."
            # Save off-topic response to history
            chat_history.append({"role": "user", "content": query})
            chat_history.append({"role": "assistant", "content": response_text})
            save_chat_history(schedule_id, chat_history)
        else:
            # === Use LLM with conversation history ===
            # Build the system prompt with rich real-time database context
            system_prompt = (
                f"Siz ta'lim tizimida o'qituvchi va talabalarga yordam beradigan Anora ismli aqlli, mehribon va o'zbekona AI yordamchisiz. "
                f"Siz hozir dars olib bormoqdasiz.\n\n"
                f"{db_context}\n\n"
                f"Siz talabalar va o'qituvchi bilan suhbatlashyapsiz. Oldingi savollar va javoblarni yaxshi eslab qoling.\n"
                f"MUHIM YO'RIQNOMA:\n"
                f"1. Agar foydalanuvchi darsga kim kelmagani (kelmaganlar/yo'qlar) yoki necha kishi kelmagani haqida so'rasa, "
                f"kelmagan talabalarning ismlarini chiroyli va chiroyli gap bilan to'liq sanab bering. Agar hamma kelgan bo'lsa, hech kim dars qoldirmaganini ayting.\n"
                f"2. Agar darsga kimlar kelgani (kelganlar/borlar) yoki necha kishi kelgani haqida so'rasa, darsda necha kishi qatnashayotganini va ismlarini chiroyli aytib bering.\n"
                f"3. Agar foydalanuvchi bugungi dars yoki dars haqida umumiy ma'lumot so'rasa (masalan: 'bugungi dars haqida ma'lumot ber', 'dars haqida gapir', 'bugungi dars nima'), "
                f"o'qituvchining ismi, haftaning qaysi kuni ekanligi, dars mavzusi va uning qisqacha tavsifi, hamda darsga necha talaba kelgani va necha talaba kelmaganini bitta to'liq, "
                f"ravon va pro darajadagi gapda chiroyli aytib bering.\n"
                f"4. Javoblaringiz aniq, tushunarli va ravon o'zbek tilida bo'lsin. Savol chuqur tushuntirishni talab qilsa, ko'proq ma'lumot bering — lekin ortiqcha uzaytirmang. Hech qanday dasturlash kodi, shablon yoki format ko'rsatmang.\n"
                f"5. Hech qanday dasturlash kodi, shablon yoki format ko'rsatmang.\n"
                f"6. MUHIM: Har doim 'Bugungi dars ma'lumotlari' (db_context) dagi eng so'nggi ma'lumotlarni haqiqiy deb qabul qiling. Agar oldingi suhbatlar tarixida (chat_history) talabalar ro'yxati, mavzu yoki har qanday boshqa ma'lumot boshqacha ko'rsatilgan bo'lsa, tarixdagi eski ma'lumotlarni e'tiborsiz qoldiring va Hozirgi db_context bo'yicha javob bering."
            )
            
            # Build messages list: system + history + current query
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add conversation history
            for msg in chat_history:
                messages.append(msg)
            
            user_prompt = query
            messages.append({"role": "user", "content": user_prompt})
            
            logger.info(f"[Anora] Sending {len(messages)} messages to LLM (history: {len(chat_history)} msgs)")

            raw_response = query_llm_for_anora(messages, timeout=20.0)
            logger.info(f"[Anora] Raw response from LLM: {raw_response[:200] if raw_response else 'None'}")
            response_text = clean_llm_response(raw_response) if raw_response else ""
            logger.info(f"[Anora] Cleaned response from LLM: {response_text[:200] if response_text else 'None'}")

            # Agar LLM xato qaytarsa, tarixsiz qayta urinib ko'ramiz
            if not response_text and chat_history:
                logger.warning("[Anora] LLM failed with history, retrying without history...")
                messages_no_history = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
                raw_response = query_llm_for_anora(messages_no_history, timeout=20.0)
                response_text = clean_llm_response(raw_response) if raw_response else ""
                logger.info(f"[Anora] Cleaned response without history: {response_text[:200] if response_text else 'None'}")

            # Validate response quality - reject if it contains garbage
            def is_response_valid(resp):
                if not resp or len(resp) < 5:
                    logger.warning(f"[Anora] Response validation failed: too short (len={len(resp) if resp else 0})")
                    return False
                garbage_indicators = [
                    "jismoniy teri", "ijtimoiy tarmoq", "ruhiyatga salbiy"
                ]
                for gi in garbage_indicators:
                    if gi in resp.lower():
                        logger.warning(f"[Anora] Response validation failed: contains garbage indicator '{gi}'")
                        return False
                return True

            response_is_valid = is_response_valid(response_text)
            logger.info(f"[Anora] Is response valid? {response_is_valid}")
            if not response_is_valid:
                response_text = "Kechirasiz, hozir texnik nosozlik yuz berdi. Iltimos, bir necha soniyadan so'ng qayta so'rang."

            # Save this Q&A pair to history
            chat_history.append({"role": "user", "content": user_prompt})
            chat_history.append({"role": "assistant", "content": response_text})
            # Tarixni 6 ta xabarga cheklaymiz (3 ta savol-javob)
            if len(chat_history) > 6:
                chat_history = chat_history[-6:]
            save_chat_history(schedule_id, chat_history)
            
    # Yopish belgisini tekshiramiz
    should_close = response_text.startswith("__CLOSE__:")
    if should_close:
        response_text = response_text[len("__CLOSE__:"):]

    return JsonResponse({
        "success": True,
        "response_text": response_text,
        "audio_url": f"/api/tts/generate/?text={quote(response_text)}",
        "close": should_close
    })


@login_required(login_url='login')
def hemis_schedule_view(request):
    from camera.models import LessonSchedule
    from users.models import AcademicGroup
    
    academic_groups = AcademicGroup.objects.all().order_by('name')
    
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'HEMIS darslar', 'url': None},
    ]
    
    stats = {
        "total": LessonSchedule.objects.count(),
        "groups_count": LessonSchedule.objects.values('academic_group').distinct().count(),
    }
    
    context = {
        'breadcrumbs': breadcrumbs,
        'academic_groups': academic_groups,
        'stats': stats,
    }
    return render(request, 'cameras/hemis_schedule.html', context)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_sync_hemis_schedule(request):
    from camera.tasks import hemis_sync_schedules_task
    from users.view import progress as prog
    import json
    
    try:
        body = json.loads(request.body or "{}")
    except Exception:
        body = {}
        
    prog.reset("schedules", 0, message="Vazifa navbatga qo'yildi...")
    
    celery_available = hasattr(hemis_sync_schedules_task, "delay")
    if celery_available:
        task = hemis_sync_schedules_task.delay(body)
        return JsonResponse({"success": True, "task_id": task.id})
    else:
        hemis_sync_schedules_task(None, body)
        return JsonResponse({"success": True, "task_id": None, "mode": "direct"})


@login_required(login_url='login')
def api_get_hemis_semesters(request):
    from attendance.models import SiteSettings
    import requests
    
    try:
        settings_obj = SiteSettings.get_settings()
        if not settings_obj or not settings_obj.hemis_url or not settings_obj.hemis_api_token:
            return JsonResponse({"success": False, "message": "HEMIS sozlamalari topilmadi."}, status=400)
            
        base_url = f"{settings_obj.hemis_url.rstrip('/')}/rest/v1/data/semester-list"
        headers = {
            "Authorization": f"Bearer {settings_obj.hemis_api_token}",
            "Accept": "application/json"
        }
        
        resp = requests.get(base_url, headers=headers, params={"limit": 200}, timeout=15)
        resp.raise_for_status()
        
        data = resp.json() or {}
        items = (data.get("data") or {}).get("items") or []
        
        parsed_sems = []
        for item in items:
            name = item.get("name") or ""
            edu_year = item.get("educationYear") or item.get("_education_year") or ""
            year_name = ""
            year_code = ""
            if isinstance(edu_year, dict):
                year_name = edu_year.get("name") or edu_year.get("code") or ""
                year_code = edu_year.get("code") or ""
            else:
                year_name = str(edu_year)
                year_code = str(edu_year)
                
            level_data = item.get("level") or {}
            level_name = level_data.get("name") if isinstance(level_data, dict) else ""
            
            display_name = f"{year_name}-o'quv yili - {name}"
            if level_name:
                display_name += f" ({level_name})"
                
            parsed_sems.append({
                "id": f"{item.get('code')}:{year_code}",
                "name": display_name,
                "current": item.get("current", False) or item.get("is_current", False),
                "raw_id": item.get("id")
            })
            
        parsed_sems.sort(key=lambda x: x["raw_id"], reverse=True)
        
        return JsonResponse({
            "success": True,
            "semesters": parsed_sems
        })
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Semestrlar yuklashda xatolik: {str(e)}"}, status=500)


@login_required(login_url='login')
def api_get_group_semesters(request):
    from attendance.models import SiteSettings
    from django.core.cache import cache
    import requests
    
    group_id = request.GET.get("group_id")
    if not group_id:
        return JsonResponse({"success": False, "message": "Guruh ID berilmadi"}, status=400)
        
    try:
        group_id = int(group_id)
    except ValueError:
        return JsonResponse({"success": False, "message": "Noto'g'ri guruh ID"}, status=400)
        
    curriculum_id = None
    
    # 1. Try to find in cache
    groups_all = cache.get("hemis_group_list_all_v1")
    if groups_all:
        for g in groups_all:
            if g.get("id") == group_id:
                curriculum_id = g.get("curriculum")
                break
                
    # 2. Try to fetch from group-list API
    if not curriculum_id:
        try:
            settings_obj = SiteSettings.get_settings()
            if settings_obj and settings_obj.hemis_url and settings_obj.hemis_api_token:
                base_url = f"{settings_obj.hemis_url.rstrip('/')}/rest/v1/data/group-list"
                headers = {
                    "Authorization": f"Bearer {settings_obj.hemis_api_token}",
                    "Accept": "application/json"
                }
                params = {"page": 1, "limit": 200}
                found = False
                for page in range(1, 5):
                    params["page"] = page
                    resp = requests.get(base_url, headers=headers, params=params, timeout=15)
                    if resp.status_code == 200:
                        items = resp.json().get("data", {}).get("items", [])
                        for item in items:
                            if item.get("id") == group_id:
                                curriculum_id = item.get("_curriculum")
                                found = True
                                break
                        if found or len(items) < 200:
                            break
                    else:
                        break
        except Exception:
            pass
            
    if not curriculum_id:
        return JsonResponse({"success": False, "message": "Guruhning o'quv rejasi aniqlanmadi"}, status=400)
        
    try:
        settings_obj = SiteSettings.get_settings()
        base_url_sem = f"{settings_obj.hemis_url.rstrip('/')}/rest/v1/data/semester-list"
        headers = {
            "Authorization": f"Bearer {settings_obj.hemis_api_token}",
            "Accept": "application/json"
        }
        resp = requests.get(base_url_sem, headers=headers, params={"_curriculum": str(curriculum_id), "limit": 200}, timeout=15)
        resp.raise_for_status()
        
        data = resp.json() or {}
        items = (data.get("data") or {}).get("items") or []
        
        import time
        current_time = time.time()
        
        unique_sems = {}
        for item in items:
            start_date = item.get("start_date")
            # Filter out future semesters that haven't started yet (allow semesters starting in next 60 days)
            if start_date and start_date > current_time + 60 * 86400:
                continue
                
            name = item.get("name") or ""
            edu_year = item.get("educationYear") or item.get("_education_year") or ""
            
            sem_code = item.get("code")
            sem_name = item.get("name")
            
            year_code = ""
            year_name = ""
            if isinstance(edu_year, dict):
                year_name = edu_year.get("name") or edu_year.get("code") or ""
                year_code = edu_year.get("code") or ""
            else:
                year_name = str(edu_year)
                year_code = str(edu_year)
                
            # If the year is formatted as single 4-digit code (e.g. 2025), format as academic year range (e.g. 2025-2026)
            if len(year_code) == 4 and year_code.isdigit():
                next_year = str(int(year_code) + 1)
                year_name = f"{year_code}-{next_year}"
                
            if sem_code and year_code:
                key = f"{sem_code}:{year_code}"
                
                is_current = False
                if isinstance(edu_year, dict):
                    is_current = edu_year.get("current", False) or edu_year.get("is_current", False)
                elif isinstance(item, dict):
                    is_current = item.get("current", False) or item.get("is_current", False)
                
                unique_sems[key] = {
                    "id": key,
                    "semester_code": sem_code,
                    "semester_name": sem_name,
                    "year_code": year_code,
                    "year_name": year_name,
                    "current": is_current,
                    "semester_id": item.get("id", 0)
                }
                
        sem_list = list(unique_sems.values())
        sem_list.sort(key=lambda x: x["semester_id"], reverse=True)
        
        return JsonResponse({
            "success": True,
            "semesters": sem_list
        })
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Semestrlarni yuklashda xatolik: {str(e)}"}, status=500)


@login_required(login_url='login')
def api_get_hemis_schedule(request):
    from attendance.models import SiteSettings
    from camera.models import LessonSchedule
    import requests
    
    group_id = request.GET.get("group_id")
    semester_id = request.GET.get("semester_id")
    if not group_id:
        return JsonResponse({"success": False, "message": "Guruh ID berilmadi"}, status=400)
        
    try:
        settings_obj = SiteSettings.get_settings()
        if not settings_obj or not settings_obj.hemis_url or not settings_obj.hemis_api_token:
            return JsonResponse({"success": False, "message": "HEMIS sozlamalari topilmadi. Tizim sozlamalarida kiritilganiga ishonch hosil qiling."}, status=400)
            
        base_url = f"{settings_obj.hemis_url.rstrip('/')}/rest/v1/data/schedule-list"
        headers = {
            "Authorization": f"Bearer {settings_obj.hemis_api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        
        params = {
            "page": 1,
            "limit": 200,
            "_group": str(group_id)
        }
        if semester_id:
            if ":" in semester_id:
                sem_code, edu_year = semester_id.split(":", 1)
                params["_semester"] = sem_code
                params["_education_year"] = edu_year
            else:
                params["_semester"] = str(semester_id)
            
        resp = requests.get(base_url, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        
        data = resp.json() or {}
        if not data.get("success"):
            return JsonResponse({"success": False, "message": "HEMIS success=false qaytardi"}, status=500)
            
        block = data.get("data") or {}
        items = block.get("items") or []
        
        parsed_items = []
        
        weekday_map = {
            1: "Dushanba",
            2: "Seshanba",
            3: "Chorshanba",
            4: "Payshanba",
            5: "Juma",
            6: "Shanba"
        }
        
        for item in items:
            # 1. Group
            group_data = item.get("academic_group") or item.get("group") or item.get("_academic_group")
            if not group_data or not isinstance(group_data, dict):
                continue
            group_name = (group_data.get("name") or "").strip()
            
            # 2. Subject
            subject_data = item.get("subject") or item.get("_subject")
            if not subject_data or not isinstance(subject_data, dict):
                continue
            subject_name = (subject_data.get("name") or "").strip()
            subject_code = (subject_data.get("code") or "").strip()
            
            # 3. Auditorium
            aud_data = item.get("auditorium") or item.get("_auditorium")
            aud_name = ""
            building_name = ""
            capacity = 30
            description = ""
            if isinstance(aud_data, dict):
                aud_name = (aud_data.get("name") or "").strip()
                capacity = aud_data.get("volume") or 30
                b_data = aud_data.get("building")
                if isinstance(b_data, dict):
                    building_name = (b_data.get("name") or "").strip()
                type_data = aud_data.get("auditoriumType")
                if isinstance(type_data, dict):
                    description = (type_data.get("name") or "").strip()
                
            # 4. Weekday
            weekday_data = item.get("week_day") or item.get("weekDay") or item.get("weekday") or item.get("_week_day")
            weekday_val = None
            if isinstance(weekday_data, dict):
                weekday_val = weekday_data.get("code")
            elif isinstance(weekday_data, (str, int)):
                weekday_val = weekday_data
                
            if weekday_val is None:
                lesson_date = item.get("lesson_date")
                if lesson_date:
                    try:
                        import datetime
                        dt = datetime.datetime.fromtimestamp(int(lesson_date))
                        weekday_val = dt.isoweekday()
                    except Exception:
                        pass
                        
            try:
                weekday_val = int(weekday_val)
            except (ValueError, TypeError):
                continue
                
            if weekday_val not in range(1, 7):
                continue
                
            # 5. Pair
            pair_data = item.get("pair") or item.get("lesson_pair") or item.get("_lesson_pair") or item.get("lessonPair")
            if not pair_data or not isinstance(pair_data, dict):
                continue
                
            pair_number_raw = pair_data.get("number") or pair_data.get("id") or pair_data.get("pair_number")
            try:
                pair_number = int(pair_number_raw)
            except (ValueError, TypeError):
                pair_name = (pair_data.get("name") or "").strip()
                import re
                match = re.search(r'\d+', pair_name)
                if match:
                    pair_number = int(match.group())
                else:
                    continue
                    
            start_time = pair_data.get("start_time") or pair_data.get("startTime") or "08:30"
            end_time = pair_data.get("end_time") or pair_data.get("endTime") or "09:50"
            
            # 6. Teacher
            teacher_data = item.get("employee") or item.get("teacher") or item.get("_employee")
            teacher_name = ""
            if isinstance(teacher_data, dict):
                teacher_name = (teacher_data.get("name") or "").strip()
            elif isinstance(teacher_data, str):
                teacher_name = teacher_data.strip()
                
            # Lookup teacher image in local database
            teacher_image_url = None
            if teacher_name:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                
                # Normalization function
                def normalize_name(s):
                    return s.replace("‘", "'").replace("’", "'").replace("`", "'").strip().upper()
                
                normalized_teacher = normalize_name(teacher_name)
                tokens = [t for t in normalized_teacher.split() if t]
                
                if tokens:
                    family_name = tokens[0]
                    initials = [t.replace('.', '') for t in tokens[1:] if t.replace('.', '')]
                    
                    # Fetch all candidates with same family name prefix/contains
                    candidates = User.objects.filter(role=User.Role.EMPLOYEE)
                    matched_user = None
                    
                    for cand in candidates:
                        cand_fullname = normalize_name(cand.full_name or "")
                        cand_shortname = normalize_name(cand.short_name or "")
                        
                        # Direct match check
                        if cand_fullname == normalized_teacher or cand_shortname == normalized_teacher:
                            matched_user = cand
                            break
                            
                        # Initials match check
                        cand_tokens = [t for t in cand_fullname.split() if t]
                        if cand_tokens and cand_tokens[0] == family_name:
                            matches_initials = True
                            for i, init in enumerate(initials):
                                if i + 1 < len(cand_tokens):
                                    if not cand_tokens[i + 1].startswith(init[0]):
                                        matches_initials = False
                                        break
                                else:
                                    matches_initials = False
                                    break
                            if matches_initials:
                                matched_user = cand
                                break
                    
                    if matched_user and matched_user.image:
                        teacher_image_url = matched_user.image.url
                
            # 8. Training type (Dars turi)
            training_type_data = item.get("trainingType") or item.get("training_type") or item.get("_training_type")
            training_type_name = ""
            if isinstance(training_type_data, dict):
                training_type_name = (training_type_data.get("name") or "").strip()
            elif isinstance(training_type_data, str):
                training_type_name = training_type_data.strip()
                
            lesson_type_mapped = "lecture"
            if training_type_name:
                norm_t = training_type_name.upper()
                if "SEMINAR" in norm_t or "AMALIY" in norm_t or "PRACTICAL" in norm_t:
                    lesson_type_mapped = "seminar"
                elif "LAB" in norm_t or "LABORATORIYA" in norm_t or "LABORATORY" in norm_t:
                    lesson_type_mapped = "lab"

            # 7. Check if exists locally
            exists_locally = LessonSchedule.objects.filter(
                academic_group__name=group_name,
                weekday=weekday_val,
                lesson_pair__pair_number=pair_number
            ).exists()
            
            parsed_items.append({
                "group_name": group_name,
                "subject_name": subject_name,
                "subject_code": subject_code,
                "auditorium_name": aud_name,
                "building_name": building_name,
                "capacity": capacity,
                "description": description,
                "weekday": weekday_val,
                "weekday_name": weekday_map.get(weekday_val, "Noma'lum"),
                "pair_number": pair_number,
                "start_time": start_time[:5],
                "end_time": end_time[:5],
                "teacher_name": teacher_name,
                "teacher_image": teacher_image_url,
                "exists_locally": exists_locally,
                "lesson_type": lesson_type_mapped
            })
            
        # De-duplicate by weekday, pair_number, subject_name, auditorium_name, teacher_name
        seen = set()
        unique_items = []
        for item in parsed_items:
            key = (item["weekday"], item["pair_number"], item["subject_name"], item["auditorium_name"], item["teacher_name"])
            if key not in seen:
                seen.add(key)
                unique_items.append(item)
                
        # sort by weekday, then by pair_number
        unique_items.sort(key=lambda x: (x["weekday"], x["pair_number"]))
        
        return JsonResponse({
            "success": True,
            "items": unique_items
        })
        
    except requests.exceptions.RequestException as e:
        return JsonResponse({"success": False, "message": f"HEMIS bilan bog'lanishda xato: {str(e)}"}, status=500)
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Kutilmagan xato: {str(e)}"}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_save_hemis_schedule(request):
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    from users.models import AcademicGroup
    from django.db import transaction
    from datetime import datetime
    import json
    
    try:
        data = json.loads(request.body or "{}")
        items = data.get("items") or []
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    if not items:
        return JsonResponse({"success": False, "message": "Saqlash uchun darslar ro'yxati topilmadi"}, status=400)
        
    def parse_time(time_str):
        if not time_str:
            return None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(time_str.strip(), fmt).time()
            except ValueError:
                continue
        return None

    created_count = 0
    updated_count = 0
    
    try:
        with transaction.atomic():
            for item in items:
                group_name = (item.get("group_name") or "").strip()
                subject_name = (item.get("subject_name") or "").strip()
                subject_name = subject_name.replace("‘", "'").replace("’", "'").replace("`", "'").replace("'", "'").strip()
                subject_code = (item.get("subject_code") or "").strip()
                auditorium_name = (item.get("auditorium_name") or "").strip()
                weekday = int(item.get("weekday"))
                pair_number = int(item.get("pair_number"))
                start_time_str = item.get("start_time")
                end_time_str = item.get("end_time")
                teacher_name = (item.get("teacher_name") or "").strip()
                building_name = (item.get("building_name") or "").strip()
                lesson_type = (item.get("lesson_type") or "lecture").strip()
                try:
                    capacity = int(item.get("capacity") or 30)
                except (ValueError, TypeError):
                    capacity = 30
                
                # Get or create group
                group_obj, _ = AcademicGroup.objects.get_or_create(name=group_name)
                
                # Get or create subject
                subject_obj, _ = Subject.objects.get_or_create(
                    name=subject_name,
                    defaults={"code": subject_code, "is_active": True}
                )
                
                # Get or create building
                from camera.models import Building
                building_obj = None
                if building_name:
                    building_obj, _ = Building.objects.get_or_create(name=building_name)
                
                # Get or create auditorium
                auditorium_obj, _ = Auditorium.objects.get_or_create(
                    name=auditorium_name,
                    defaults={
                        "building": building_obj,
                        "capacity": capacity,
                        "is_active": True
                    }
                )
                if auditorium_obj.building != building_obj or auditorium_obj.capacity != capacity:
                    if building_obj:
                        auditorium_obj.building = building_obj
                    auditorium_obj.capacity = capacity
                    auditorium_obj.save()
                
                # Get or create pair
                start_time = parse_time(start_time_str) or parse_time("08:30:00")
                end_time = parse_time(end_time_str) or parse_time("09:50:00")
                
                pair_obj, _ = LessonPair.objects.get_or_create(
                    shift=1,
                    pair_number=pair_number,
                    defaults={
                        "start_time": start_time,
                        "end_time": end_time
                    }
                )
                
                # Create/update schedule
                schedule_obj, created_s = LessonSchedule.objects.update_or_create(
                    academic_group=group_obj,
                    weekday=weekday,
                    lesson_pair=pair_obj,
                    defaults={
                        "subject": subject_obj,
                        "auditorium": auditorium_obj,
                        "teacher_name": teacher_name or None,
                        "lesson_type": lesson_type
                    }
                )
                if created_s:
                    created_count += 1
                else:
                    updated_count += 1
                    
        return JsonResponse({
            "success": True, 
            "message": f"Muvaffaqiyatli saqlandi! {created_count} ta yangi dars jadvali yaratildi, {updated_count} tasi yangilandi."
        })
    except Exception as e:
        return JsonResponse({"success": False, "message": f"Saqlashda xatolik yuz berdi: {str(e)}"}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_drag_update_lesson_schedule(request, pk):
    from camera.models import LessonSchedule, LessonPair
    import json
    try:
        schedule = LessonSchedule.objects.get(pk=pk)
    except LessonSchedule.DoesNotExist:
        return JsonResponse({"success": False, "message": "Dars jadvali topilmadi"}, status=404)
        
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xatolik"}, status=400)
        
    weekday = data.get("weekday")
    lesson_pair_id = data.get("lesson_pair_id")
    
    if weekday is None:
        return JsonResponse({"success": False, "message": "Hafta kuni majburiy"}, status=400)
        
    try:
        weekday = int(weekday)
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "message": "Hafta kuni noto'g'ri shaklda"}, status=400)
        
    # Check for conflicts
    if lesson_pair_id:
        try:
            lesson_pair_id = int(lesson_pair_id)
            lesson_pair = LessonPair.objects.get(id=lesson_pair_id)
            target_pair = lesson_pair
        except (TypeError, ValueError, LessonPair.DoesNotExist):
            return JsonResponse({"success": False, "message": "Dars vaqti (Para) topilmadi"}, status=400)
    else:
        target_pair = schedule.lesson_pair
        
    # 1. Group conflict
    if LessonSchedule.objects.filter(
        academic_group=schedule.academic_group,
        weekday=weekday,
        lesson_pair=target_pair
    ).exclude(pk=pk).exists():
        return JsonResponse({"success": False, "message": f"{schedule.academic_group.name} uchun ushbu vaqtda dars allaqachon belgilangan"}, status=400)
        
    # 2. Auditorium conflict
    if LessonSchedule.objects.filter(
        auditorium=schedule.auditorium,
        weekday=weekday,
        lesson_pair=target_pair
    ).exclude(pk=pk).exists():
        conflict = LessonSchedule.objects.filter(
            auditorium=schedule.auditorium,
            weekday=weekday,
            lesson_pair=target_pair
        ).exclude(pk=pk).first()
        return JsonResponse({"success": False, "message": f"{schedule.auditorium.name} xonasi band! ({conflict.academic_group.name} guruhi, '{conflict.subject.name}' darsi)"}, status=400)
        
    schedule.weekday = weekday
    if lesson_pair_id:
        schedule.lesson_pair = target_pair
    schedule.save()
    
    return JsonResponse({
        "success": True,
        "message": f"Dars jadvali {schedule.get_weekday_display()} kuniga muvaffaqiyatli ko'chirildi!"
    })











def dars_app_view(request):
    """
    React Dars App-ni xizmat ko'rsatish view-i.
    Build qilingan React static index.html faylini yuklaydi.
    """
    import os
    from django.conf import settings
    from django.http import HttpResponse
    from django.shortcuts import redirect
    from urllib.parse import urlencode

    if not request.user.is_authenticated:
        return redirect(f"/login/?{urlencode({'next': request.get_full_path()})}")

    if not request.GET.get("token"):
        from camera.dars_api import generate_dars_token
        token = generate_dars_token(request.user.id)
        query = request.GET.copy()
        query["token"] = token
        return redirect(f"{request.path}?{query.urlencode()}")

    index_path = os.path.join(settings.BASE_DIR, 'static', 'dars-app', 'index.html')
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            content = f.read()
        response = HttpResponse(content, content_type='text/html; charset=utf-8')
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response
    else:
        return HttpResponse(
            "<h4>React Dars App loyihasi hali build qilinmagan yoki topilmadi.</h4>"
            "<p>Iltimos, serverda frontend loyihani build qiling.</p>",
            status=503
        )


@login_required(login_url='login')
def lesson_report_view(request, schedule_id, report_date=None):
    """
    Dars yakunidagi to'liq hisobot sahifasi.
    LessonSession.analysis_json dan ma'lumot olinadi.
    Agar yo'q bo'lsa — real-time tahlil API chaqiriladi.
    """
    from camera.models import LessonSchedule, LessonSession
    from django.utils import timezone
    import math

    try:
        schedule = LessonSchedule.objects.select_related('subject', 'lesson_pair').get(pk=schedule_id)
    except LessonSchedule.DoesNotExist:
        from django.http import Http404
        raise Http404("Dars topilmadi")

    if report_date:
        import datetime
        try:
            target_date = datetime.date.fromisoformat(report_date)
        except ValueError:
            target_date = timezone.localdate()
    else:
        target_date = timezone.localdate()

    report_data = None

    # LessonSession dan saqlangan tahlilni olish
    try:
        session = LessonSession.objects.get(schedule=schedule, date=target_date)
        if session.analysis_json:
            report_data = session.analysis_json
    except LessonSession.DoesNotExist:
        pass

    # Agar tahlil saqlanmagan bo'lsa — stub data
    if not report_data:
        pair = schedule.lesson_pair
        lesson_type_labels = {'lecture': "Ma'ruza", 'seminar': 'Seminar', 'lab': 'Laboratoriya'}
        report_data = {
            "success": True,
            "lesson_info": {
                "subject": schedule.subject.name if schedule.subject else "—",
                "topic": schedule.topic or "Mavzu kiritilmagan",
                "teacher": schedule.teacher_name or "—",
                "lesson_type": lesson_type_labels.get(schedule.lesson_type, "Ma'ruza"),
                "date": str(target_date),
                "duration_minutes": None,
                "planned_start": pair.start_time.strftime('%H:%M') if pair and pair.start_time else None,
                "planned_end": pair.end_time.strftime('%H:%M') if pair and pair.end_time else None,
                "teacher_actual_start": None,
                "teacher_actual_end": None,
                "teacher_late_minutes": 0,
                "end_status": "Ma'lumot yo'q",
                "end_diff_minutes": 0,
                "present_count": 0,
                "absent_count": 0,
                "total_students": 0,
                "attendance_rate": 0,
            },
            "overall_quality": 0,
            "quality_label": "Ma'lumot yo'q",
            "quality_color": "warning",
            "relevance_score": 0,
            "teacher_activity_score": 0,
            "student_activity_score": 0,
            "time_efficiency_score": 0,
            "pedagogical_portrait": None,
            "ai_description": "Hisobot hali tayyorlanmagan. Dars yakunlanganidan so'ng ma'lumot ko'rinadi.",
        }

    # SVG uchun arc hisoblash (r=50)
    overall = report_data.get("overall_quality", 0)
    circumference = 2 * math.pi * 50  # r=50
    overall_dash = int(circumference * overall / 100)

    return render(request, 'lesson_report.html', {
        'report': report_data,
        'schedule': schedule,
        'overall_dash': overall_dash,
        'circumference': int(circumference),
    })
