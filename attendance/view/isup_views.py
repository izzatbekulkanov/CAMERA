import json
import logging
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, StreamingHttpResponse, HttpResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.db import models
from camera.models import Camera

logger = logging.getLogger(__name__)

ISUP_API_BASE = "http://127.0.0.1:8670"

def format_camera_isapi_error(err, ip: str = "", port: int = 80, username: str = "admin") -> str:
    """
    Kamera bilan bog'liq xatoliklarni (requests/urllib3/HTTP/ISAPI)
    foydalanuvchiga tushunarli va professional o'zbekcha matnga aylantiradi.
    """
    err_str = str(err or "").strip()
    err_lower = err_str.lower()
    ip_display = f" ({ip})" if ip else ""
    ip_port_display = f" ({ip}:{port})" if ip else ""

    # 1. No route to host / tarmoq yo'q / unreachable
    if "no route to host" in err_lower or "errno 113" in err_lower or "hostunreachable" in err_lower or "ehostunreach" in err_lower or "network is unreachable" in err_lower or "errno 101" in err_lower:
        return (
            f"Kameraga tarmoq orqali ulanib bo'lmadi{ip_display}. "
            f"Kamera o'chirilgan, tarmoq kabeli uzilgan yoki ushbu IP server tarmog'ida mavjud emas (No route to host)."
        )

    # 2. Connection refused / port yopiq
    if "connection refused" in err_lower or "errno 111" in err_lower or "econnrefused" in err_lower:
        return (
            f"Kamera ulanishni rad etdi{ip_port_display}. "
            f"HTTP porti ({port}) noto'g'ri ko'rsatilgan yoki kamerada ISAPI/veb xizmati yoqilmagan (Connection refused)."
        )

    # 3. Connection timeout / javob kutish vaqti tugadi
    if "timed out" in err_lower or "timeout" in err_lower or "timeouterror" in err_lower:
        return (
            f"Kameradan javob kutish vaqti tugadi{ip_port_display}. "
            f"Kamera haddan tashqari band yoki lokal tarmoq aloqasi juda sekin (Connection Timeout)."
        )

    # 4. Max retries exceeded (NewConnectionError ichidagi xatoliklar)
    if "max retries exceeded" in err_lower or "failed to establish a new connection" in err_lower:
        if "113" in err_str:
            return (
                f"Kameraga tarmoq orqali ulanib bo'lmadi{ip_display}. "
                f"Kamera o'chirilgan yoki server ushbu IP manzilini ko'ra olmayapti (No route to host)."
            )
        if "111" in err_str:
            return (
                f"Kameraning {port}-porti yopiq yoki ulanish rad etildi{ip_port_display}."
            )
        return (
            f"Kameraga ulanish urinishlari muvaffaqiyatsiz bo'ldi{ip_port_display}. "
            f"Kamera yoqilgani va IP manzili to'g'riligini tekshiring."
        )

    # 5. Auth / 401 Unauthorized / 403 Forbidden
    if "401" in err_str or "unauthorized" in err_lower or "403" in err_str or "forbidden" in err_lower or "bad auth" in err_lower or "invalid password" in err_lower:
        return (
            f"Kamera login yoki paroli noto'g'ri{ip_display}. "
            f"Foydalanuvchi: '{username}'. Iltimos, kamera parolini tekshirib qayta kiriting (HTTP 401 Unauthorized)."
        )

    # 6. 404 Not Found / ISAPI yo'li yo'q
    if "404" in err_str or "not found" in err_lower:
        return (
            f"Kamera ushbu ISAPI/ISUP (EHome) protokolini qo'llab-quvvatlamaydi{ip_display}. "
            f"Qurilma Hikvision bo'lmagan yoki dasturiy ta'minoti (firmware) juda eski."
        )

    # 7. 500 Internal Server Error
    if "500" in err_str or "internal server error" in err_lower:
        return (
            f"Kamera ichki xatolik qaytardi (HTTP 500){ip_display}. "
            f"Kamera sozlamalari bloklangan bo'lishi mumkin. Kamerani qayta ishga tushirish tavsiya etiladi."
        )

    # 8. Umumiy HTTP statuslar
    if err_str.startswith("HTTP "):
        return f"Kamera xatolik qaytardi{ip_display}: {err_str}"

    # 9. Boshqa barcha holatlar
    return f"Kameraga ulanishda xatolik yuz berdi{ip_display}: {err_str}"

def get_isup_health():
    try:
        r = requests.get(f"{ISUP_API_BASE}/health", timeout=2.0)
        if r.status_code == 200:
            data = r.json()
            data["is_active"] = True
            return data
    except Exception as e:
        logger.debug("ISUP health request failed: %s", e)
    return {
        "status": "offline",
        "mode": "hikvision_sdk",
        "public_host": "10.10.0.40",
        "register_port": 8660,
        "alarm_port": 8661,
        "picture_port": 8662,
        "devices": 0,
        "online_devices": 0,
        "is_active": False
    }

def get_isup_devices():
    try:
        r = requests.get(f"{ISUP_API_BASE}/devices", timeout=2.0)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("ISUP devices request failed: %s", e)
    return []

def enrich_db_cameras_list(db_cameras, raw_devices):
    """
    Barcha DB kameralarini 3 xil ISUP holati bilan boyitadi va tartiblaydi:
      1. online: 🟢 ISUP Online (hozir serverga ulangan va faol)
      2. offline: 🟡 Offline (avval ISUP ga ulangan, lekin hozir aloqa uzilgan)
      3. not_connected: ⚪ Ulanmagan (hali ISUP serverga ulanmagan)
    Tartib: Online kameralar har doim eng tepada turadi.
    """
    enriched_db_cams = []
    for cam in db_cameras:
        matched_dev = None
        for d in raw_devices:
            if not isinstance(d, dict):
                continue
            dev_id = str(d.get("device_id") or d.get("id") or "")
            dev_ip = str(d.get("remote_ip") or d.get("ip") or "")
            dev_sn = str(d.get("serial") or "")

            if dev_ip and dev_ip == cam.ip:
                matched_dev = d
                break
            if cam.serial_number and ((dev_id and dev_id in cam.serial_number) or (dev_sn and dev_sn in cam.serial_number)):
                matched_dev = d
                break

        if matched_dev:
            is_online = bool(matched_dev.get("online"))
            dev_id = matched_dev.get("device_id") or (cam.serial_number[-9:] if cam.serial_number and len(cam.serial_number) >= 9 else cam.ip)
            last_seen = matched_dev.get("last_seen_at") or matched_dev.get("last_seen")
            status_code = "online" if is_online else "offline"
            status_text = "Online" if is_online else "Offline"
        else:
            is_online = False
            dev_id = cam.serial_number[-9:] if cam.serial_number and len(cam.serial_number) >= 9 else cam.ip
            last_seen = None
            status_code = "not_connected"
            status_text = "Ulanmagan"

        # Juftlik (Checkpoint / Bino) ma'lumoti
        pair_info = None
        entry_p = getattr(cam, 'entry_pairs', None)
        active_entry_p = entry_p.filter(is_active=True).first() if entry_p else None
        exit_p = getattr(cam, 'exit_pairs', None)
        active_exit_p = exit_p.filter(is_active=True).first() if exit_p else None

        if active_entry_p:
            pair_info = {
                "id": active_entry_p.id,
                "name": active_entry_p.name,
                "role": "Kirish",
                "paired_camera_name": active_entry_p.exit_camera.name if active_entry_p.exit_camera else "--",
                "paired_camera_ip": active_entry_p.exit_camera.ip if active_entry_p.exit_camera else "--",
            }
        elif active_exit_p:
            pair_info = {
                "id": active_exit_p.id,
                "name": active_exit_p.name,
                "role": "Chiqish",
                "paired_camera_name": active_exit_p.entry_camera.name if active_exit_p.entry_camera else "--",
                "paired_camera_ip": active_exit_p.entry_camera.ip if active_exit_p.entry_camera else "--",
            }

        enriched_db_cams.append({
            "id": cam.id,
            "name": cam.name or cam.ip,
            "ip": cam.ip,
            "port": cam.port or 80,
            "mac_address": cam.mac_address or "--",
            "serial_number": cam.serial_number or "--",
            "device_model": cam.device_model or "Hikvision IPC",
            "is_entry_camera": cam.is_entry_camera,
            "is_exit_camera": cam.is_exit_camera,
            "enable_face_detection": cam.enable_face_detection,
            "is_lesson_camera": cam.is_lesson_camera,
            "isup_online": is_online,
            "isup_status": status_code,
            "isup_status_text": status_text,
            "isup_device_id": dev_id,
            "isup_last_seen": last_seen or ("Online" if is_online else ("Offline" if status_code == "offline" else "Ulanmagan")),
            "camera_obj": cam,
            "pair_info": pair_info,
        })

    # Online kameralar eng tepada, keyin offline, keyin ulanmagan
    def sort_key(c):
        order = {"online": 0, "offline": 1, "not_connected": 2}
        return (order.get(c["isup_status"], 3), c["name"] or "")

    enriched_db_cams.sort(key=sort_key)
    return enriched_db_cams


@login_required(login_url='login')
def isup_dashboard_view(request):
    """SmartGate ISUP Server va ulangan kameralar boshqaruv sahifasi."""
    from camera.models import CameraPair, Building
    from attendance.models import Attendance
    from django.utils import timezone

    health = get_isup_health()
    raw_devices = get_isup_devices()

    # DB-dagi barcha kameralar
    db_cameras = Camera.objects.all()
    enriched_db_cameras = enrich_db_cameras_list(db_cameras, raw_devices)

    cameras_by_ip = {c.ip: c for c in db_cameras}
    cameras_by_sn = {c.serial_number: c for c in db_cameras if c.serial_number}

    enriched_devices = []
    for dev in raw_devices:
        dev_id = dev.get("device_id") or dev.get("id")
        remote_ip = dev.get("remote_ip") or dev.get("ip")
        serial = dev.get("serial")

        matched_cam = cameras_by_ip.get(remote_ip) or (cameras_by_sn.get(serial) if serial else None)

        dev_info = {
            "device_id": dev_id,
            "ip": remote_ip,
            "port": dev.get("remote_port") or dev.get("port"),
            "serial": serial or (matched_cam.serial_number if matched_cam else "--"),
            "model": dev.get("device_model") or (matched_cam.device_model if matched_cam else "--"),
            "online": dev.get("online", False),
            "registered_at": dev.get("registered_at"),
            "last_seen": dev.get("last_seen_at") or dev.get("last_seen"),
            "camera_name": matched_cam.name if matched_cam else dev_id,
            "mac_address": matched_cam.mac_address if matched_cam else "--",
            "camera_obj": matched_cam,
        }
        enriched_devices.append(dev_info)

    # Kamera juftliklari (Nazorat punktlari / Bino kirish-chiqishlari)
    today = timezone.now().date()
    db_pairs = CameraPair.objects.select_related('building', 'entry_camera', 'exit_camera').filter(is_active=True)

    enriched_pairs = []
    for p in db_pairs:
        in_online = False
        in_dev_id = None
        if p.entry_camera:
            in_match = next((d for d in raw_devices if (d.get("remote_ip") == p.entry_camera.ip or (p.entry_camera.serial_number and d.get("device_id") in p.entry_camera.serial_number))), None)
            in_online = bool(in_match and in_match.get("online"))
            in_dev_id = in_match.get("device_id") if in_match else (p.entry_camera.serial_number[-9:] if p.entry_camera.serial_number else p.entry_camera.ip)

        out_online = False
        out_dev_id = None
        if p.exit_camera:
            out_match = next((d for d in raw_devices if (d.get("remote_ip") == p.exit_camera.ip or (p.exit_camera.serial_number and d.get("device_id") in p.exit_camera.serial_number))), None)
            out_online = bool(out_match and out_match.get("online"))
            out_dev_id = out_match.get("device_id") if out_match else (p.exit_camera.serial_number[-9:] if p.exit_camera.serial_number else p.exit_camera.ip)

        entry_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera).count() if p.entry_camera else 0
        exit_count = Attendance.objects.filter(date=today, exit_camera=p.exit_camera).count() if p.exit_camera else 0
        inside_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera, is_present=True).count() if p.entry_camera else 0

        enriched_pairs.append({
            "id": p.id,
            "name": p.name,
            "description": p.description or "",
            "building_id": p.building_id,
            "building_name": p.building.name if p.building else "",
            "entry_camera": p.entry_camera,
            "entry_camera_id": p.entry_camera_id,
            "entry_camera_name": p.entry_camera.name if p.entry_camera else "--",
            "entry_camera_ip": p.entry_camera.ip if p.entry_camera else "--",
            "entry_camera_model": p.entry_camera.device_model if p.entry_camera else "",
            "entry_camera_sn": p.entry_camera.serial_number if p.entry_camera else "",
            "entry_camera_mac": p.entry_camera.mac_address if p.entry_camera else "",
            "entry_online": in_online,
            "entry_dev_id": in_dev_id,
            "exit_camera": p.exit_camera,
            "exit_camera_id": p.exit_camera_id,
            "exit_camera_name": p.exit_camera.name if p.exit_camera else "--",
            "exit_camera_ip": p.exit_camera.ip if p.exit_camera else "--",
            "exit_camera_model": p.exit_camera.device_model if p.exit_camera else "",
            "exit_camera_sn": p.exit_camera.serial_number if p.exit_camera else "",
            "exit_camera_mac": p.exit_camera.mac_address if p.exit_camera else "",
            "exit_online": out_online,
            "exit_dev_id": out_dev_id,
            "entry_count_today": entry_count,
            "exit_count_today": exit_count,
            "inside_count_today": inside_count,
        })

    breadcrumbs = [
        {"name": "Asosiy sahifa", "url": "/"},
        {"name": "Sayt sozlamalari", "url": "/settings/site/"},
        {"name": "ISUP Sozlamalari", "url": None},
    ]

    total_db_cameras = len(enriched_db_cameras)
    online_isup_count = sum(1 for d in enriched_db_cameras if d["isup_online"])
    offline_isup_count = total_db_cameras - online_isup_count

    context = {
        "breadcrumbs": breadcrumbs,
        "page_title": "ISUP (EHome 5.0) Boshqaruv Markazi",
        "health": health,
        "devices": enriched_devices,
        "total_count": total_db_cameras,
        "online_count": online_isup_count,
        "offline_count": offline_isup_count,
        "db_cameras": enriched_db_cameras,
        "camera_pairs": enriched_pairs,
        "buildings": Building.objects.all(),
        "all_cameras": db_cameras,
    }
    return render(request, "settings/isup_dashboard.html", context)

@login_required(login_url='login')
def isup_api_status(request):
    """AJAX orqali real-vaqtda status olish."""
    try:
        health = get_isup_health()
        raw_devices = get_isup_devices()

        db_cameras = Camera.objects.all()
        enriched_db_cameras = enrich_db_cameras_list(db_cameras, raw_devices)

        cameras_by_ip = {c.ip: c for c in db_cameras}
        cameras_by_sn = {c.serial_number: c for c in db_cameras if c.serial_number}

        enriched = []
        for dev in raw_devices:
            if not isinstance(dev, dict):
                continue
            dev_id = dev.get("device_id") or dev.get("id") or "UNKNOWN"
            remote_ip = dev.get("remote_ip") or dev.get("ip") or "--"
            serial = dev.get("serial")
            matched_cam = cameras_by_ip.get(remote_ip) or (cameras_by_sn.get(serial) if serial else None)
            enriched.append({
                "device_id": str(dev_id),
                "ip": str(remote_ip),
                "port": dev.get("remote_port") or dev.get("port") or "",
                "serial": serial or (matched_cam.serial_number if matched_cam else "--"),
                "model": dev.get("device_model") or (matched_cam.device_model if matched_cam else "--"),
                "online": bool(dev.get("online", False)),
                "camera_name": matched_cam.name if matched_cam else dev_id,
                "mac_address": matched_cam.mac_address if matched_cam else "--",
                "last_seen": dev.get("last_seen_at") or dev.get("last_seen") or "Hozirgina",
            })

        # Sanitize for JSON response
        db_cams_json = []
        for c in enriched_db_cameras:
            db_cams_json.append({
                "id": c["id"],
                "name": c["name"],
                "ip": c["ip"],
                "port": c["port"],
                "mac_address": c["mac_address"],
                "serial_number": c["serial_number"],
                "device_model": c["device_model"],
                "is_entry_camera": c["is_entry_camera"],
                "is_exit_camera": c["is_exit_camera"],
                "enable_face_detection": c["enable_face_detection"],
                "is_lesson_camera": c["is_lesson_camera"],
                "isup_online": c["isup_online"],
                "isup_status": c["isup_status"],
                "isup_status_text": c["isup_status_text"],
                "isup_device_id": c["isup_device_id"],
                "isup_last_seen": c["isup_last_seen"],
            })

        total_db_cameras = len(enriched_db_cameras)
        online_isup_count = sum(1 for d in enriched_db_cameras if d["isup_online"])
        offline_isup_count = total_db_cameras - online_isup_count

        return JsonResponse({
            "success": True,
            "health": health,
            "devices": enriched,
            "db_cameras": db_cams_json,
            "total_count": total_db_cameras,
            "online_count": online_isup_count,
            "offline_count": offline_isup_count,
        })
    except Exception as exc:
        logger.exception("isup_api_status error: %s", exc)
        return JsonResponse({
            "success": False,
            "error": str(exc),
            "health": {"is_active": False, "status": "error"},
            "devices": [],
            "db_cameras": [],
            "total_count": 0,
            "online_count": 0,
            "offline_count": 0,
        })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_connect_camera(request):
    """Bazada mavjud kamerani tanlab, uning ISAPI sozlamalari orqali ISUP 8660 ga ulash."""
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "Noto'g'ri JSON format"}, status=400)

    camera_id = data.get("camera_id")
    camera = None

    # 1. Agar camera_id berilgan bo'lsa, bazadagi mavjud kameradan ma'lumotlarni olamiz
    if camera_id:
        try:
            camera = Camera.objects.get(id=int(camera_id))
            ip = camera.ip
            port = camera.port or 80
            username = camera.username or "admin"
            password = camera.password or ""
        except Camera.DoesNotExist:
            return JsonResponse({"success": False, "message": "Tanlangan kamera bazada topilmadi!"}, status=404)
    else:
        # Qo'lda kiritilgan bo'lsa
        ip = data.get("ip", "").strip()
        port = int(data.get("port", 80) or 80)
        username = data.get("username", "admin").strip() or "admin"
        password = data.get("password", "").strip()

    if not ip:
        return JsonResponse({"success": False, "message": "Kamera IP manzili talab qilinadi!"}, status=400)

    server_ip = data.get("server_ip", "10.10.0.40").strip()
    server_port = int(data.get("server_port", 8660))
    isup_key = data.get("isup_key", "facex2024").strip()

    # Avtomatik hisob ma'lumotlarini tekshirish va model/serialni olish
    from camera.views import fetch_camera_device_info
    dev_info = fetch_camera_device_info(ip, port, username, password)

    if not dev_info.get("reachable") and not dev_info.get("auth_success"):
        friendly_error = format_camera_isapi_error("No route to host", ip=ip, port=port, username=username)
        return JsonResponse({"success": False, "message": friendly_error}, status=400)

    if dev_info.get("reachable") and not dev_info.get("auth_success"):
        return JsonResponse({
            "success": False,
            "message": f"Kamera bilan aloqa mavjud ({ip}), ammo standart parollar (parol400, Qwerty@12, N@mdu309...) to'g'ri kelmadi. Iltimos, kamera parolini tekshirib kiriting."
        }, status=400)

    working_port = dev_info.get("working_port") or port or 80
    working_username = dev_info.get("working_username") or username or "admin"
    working_password = dev_info.get("working_password") or password or "parol400"
    sn = dev_info.get("serial_number") or (camera.serial_number if camera else "")
    device_id = sn[-9:] if (sn and len(sn) > 16) else (sn or "CAM_" + ip.replace(".", "_"))

    if camera:
        if dev_info.get("mac_address") and not camera.mac_address:
            camera.mac_address = dev_info["mac_address"]
        if dev_info.get("serial_number") and not camera.serial_number:
            camera.serial_number = dev_info["serial_number"]
        if dev_info.get("device_model") and not camera.device_model:
            camera.device_model = dev_info["device_model"]
        if working_password and camera.password != working_password:
            camera.password = working_password
        camera.save()

    xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ehome version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<enabled>true</enabled>
<addressingFormatType>ipaddress</addressingFormatType>
<ipAddress>{server_ip}</ipAddress>
<portNo>{server_port}</portNo>
<deviceID>{device_id}</deviceID>
<key>{isup_key}</key>
<ehomeKey>{isup_key}</ehomeKey>
<version>v5.0</version>
<protocolVersion>v5.0</protocolVersion>
</Ehome>"""

    success = False
    last_err = ""
    for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
        try:
            url = f"http://{ip}:{working_port}/ISAPI/System/Network/ehome"
            r = requests.put(
                url,
                data=xml_payload.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
                auth=auth_cls(working_username, working_password),
                timeout=4.0
            )
            if r.status_code == 200:
                success = True
                break
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:
            last_err = str(e)

    cam_name = camera.name if camera else ip
    if success:
        return JsonResponse({
            "success": True,
            "message": f"'{cam_name}' ({ip}) muvaffaqiyatli SmartGate ISUP (Port {server_port}) ga ulandi! Device ID: {device_id}"
        })
    else:
        friendly_error = format_camera_isapi_error(last_err, ip=ip, port=working_port, username=working_username)
        return JsonResponse({
            "success": False,
            "message": friendly_error
        })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_add_camera(request):
    """Yangi kamerani bazaga qo'shish va avtomatik parolni sinab ISUP 8660 ga ulash."""
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "Noto'g'ri JSON format"}, status=400)

    ip = str(data.get("ip", "")).strip()
    port = int(data.get("port", 80) or 80)
    name = str(data.get("name", "")).strip()
    username = str(data.get("username", "admin")).strip() or "admin"
    password = str(data.get("password", "")).strip()

    is_entry = bool(data.get("is_entry_camera", False))
    is_exit = bool(data.get("is_exit_camera", False))
    enable_face = bool(data.get("enable_face_detection", True))
    is_lesson = bool(data.get("is_lesson_camera", False))
    auto_isup = bool(data.get("auto_connect_isup", True))

    if not ip:
        return JsonResponse({"success": False, "message": "IP manzil kiritilishi shart!"}, status=400)

    # 1. Avtomatik hisob ma'lumotlari va parametrlarini aniqlash (Standart parollarni sinash)
    from camera.views import fetch_camera_device_info
    dev_info = fetch_camera_device_info(ip, port, username, password)

    if not dev_info.get("reachable") and not dev_info.get("auth_success"):
        friendly_err = format_camera_isapi_error("No route to host", ip=ip, port=port, username=username)
        return JsonResponse({"success": False, "message": friendly_err}, status=400)

    if dev_info.get("reachable") and not dev_info.get("auth_success"):
        return JsonResponse({
            "success": False,
            "message": f"Kamera bilan tarmoq aloqasi mavjud ({ip}), ammo standart parollar (parol400, Qwerty@12, Qwerty@123456., N@mdu309, namdu309, n@mdu309) to'g'ri kelmadi. Iltimos, maxsus parolni qo'lda kiriting."
        }, status=400)

    working_port = dev_info.get("working_port") or port or 80
    working_username = dev_info.get("working_username") or username or "admin"
    working_password = dev_info.get("working_password") or password or "parol400"
    mac = dev_info.get("mac_address")
    sn = dev_info.get("serial_number")
    model = dev_info.get("device_model") or "Hikvision IPC"
    discovered_name = dev_info.get("channel_name")

    final_name = name or discovered_name or f"Kamera {ip}"

    # 2. Bazada Camera yaratish yoki yangilash
    cam, created = Camera.objects.update_or_create(
        ip=ip,
        defaults={
            "port": working_port,
            "name": final_name,
            "username": working_username,
            "password": working_password,
            "is_active": True,
            "is_entry_camera": is_entry,
            "is_exit_camera": is_exit,
            "enable_face_detection": enable_face,
            "is_lesson_camera": is_lesson,
            "mac_address": mac or None,
            "serial_number": sn or None,
            "device_model": model or None,
        }
    )

    isup_connected = False
    isup_msg = ""
    # 3. Agar auto_isup bo'lsa, ISAPI orqali ISUP serverga ulash
    if auto_isup:
        server_ip = "10.10.0.40"
        server_port = 8660
        isup_key = "facex2024"
        device_id = sn[-9:] if (sn and len(sn) > 16) else (sn or "CAM_" + ip.replace(".", "_"))

        xml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ehome version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<enabled>true</enabled>
<addressingFormatType>ipaddress</addressingFormatType>
<ipAddress>{server_ip}</ipAddress>
<portNo>{server_port}</portNo>
<deviceID>{device_id}</deviceID>
<key>{isup_key}</key>
<ehomeKey>{isup_key}</ehomeKey>
<version>v5.0</version>
<protocolVersion>v5.0</protocolVersion>
</Ehome>"""
        ehome_url = f"http://{ip}:{working_port}/ISAPI/System/Network/ehome"
        add_last_err = ""
        for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
            try:
                r = requests.put(
                    ehome_url,
                    data=xml_payload.encode("utf-8"),
                    headers={"Content-Type": "application/xml"},
                    auth=auth_cls(working_username, working_password),
                    timeout=4.0
                )
                if r.status_code == 200:
                    isup_connected = True
                    isup_msg = f" va SmartGate ISUP 5.0 ({server_ip}:{server_port}) ga muvaffaqiyatli ulandi (Device ID: {device_id})"
                    break
                else:
                    add_last_err = f"HTTP {r.status_code}"
            except Exception as e:
                add_last_err = str(e)

        if not isup_connected and not isup_msg:
            if add_last_err:
                friendly_add_err = format_camera_isapi_error(add_last_err, ip=ip, port=working_port, username=working_username)
                isup_msg = f" (Diqqat: ISUP ulanmadi — {friendly_add_err})"
            else:
                isup_msg = " (ISUP sozlamalari yuborildi, kamera 10-30 soniyada ulanadi)"

    action_text = "qo'shildi" if created else "yangilandi"
    return JsonResponse({
        "success": True,
        "message": f"'{final_name}' ({ip}) kamerasining parametrlari bazaga {action_text} (Parol: {working_password}, Model: {model}){isup_msg}!",
        "camera_id": cam.id,
        "isup_connected": isup_connected
    })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_probe_camera(request):
    """
    Kamera IP manzili berilganda uni tarmoqda tekshirish,
    standart parollardan qaysi biri ishlashini aniqlash va model/SN ni qaytarish.
    """
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "Noto'g'ri JSON format"}, status=400)

    ip = str(data.get("ip", "")).strip()
    port = int(data.get("port", 80) or 80)
    username = str(data.get("username", "admin")).strip() or "admin"
    password = str(data.get("password", "")).strip()

    if not ip:
        return JsonResponse({"success": False, "message": "Kamera IP manzili kiritilishi shart!"}, status=400)

    from camera.views import fetch_camera_device_info
    dev_info = fetch_camera_device_info(ip, port, username, password)

    if not dev_info.get("reachable") and not dev_info.get("auth_success"):
        friendly_err = format_camera_isapi_error("No route to host", ip=ip, port=port, username=username)
        return JsonResponse({
            "success": False,
            "reachable": False,
            "message": friendly_err
        }, status=400)

    if dev_info.get("reachable") and not dev_info.get("auth_success"):
        return JsonResponse({
            "success": False,
            "reachable": True,
            "message": f"Kamera bilan aloqa mavjud ({ip}), ammo standart parollar (parol400, Qwerty@12, Qwerty@123456., N@mdu309, namdu309, n@mdu309) to'g'ri kelmadi. Iltimos, maxsus parolni qo'lda kiriting."
        }, status=400)

    return JsonResponse({
        "success": True,
        "reachable": True,
        "auth_success": True,
        "ip": ip,
        "working_port": dev_info.get("working_port", 80),
        "working_username": dev_info.get("working_username", "admin"),
        "working_password": dev_info.get("working_password", "parol400"),
        "mac_address": dev_info.get("mac_address", "--"),
        "serial_number": dev_info.get("serial_number", "--"),
        "device_model": dev_info.get("device_model", "Hikvision IPC"),
        "channel_name": dev_info.get("channel_name", ""),
        "message": f"Kamera muvaffaqiyatli aniqlandi! To'g'ri parol: '{dev_info.get('working_password')}' | Model: {dev_info.get('device_model')}"
    })

def classify_device_type(model: str = "", device_type: str = "", dev_desc: str = "") -> dict:
    """
    Kamera yoki qurilma modeliga qarab uning turi va vizual belgisini qaytaradi.
    """
    combined = f"{model or ''} {device_type or ''} {dev_desc or ''}".upper()

    if any(k in combined for k in ["DS-K1T", "DS-K2", "DS-K3", "FACE", "TERMINAL", "852144", "ACCESS"]):
        return {"name": "Face Terminal (Turniket)", "badge": "bg-soft-danger text-danger", "icon": "mdi-account-box-outline", "is_nvr": False}
    if any(k in combined for k in ["DS-96", "DS-76", "DS-77", "DS-86", "NVR", "DVR", "VIDEO RECORDER"]):
        return {"name": "NVR (Video Yozuvchi)", "badge": "bg-soft-dark text-dark", "icon": "mdi-server", "is_nvr": True}
    if any(k in combined for k in ["DS-2CD24", "DS-2CD14", "CUBE"]):
        return {"name": "Cube Kamera (Ichki/Darsxona)", "badge": "bg-soft-info text-info", "icon": "mdi-cube-outline", "is_nvr": False}
    if any(k in combined for k in ["DS-2CD21", "DS-2CD27", "DS-2CD11", "DS-2CD13", "DS-2CD23", "DOME"]):
        return {"name": "Dome Kamera (Gumbazli)", "badge": "bg-soft-primary text-primary", "icon": "mdi-camera-metering-center", "is_nvr": False}
    if any(k in combined for k in ["DS-2CD20", "DS-2CD26", "DS-2CD2T", "DS-2CD10", "BULLET"]):
        return {"name": "Bullet Kamera (Tashqi/Silindr)", "badge": "bg-soft-success text-success", "icon": "mdi-camera", "is_nvr": False}
    if any(k in combined for k in ["DS-2DE", "DS-2DF", "PTZ", "SPEED"]):
        return {"name": "PTZ Speed Dome (Aylanuvchi)", "badge": "bg-soft-warning text-warning", "icon": "mdi-axis-arrow", "is_nvr": False}

    return {"name": "IP Kamera", "badge": "bg-soft-secondary text-secondary", "icon": "mdi-cctv", "is_nvr": False}

@login_required(login_url='login')
def isup_api_discover_network_cameras(request):
    """
    Lokal tarmoqdagi barcha kameralarni tanlangan protokol (SADP, ONVIF, Dahua yoki Barchasi)
    orqali qidirish va ISUP 5.0 qo'llab-quvvatlash holatini aniqlash.
    """
    import socket
    import uuid
    import xml.etree.ElementTree as ET
    import re
    import json
    import ipaddress

    protocol = request.GET.get('protocol', 'all').lower().strip()
    devices = {}

    # 1. Hikvision SADP Discovery (UDP 37020)
    if protocol in ['all', 'sadp']:
        probe_msg = """<?xml version="1.0" encoding="utf-8"?>
<Probe>
    <Uuid>00000000-0000-0000-0000-000000000000</Uuid>
    <Types>inquiry</Types>
</Probe>""".encode('utf-8')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.5)
        try:
            sock.bind(('', 0))
            for dest in [('239.255.255.250', 37020), ('10.10.7.255', 37020), ('255.255.255.255', 37020)]:
                try: sock.sendto(probe_msg, dest)
                except: pass
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    text = data.decode('utf-8', errors='ignore')
                    root = ET.fromstring(text)
                    ip = root.findtext('.//IPv4Address') or addr[0]
                    mac = root.findtext('.//MAC')
                    mac_clean = mac.replace('-', ':').lower() if mac else '--'
                    model = root.findtext('.//DeviceDescription') or root.findtext('.//DeviceModel') or root.findtext('.//Description') or 'Hikvision IPC'
                    sn = root.findtext('.//DeviceSN') or root.findtext('.//DeviceSerialNo') or root.findtext('.//Serial') or '--'
                    port = int(root.findtext('.//HttpPort') or root.findtext('.//Port') or 80)
                    firmware = root.findtext('.//SoftwareVersion') or '--'

                    devices[ip] = {
                        'ip': ip,
                        'mac': mac_clean,
                        'model': model,
                        'sn': sn,
                        'port': port,
                        'firmware': firmware,
                        'vendor': 'Hikvision',
                        'protocol': 'Hikvision SADP',
                        'is_isup_supported': True
                    }
                except socket.timeout:
                    break
                except Exception:
                    pass
        finally:
            sock.close()

    # 2. ONVIF WS-Discovery (UDP 3702)
    if protocol in ['all', 'onvif']:
        onvif_probe = f"""<?xml version="1.0" encoding="utf-8"?>
<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <Header>
    <wsa:MessageID xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing">uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:To xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing">urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
  </Header>
  <Body>
    <Probe xmlns="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
      <Types>dn:NetworkVideoTransmitter</Types>
    </Probe>
  </Body>
</Envelope>""".encode('utf-8')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.5)
        try:
            sock.bind(('', 0))
            for dest in [('239.255.255.250', 3702), ('255.255.255.255', 3702), ('10.10.7.255', 3702)]:
                try: sock.sendto(onvif_probe, dest)
                except: pass
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    text = data.decode('utf-8', errors='ignore')
                    ip = addr[0]
                    if ip not in devices:
                        model_match = re.search(r'hardware/([^\s/]+)', text)
                        name_match = re.search(r'name/([^\s/]+)', text)
                        model = model_match.group(1) if model_match else (name_match.group(1) if name_match else 'ONVIF Camera')
                        is_hik = 'hik' in text.lower() or model.startswith('DS-') or model.startswith('iDS-') or model.startswith('IPC-')
                        devices[ip] = {
                            'ip': ip,
                            'mac': '--',
                            'model': model,
                            'sn': '--',
                            'port': 80,
                            'firmware': '--',
                            'vendor': 'Hikvision' if is_hik else 'ONVIF / Universal',
                            'protocol': 'ONVIF WS-Discovery',
                            'is_isup_supported': is_hik
                        }
                except socket.timeout:
                    break
                except Exception:
                    pass
        finally:
            sock.close()

    # 3. Dahua Discovery (UDP 37810)
    if protocol in ['all', 'dahua']:
        dh_probe = json.dumps({"method": "client.search", "params": {"mac": "", "ip": ""}}).encode('utf-8')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.2)
        try:
            sock.bind(('', 0))
            for dest in [('239.255.255.251', 37810), ('255.255.255.255', 37810), ('10.10.7.255', 37810)]:
                try:
                    sock.sendto(dh_probe, dest)
                    sock.sendto(b'DHIP\x00\x00\x00\x00', dest)
                except: pass
            while True:
                try:
                    data, addr = sock.recvfrom(4096)
                    text = data.decode('utf-8', errors='ignore')
                    ip = addr[0]
                    if ip not in devices:
                        try:
                            dj = json.loads(text)
                            params = dj.get('params', {})
                            devices[ip] = {
                                'ip': ip,
                                'mac': params.get('mac', '--'),
                                'model': params.get('deviceType', 'Dahua IPC'),
                                'sn': params.get('sn', '--'),
                                'port': params.get('httpPort', 80),
                                'firmware': params.get('version', '--'),
                                'vendor': 'Dahua',
                                'protocol': 'Dahua NetSDK',
                                'is_isup_supported': False
                            }
                        except Exception:
                            pass
                except socket.timeout:
                    break
        finally:
            sock.close()

    db_cameras = list(Camera.objects.all())
    db_ips = {c.ip: c for c in db_cameras}
    db_macs = {c.mac_address.lower(): c for c in db_cameras if c.mac_address}
    db_sns = {c.serial_number: c for c in db_cameras if c.serial_number}

    discovered_list = []
    for ip, dev in devices.items():
        matched_cam = db_ips.get(ip) or db_macs.get(dev['mac']) or db_sns.get(dev['sn'])
        if matched_cam:
            dev['is_in_db'] = True
            dev['db_camera_id'] = matched_cam.id
            dev['db_camera_name'] = matched_cam.name
        else:
            dev['is_in_db'] = False
            dev['db_camera_id'] = None
            dev['db_camera_name'] = None
        dev['device_category'] = classify_device_type(dev.get('model', ''), dev.get('device_type', ''), dev.get('description', ''))
        discovered_list.append(dev)

    # Sort: New cameras first, then by IP address
    def sort_key(item):
        try:
            ip_obj = ipaddress.ip_address(item['ip'])
            return (1 if item['is_in_db'] else 0, int(ip_obj))
        except Exception:
            return (1 if item['is_in_db'] else 0, 999999)

    discovered_list.sort(key=sort_key)

    return JsonResponse({
        "success": True,
        "protocol": protocol,
        "count": len(discovered_list),
        "new_count": sum(1 for d in discovered_list if not d['is_in_db']),
        "in_db_count": sum(1 for d in discovered_list if d['is_in_db']),
        "isup_count": sum(1 for d in discovered_list if d.get('is_isup_supported')),
        "devices": discovered_list
    })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_disconnect_device(request):
    """ISUP serverdan qurilmani majburiy uzish."""
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "Noto'g'ri JSON format"}, status=400)

    device_id = data.get("device_id", "").strip()
    if not device_id:
        return JsonResponse({"success": False, "message": "Device ID talab qilinadi"}, status=400)

    try:
        r = requests.delete(f"{ISUP_API_BASE}/devices/{device_id}", timeout=3.0)
        if r.status_code == 200:
            return JsonResponse({"success": True, "message": f"Qurilma '{device_id}' uzildi."})
        return JsonResponse({"success": False, "message": f"Xatolik: {r.text}"})
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)})

def _rtsp_check_password(ip: str, user: str, pwd: str, channel: str = "102", timeout: float = 0.8) -> bool:
    """RTSP DESCRIBE so'rovini yuborib parolni tekshirish (FFmpeg ishlatmasdan)."""
    import socket, base64, hashlib
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, 554))
        uri = f"rtsp://{ip}:554/Streaming/Channels/{channel}"
        cseq = 1
        # 1. DESCRIBE so'rovi (auth yo'q)
        req = f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: SmartGate/1.0\r\n\r\n"
        sock.sendall(req.encode())
        resp = sock.recv(4096).decode(errors='replace')
        if "200 OK" in resp:
            sock.close()
            return True
        if "401" not in resp:
            sock.close()
            return False
        # WWW-Authenticate header dan realm/nonce olish
        import re
        realm_m = re.search(r'realm="([^"]*)"', resp)
        nonce_m = re.search(r'nonce="([^"]*)"', resp)
        if realm_m and nonce_m:
            realm = realm_m.group(1)
            nonce = nonce_m.group(1)
            ha1 = hashlib.md5(f"{user}:{realm}:{pwd}".encode()).hexdigest()
            ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
            response_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            cseq += 1
            auth = (f'Digest username="{user}", realm="{realm}", '
                    f'nonce="{nonce}", uri="{uri}", response="{response_hash}"')
            req2 = (f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
                    f"Authorization: {auth}\r\nUser-Agent: SmartGate/1.0\r\n\r\n")
        else:
            # Basic auth
            creds = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            cseq += 1
            req2 = (f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\n"
                    f"Authorization: Basic {creds}\r\nUser-Agent: SmartGate/1.0\r\n\r\n")
        sock.sendall(req2.encode())
        resp2 = sock.recv(4096).decode(errors='replace')
        sock.close()
        return "200 OK" in resp2
    except Exception:
        return False


def resolve_best_camera_rtsp(ip: str, user: str = "admin", pwd: str = "", custom_channel: str = "") -> str:
    """
    Kamera yoki NVR uchun to'g'ri va faol RTSP oqim manzilini aniqlaydi.
    Barcha ma'lum parollarni RTSP orqali sinab ko'radi.
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    from camera.views import KNOWN_CAMERA_PASSWORDS

    channel = custom_channel or "102"

    # 1. Berilgan parolni avval sinab ko'ramiz
    candidates = []
    if pwd and pwd.strip():
        candidates.append(pwd.strip())

    # 2. Barcha ma'lum parollarni qo'shamiz
    for p in KNOWN_CAMERA_PASSWORDS:
        if p not in candidates:
            candidates.append(p)

    # 3. RTSP orqali to'g'ri parolni topamiz
    working_pwd = None
    for p in candidates:
        if _rtsp_check_password(ip, user, p, channel, timeout=0.7):
            working_pwd = p
            logger.info("[LIVE PREVIEW] RTSP parol topildi IP=%s pwd=***", ip)
            break
        elif channel != "101" and _rtsp_check_password(ip, user, p, "101", timeout=0.7):
            working_pwd = p
            logger.info("[LIVE PREVIEW] RTSP parol (kanal 101) topildi IP=%s pwd=***", ip)
            break

    if not working_pwd:
        working_pwd = candidates[0] if candidates else "parol400"
        logger.warning("[LIVE PREVIEW] RTSP uchun mos parol topilmadi IP=%s, birinchisini ishlatamiz", ip)

    pwd_encoded = quote(str(working_pwd), safe="!$&'()*+,;=~")
    user_encoded = quote(str(user or "admin"), safe="!$&'()*+,;=~")

    if custom_channel:
        return f"rtsp://{user_encoded}:{pwd_encoded}@{ip}:554/Streaming/Channels/{custom_channel}"

    # 4. ISAPI orqali to'g'ri kanal raqamini topamiz
    for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
        try:
            r = requests.get(
                f"http://{ip}:80/ISAPI/Streaming/channels",
                auth=auth_cls(user, working_pwd), timeout=1.0
            )
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                channels = []
                for ch in root.findall(".//{http://www.isapi.org/ver20/XMLSchema}StreamingChannel"):
                    cid = ch.findtext("{http://www.isapi.org/ver20/XMLSchema}id")
                    en = ch.findtext(".//{http://www.isapi.org/ver20/XMLSchema}enabled")
                    if en == "true" and cid:
                        channels.append(cid)
                if channels:
                    sub = [c for c in channels if c.endswith("2") or c.endswith("02")]
                    chosen = sub[0] if sub else channels[0]
                    return f"rtsp://{user_encoded}:{pwd_encoded}@{ip}:554/Streaming/Channels/{chosen}"
        except Exception:
            pass

    return f"rtsp://{user_encoded}:{pwd_encoded}@{ip}:554/Streaming/Channels/102"


@login_required(login_url='login')
def isup_api_camera_channels(request):
    """Kamera yoki NVR ning faol RTSP kanallari ro'yxatini qaytarish."""
    import xml.etree.ElementTree as ET
    from camera.views import KNOWN_CAMERA_PASSWORDS

    ip = request.GET.get('ip', '').strip()
    pwd = request.GET.get('password', '').strip()
    user = request.GET.get('username', 'admin').strip()

    candidates = [pwd.strip()] if pwd and pwd.strip() else []
    for p in KNOWN_CAMERA_PASSWORDS:
        if p not in candidates:
            candidates.append(p)

    channel_list = []
    is_nvr = False
    working_pwd = candidates[0] if candidates else 'parol400'

    for p in candidates:
        found = False
        for auth_cls in [HTTPDigestAuth, HTTPBasicAuth]:
            try:
                r = requests.get(f"http://{ip}:80/ISAPI/Streaming/channels", auth=auth_cls(user, p), timeout=0.8)
                if r.status_code == 200:
                    working_pwd = p
                    root = ET.fromstring(r.text)
                    for ch in root.findall(".//{http://www.isapi.org/ver20/XMLSchema}StreamingChannel"):
                        cid = ch.findtext("{http://www.isapi.org/ver20/XMLSchema}id")
                        cname = ch.findtext("{http://www.isapi.org/ver20/XMLSchema}channelName") or cid
                        en = ch.findtext(".//{http://www.isapi.org/ver20/XMLSchema}enabled")
                        res_w = ch.findtext(".//{http://www.isapi.org/ver20/XMLSchema}videoResolutionWidth") or ""
                        res_h = ch.findtext(".//{http://www.isapi.org/ver20/XMLSchema}videoResolutionHeight") or ""
                        res_str = f" ({res_w}x{res_h})" if res_w else ""
                        if en == "true" and cid:
                            is_sub = cid.endswith("2") or cid.endswith("02")
                            type_str = "Substream" if is_sub else "Asosiy"
                            channel_list.append({
                                "id": cid,
                                "name": f"Kanal {cid} - {type_str}{res_str}",
                                "is_sub": is_sub
                            })
                    if len(channel_list) > 2:
                        is_nvr = True
                    found = True
                    break
            except Exception:
                pass
        if found:
            break

    return JsonResponse({
        "success": True,
        "ip": ip,
        "is_nvr": is_nvr,
        "channels": channel_list,
        "working_password": working_pwd
    })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_delete_camera(request, camera_id):
    """Kamerani bazadan va agar ISUP da bo'lsa serverdan o'chirish."""
    cam = Camera.objects.filter(pk=camera_id).first()
    if not cam:
        return JsonResponse({"success": False, "message": "Kamera topilmadi"}, status=404)

    cam_name = cam.name or cam.ip
    cam_ip = cam.ip

    # Agar ISUP da ro'yxatda bo'lsa, ISUP dan ham uzamiz
    if cam.isup_device_id:
        try:
            requests.delete(f"{ISUP_API_BASE}/devices/{cam.isup_device_id}", timeout=2.0)
        except Exception:
            pass

    cam.delete()
    return JsonResponse({
        "success": True,
        "message": f"'{cam_name}' ({cam_ip}) kamerasi bazadan muvaffaqiyatli o'chirildi."
    })

async def isup_camera_live_preview_stream(request):
    """
    Istalgan kamera (bazada bor yoki tarmoqdagi yangi) uchun real-time jonli video oqimi (MJPEG).
    """
    import asyncio
    from asgiref.sync import sync_to_async

    user_obj = getattr(request, 'user', None)
    if hasattr(request, 'auser'):
        try:
            user_obj = await request.auser()
        except Exception:
            pass
    if not (user_obj and user_obj.is_authenticated):
        return HttpResponseForbidden("Autentifikatsiya talab etiladi.")

    ip = request.GET.get('ip', '').strip()
    cam_id = request.GET.get('camera_id')
    user = request.GET.get('username', 'admin').strip()
    pwd = request.GET.get('password', '').strip()
    channel = request.GET.get('channel', '').strip()

    if cam_id:
        cam = await sync_to_async(lambda: Camera.objects.filter(pk=cam_id).first())()
        if cam:
            ip = cam.ip
            user = cam.username or 'admin'
            pwd = cam.password or ''

    if not ip:
        return HttpResponseBadRequest("IP manzil talab qilinadi.")

    # Faol RTSP manzilini aniqlash (barcha ma'lum parollarni tezkor tekshiradi)
    target_rtsp = await sync_to_async(resolve_best_camera_rtsp)(ip, user, pwd, channel)
    logger.info("[LIVE PREVIEW] Streaming IP=%s with RTSP=%s", ip, target_rtsp)

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-reorder_queue_size", "0",
        "-max_delay", "0",
        "-flags", "low_delay",
        "-i", target_rtsp,
        "-an",
        "-vf", "scale=-2:480",
        "-c:v", "mjpeg",
        "-q:v", "5",
        "-r", "15",
        "-f", "mpjpeg",
        "-"
    ]

    async def mjpeg_generator():
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            while True:
                chunk = await process.stdout.read(16384)
                if not chunk:
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[LIVE PREVIEW] FFmpeg error for IP=%s: %s", ip, exc)
        finally:
            if process:
                try:
                    process.terminate()
                    await process.wait()
                except Exception:
                    pass

    resp = StreamingHttpResponse(
        mjpeg_generator(),
        content_type="multipart/x-mixed-replace; boundary=ffmpeg"
    )
    resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp["Pragma"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    resp["Connection"] = "keep-alive"
    return resp



@login_required(login_url='login')
async def isup_live_stream_view(request, device_id):
    """ISUP Real-Time Video Stream (Async MJPEG Stream Proxy for ASGI/Daphne)."""
    from django.http import StreamingHttpResponse, HttpResponse
    import aiohttp

    mode = request.GET.get('mode') or 'turbo'
    isup_stream_url = f"{ISUP_API_BASE}/devices/{device_id}/stream.mjpg?mode={mode}"

    async def async_stream_generator():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(isup_stream_url, timeout=aiohttp.ClientTimeout(total=None, connect=5)) as r:
                    async for chunk, _ in r.content.iter_chunks():
                        if chunk:
                            yield chunk
        except Exception as ex:
            logger.debug("Async stream generator closed: %s", ex)

    resp = StreamingHttpResponse(
        async_stream_generator(),
        content_type="multipart/x-mixed-replace; boundary=frame"
    )
    resp["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp["Pragma"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    resp["Connection"] = "keep-alive"
    return resp

@login_required(login_url='login')
def isup_snapshot_view(request, device_id):

    """ISUP Single Live Frame (JPEG)."""
    from django.http import HttpResponse
    mode = request.GET.get('mode') or 'turbo'
    isup_url = f"{ISUP_API_BASE}/devices/{device_id}/snapshot.jpg?mode={mode}"
    try:
        r = requests.get(isup_url, timeout=3.0)
        if r.status_code == 200 and r.content.startswith(b'\xff\xd8'):
            response = HttpResponse(r.content, content_type="image/jpeg")
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["X-Accel-Buffering"] = "no"
            return response
        return HttpResponse(f"Error fetching snapshot: {r.status_code}", status=r.status_code)
    except Exception as e:
        logger.exception("ISUP snapshot error: %s", e)
        return HttpResponse(f"ISUP snapshot error: {e}", status=502)


@login_required(login_url='login')
def isup_stream_start_view(request, device_id):
    """NVIDIA GPU stream ni ishga tushirish (POST)."""
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({"error": "POST required"}, status=405)
    mode = request.GET.get('mode') or request.POST.get('mode') or 'turbo'
    isup_url = f"{ISUP_API_BASE}/devices/{device_id}/stream/start?mode={mode}"
    try:
        r = requests.post(isup_url, timeout=4.0)
        return JsonResponse(r.json() if r.content else {"started": True, "mode": mode}, status=r.status_code)
    except Exception as e:
        logger.exception("ISUP stream start error: %s", e)
        return JsonResponse({"error": str(e)}, status=502)


@login_required(login_url='login')
def isup_stream_stop_view(request, device_id):
    """NVIDIA GPU stream ni to'xtatish (POST)."""
    from django.http import JsonResponse
    if request.method != 'POST':
        return JsonResponse({"error": "POST required"}, status=405)
    isup_url = f"{ISUP_API_BASE}/devices/{device_id}/stream/stop"
    try:
        r = requests.post(isup_url, timeout=3.0)
        return JsonResponse(r.json() if r.content else {"stopped": True}, status=r.status_code)
    except Exception as e:
        logger.exception("ISUP stream stop error: %s", e)
        return JsonResponse({"error": str(e)}, status=502)


@login_required(login_url='login')
def isup_stream_status_view(request, device_id):
    """NVIDIA GPU stream jonli statistikasi (FPS, GPU latency, hajmi)."""
    from django.http import JsonResponse
    isup_url = f"{ISUP_API_BASE}/devices/{device_id}/stream/status"
    try:
        r = requests.get(isup_url, timeout=2.0)
        return JsonResponse(r.json() if r.content else {"active": False}, status=r.status_code)
    except Exception as e:
        return JsonResponse({"active": False, "error": str(e)})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_reboot_camera(request):
    """Kamerani masofadan ISAPI orqali qayta yuklash (Reboot)."""
    try:
        data = json.loads(request.body or "{}")
        camera_id = data.get("camera_id")
        camera = Camera.objects.get(id=int(camera_id))
        ip = camera.ip
        port = camera.port or 80
        username = camera.username or "admin"
        password = camera.password or "parol400"

        reboot_url = f"http://{ip}:{port}/ISAPI/System/reboot"
        r = requests.put(reboot_url, auth=HTTPDigestAuth(username, password), timeout=5.0)
        if r.status_code not in [200, 201, 204]:
            r = requests.put(reboot_url, auth=HTTPBasicAuth(username, password), timeout=5.0)

        if r.status_code in [200, 201, 204]:
            return JsonResponse({"success": True, "message": f"{camera.name or camera.ip} muvaffaqiyatli qayta ishga tushirilmoqda..."})
        else:
            friendly_err = format_camera_isapi_error(f"HTTP {r.status_code}", ip=ip, port=port, username=username)
            return JsonResponse({"success": False, "message": friendly_err}, status=400)
    except Exception as e:
        friendly_err = format_camera_isapi_error(e, ip=ip if 'ip' in locals() else '', port=port if 'port' in locals() else 80, username=username if 'username' in locals() else 'admin')
        return JsonResponse({"success": False, "message": f"Qayta yuklash xatosi: {friendly_err}"}, status=500)


@login_required(login_url='login')
def isup_api_get_isapi_config(request, camera_id):
    """Kameraning to'liq ISAPI konfiguratsiyasini o'qish (Time, Stream, DeviceInfo, EHome)."""
    import xml.etree.ElementTree as ET
    camera = get_object_or_404(Camera, id=camera_id)
    auth = HTTPDigestAuth(camera.username or 'admin', camera.password or 'parol400')
    ip = camera.ip
    port = camera.port or 80
    base = f"http://{ip}:{port}"

    result = {
        "success": True,
        "camera_id": camera.id,
        "ip": camera.ip,
        "name": camera.name,
        "device_info": {},
        "time": {},
        "stream_101": {},
        "stream_102": {},
        "ehome": {}
    }

    # 1. Device Info
    try:
        r = requests.get(f"{base}/ISAPI/System/deviceInfo", auth=auth, timeout=3.0)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            result["device_info"] = {
                "name": (root.find('.//h:deviceName', ns).text if root.find('.//h:deviceName', ns) is not None else ""),
                "model": (root.find('.//h:model', ns).text if root.find('.//h:model', ns) is not None else ""),
                "serial_number": (root.find('.//h:serialNumber', ns).text if root.find('.//h:serialNumber', ns) is not None else ""),
                "sub_serial": (root.find('.//h:subSerialNumber', ns).text if root.find('.//h:subSerialNumber', ns) is not None else ""),
                "mac": (root.find('.//h:macAddress', ns).text if root.find('.//h:macAddress', ns) is not None else ""),
                "firmware": (root.find('.//h:firmwareVersion', ns).text if root.find('.//h:firmwareVersion', ns) is not None else ""),
                "firmware_release_date": (root.find('.//h:firmwareReleasedDate', ns).text if root.find('.//h:firmwareReleasedDate', ns) is not None else ""),
                "encoder_version": (root.find('.//h:encoderVersion', ns).text if root.find('.//h:encoderVersion', ns) is not None else ""),
                "encoder_release_date": (root.find('.//h:encoderReleasedDate', ns).text if root.find('.//h:encoderReleasedDate', ns) is not None else ""),
                "boot_version": (root.find('.//h:bootVersion', ns).text if root.find('.//h:bootVersion', ns) is not None else ""),
                "hardware_version": (root.find('.//h:hardwareVersion', ns).text if root.find('.//h:hardwareVersion', ns) is not None else ""),
                "device_type": (root.find('.//h:deviceType', ns).text if root.find('.//h:deviceType', ns) is not None else ""),
                "device_id": (root.find('.//h:deviceID', ns).text if root.find('.//h:deviceID', ns) is not None else ""),
                "device_description": (root.find('.//h:deviceDescription', ns).text if root.find('.//h:deviceDescription', ns) is not None else ""),
                "device_location": (root.find('.//h:deviceLocation', ns).text if root.find('.//h:deviceLocation', ns) is not None else ""),
                "system_contact": (root.find('.//h:systemContact', ns).text if root.find('.//h:systemContact', ns) is not None else ""),
                "manufacturer": (root.find('.//h:manufacturer', ns).text if root.find('.//h:manufacturer', ns) is not None else "Hikvision"),
            }
    except Exception as e:
        logger.debug("ISAPI deviceInfo error: %s", e)

    # 2. Time
    try:
        r = requests.get(f"{base}/ISAPI/System/time", auth=auth, timeout=3.0)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            result["time"] = {
                "mode": (root.find('.//h:timeMode', ns).text if root.find('.//h:timeMode', ns) is not None else ""),
                "local_time": (root.find('.//h:localTime', ns).text if root.find('.//h:localTime', ns) is not None else ""),
                "time_zone": (root.find('.//h:timeZone', ns).text if root.find('.//h:timeZone', ns) is not None else "")
            }
    except Exception as e:
        logger.debug("ISAPI time error: %s", e)

    # 3. Main Stream (101)
    try:
        r = requests.get(f"{base}/ISAPI/Streaming/channels/101", auth=auth, timeout=3.0)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            w = root.find('.//h:videoResolutionWidth', ns)
            h = root.find('.//h:videoResolutionHeight', ns)
            fps = root.find('.//h:maxFrameRate', ns)
            br = root.find('.//h:vbrUpperCap', ns) or root.find('.//h:constantBitRate', ns)
            codec = root.find('.//h:videoCodecType', ns)
            result["stream_101"] = {
                "channel_name": (root.find('.//h:channelName', ns).text if root.find('.//h:channelName', ns) is not None else ""),
                "resolution": f"{w.text}x{h.text}" if w is not None and h is not None else "1920x1080",
                "width": w.text if w is not None else "1920",
                "height": h.text if h is not None else "1080",
                "fps": str(int(fps.text) // 100) if fps is not None and fps.text else "25",
                "bitrate": br.text if br is not None else "4096",
                "codec": codec.text if codec is not None else "H.264"
            }
    except Exception as e:
        logger.debug("ISAPI stream 101 error: %s", e)

    # 4. EHome (ISUP)
    try:
        r = requests.get(f"{base}/ISAPI/System/Network/ehome", auth=auth, timeout=3.0)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            en = root.find('.//h:enabled', ns)
            ip_node = root.find('.//h:ipAddress', ns)
            port_node = root.find('.//h:portNo', ns)
            dev_id_node = root.find('.//h:deviceID', ns) or root.find('.//h:customDeviceID', ns)
            result["ehome"] = {
                "enabled": en.text.lower() == 'true' if en is not None and en.text else False,
                "ip": ip_node.text if ip_node is not None else "10.10.0.40",
                "port": port_node.text if port_node is not None else "8660",
                "device_id": dev_id_node.text if dev_id_node is not None else ""
            }
    except Exception as e:
        logger.debug("ISAPI ehome error: %s", e)

    # 5. HTTP Host Push Destination (Domen / Webhook)
    try:
        r = requests.get(f"{base}/ISAPI/Event/notification/httpHosts/1", auth=auth, timeout=3.0)
        if r.status_code == 200:
            root = ET.fromstring(r.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            url_node = root.find('.//h:url', ns)
            proto_node = root.find('.//h:protocolType', ns)
            fmt_node = root.find('.//h:parameterFormatType', ns)
            addr_node = root.find('.//h:addressingFormatType', ns)
            host_node = root.find('.//h:hostName', ns)
            ip_node = root.find('.//h:ipAddress', ns)
            port_node = root.find('.//h:portNo', ns)

            raw_host = host_node.text if host_node is not None and host_node.text else (ip_node.text if ip_node is not None else "")
            host_val = raw_host if raw_host and raw_host != "0.0.0.0" else "ad.namspi.uz"
            url_val = url_node.text if url_node is not None and url_node.text and url_node.text != "/" else "/attendance/api/camera/event/"
            port_val = port_node.text if port_node is not None and port_node.text and port_node.text != "0" else ("443" if host_val == "ad.namspi.uz" else "80")

            result["http_host"] = {
                "url": url_val,
                "protocol": proto_node.text if proto_node is not None and proto_node.text else "HTTPS",
                "format": fmt_node.text if fmt_node is not None and fmt_node.text else "JSON",
                "addressing_type": addr_node.text if addr_node is not None else "hostname",
                "host": host_val,
                "port": port_val
            }
        else:
            result["http_host"] = {
                "url": "/attendance/api/camera/event/",
                "protocol": "HTTPS",
                "format": "JSON",
                "addressing_type": "hostname",
                "host": "ad.namspi.uz",
                "port": "443"
            }
    except Exception as e:
        logger.debug("ISAPI httpHost error: %s", e)
        result["http_host"] = {
            "url": "/attendance/api/camera/event/",
            "protocol": "HTTPS",
            "format": "JSON",
            "addressing_type": "hostname",
            "host": "ad.namspi.uz",
            "port": "443"
        }

    return JsonResponse(result)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_save_isapi_config(request):
    """Kameraning ISAPI sozlamalarini (Video, OSD, EHome, HTTP Push Domen) yangilash."""
    import xml.etree.ElementTree as ET
    try:
        data = json.loads(request.body or "{}")
        camera_id = data.get("camera_id")
        camera = Camera.objects.get(id=int(camera_id))
        auth = HTTPDigestAuth(camera.username or 'admin', camera.password or 'parol400')
        ip = camera.ip
        port = camera.port or 80
        base = f"http://{ip}:{port}"

        # 1. Update Video Stream 101
        resolution = data.get("resolution")
        fps_val = data.get("fps")
        bitrate_val = data.get("bitrate")
        codec_val = data.get("codec")
        channel_name = data.get("channel_name")

        r_stream = requests.get(f"{base}/ISAPI/Streaming/channels/101", auth=auth, timeout=3.0)
        if r_stream.status_code == 200:
            root = ET.fromstring(r_stream.text)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            ET.register_namespace('', 'http://www.hikvision.com/ver20/XMLSchema')

            if channel_name:
                cn = root.find('.//h:channelName', ns)
                if cn is not None: cn.text = str(channel_name)

            if resolution and 'x' in resolution:
                parts = resolution.split('x')
                w = root.find('.//h:videoResolutionWidth', ns)
                h = root.find('.//h:videoResolutionHeight', ns)
                if w is not None: w.text = parts[0]
                if h is not None: h.text = parts[1]

            if fps_val:
                f_node = root.find('.//h:maxFrameRate', ns)
                if f_node is not None: f_node.text = str(int(fps_val) * 100)

            if bitrate_val:
                vbr = root.find('.//h:vbrUpperCap', ns)
                cbr = root.find('.//h:constantBitRate', ns)
                if vbr is not None: vbr.text = str(bitrate_val)
                if cbr is not None: cbr.text = str(bitrate_val)

            if codec_val:
                c_node = root.find('.//h:videoCodecType', ns)
                if c_node is not None: c_node.text = str(codec_val)

            put_body = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            requests.put(f"{base}/ISAPI/Streaming/channels/101", auth=auth, data=put_body, headers={'Content-Type': 'application/xml'}, timeout=3.0)

        # 2. Update EHome ISUP settings
        if "ehome_enabled" in data:
            e_en = str(data.get("ehome_enabled", True)).lower()
            e_ip = data.get("ehome_ip", "10.10.0.40")
            e_port = data.get("ehome_port", 8660)
            e_key = data.get("ehome_key", "facex2024")
            e_dev_id = data.get("ehome_device_id", camera.serial_number[-9:] if camera.serial_number else "GU3411289")

            ehome_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Ehome version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<enabled>{e_en}</enabled>
<addressingFormatType>ipaddress</addressingFormatType>
<ipAddress>{e_ip}</ipAddress>
<portNo>{e_port}</portNo>
<customDeviceID>{e_dev_id}</customDeviceID>
<secretKey>{e_key}</secretKey>
<version>5.0</version>
</Ehome>'''
            requests.put(f"{base}/ISAPI/System/Network/ehome", auth=auth, data=ehome_xml, headers={'Content-Type': 'application/xml'}, timeout=3.0)

        # 3. Update HTTP Host Event Push Destination (Domen & URL Path)
        if "http_host_domain" in data or "http_host_url" in data:
            h_domain = data.get("http_host_domain", "ad.namspi.uz").strip()
            h_port = data.get("http_host_port", 443)
            h_url = data.get("http_host_url", "/attendance/api/camera/event/").strip()
            h_proto = data.get("http_host_protocol", "HTTP").upper()
            h_fmt = data.get("http_host_format", "JSON")
            is_ip = any(c.isdigit() for c in h_domain.split('.')) and len(h_domain.split('.')) == 4

            if is_ip:
                host_xml_part = f"<addressingFormatType>ipaddress</addressingFormatType><ipAddress>{h_domain}</ipAddress>"
            else:
                host_xml_part = f"<addressingFormatType>hostname</addressingFormatType><hostName>{h_domain}</hostName>"

            http_host_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<HttpHostNotification version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<id>1</id>
<url>{h_url}</url>
<protocolType>{h_proto}</protocolType>
<parameterFormatType>{h_fmt}</parameterFormatType>
{host_xml_part}
<portNo>{h_port}</portNo>
<userName></userName>
<httpAuthenticationMethod>none</httpAuthenticationMethod>
<httpBroken>true</httpBroken>
</HttpHostNotification>'''
            requests.put(f"{base}/ISAPI/Event/notification/httpHosts/1", auth=auth, data=http_host_xml, headers={'Content-Type': 'application/xml'}, timeout=3.0)

        # 4. Update camera face detection in database
        if "enable_face_detection" in data:
            camera.enable_face_detection = bool(data.get("enable_face_detection"))
            camera.save()

        return JsonResponse({"success": True, "message": "Barcha sozlamalar muvaffaqiyatli saqlandi va kameraga yuborildi!"})
    except Exception as e:
        friendly_err = format_camera_isapi_error(e, ip=getattr(camera, 'ip', ''), port=getattr(camera, 'port', 80), username=getattr(camera, 'username', 'admin'))
        return JsonResponse({"success": False, "message": f"ISAPI sozlash xatosi: {friendly_err}"}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_sync_time(request):
    """Kamera vaqtini serverning joriy vaqti bilan bir zumda sinxronlash."""
    import datetime
    try:
        data = json.loads(request.body or "{}")
        camera_id = data.get("camera_id")
        camera = Camera.objects.get(id=int(camera_id))
        auth = HTTPDigestAuth(camera.username or 'admin', camera.password or 'parol400')
        ip = camera.ip
        port = camera.port or 80
        username = camera.username or "admin"

        now_str = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S+05:00')
        time_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Time version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<timeMode>manual</timeMode>
<localTime>{now_str}</localTime>
<timeZone>CST-5:00:00</timeZone>
</Time>'''

        r = requests.put(f"http://{ip}:{port}/ISAPI/System/time", auth=auth, data=time_xml, headers={'Content-Type': 'application/xml'}, timeout=4.0)
        if r.status_code in [200, 201, 204]:
            return JsonResponse({"success": True, "message": f"Vaqt muvaffaqiyatli sinxronlandi: {now_str}"})
        friendly_err = format_camera_isapi_error(f"HTTP {r.status_code}", ip=ip, port=port, username=username)
        return JsonResponse({"success": False, "message": friendly_err}, status=400)
    except Exception as e:
        friendly_err = format_camera_isapi_error(e, ip=ip if 'ip' in locals() else '', port=port if 'port' in locals() else 80, username=username if 'username' in locals() else 'admin')
        return JsonResponse({"success": False, "message": friendly_err}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_send_raw_isapi(request):
    """Admin uchun maxsus ISAPI so'rov konsoli (GET/PUT/POST) - XML/JSON ni tushunarli formatga o'tkazadi."""
    import xml.etree.ElementTree as ET
    try:
        data = json.loads(request.body or "{}")
        camera_id = data.get("camera_id")
        method = data.get("method", "GET").upper()
        path = data.get("path", "/ISAPI/System/deviceInfo")
        payload = data.get("payload", "")

        camera = Camera.objects.get(id=int(camera_id))
        auth = HTTPDigestAuth(camera.username or 'admin', camera.password or 'parol400')
        url = f"http://{camera.ip}:{camera.port or 80}{path}"

        headers = {'Content-Type': 'application/xml'}
        if method == 'GET':
            r = requests.get(url, auth=auth, timeout=5.0)
        elif method == 'PUT':
            r = requests.put(url, auth=auth, data=payload, headers=headers, timeout=5.0)
        elif method == 'POST':
            r = requests.post(url, auth=auth, data=payload, headers=headers, timeout=5.0)
        else:
            return JsonResponse({"success": False, "message": "Noto'g'ri HTTP metod"}, status=400)

        # XML ni toza JSON ob'ektiga aylantiramiz
        parsed_dict = None
        if r.text and ('<?xml' in r.text or '<' in r.text):
            try:
                root = ET.fromstring(r.text)
                def elem_to_dict(elem):
                    tag = elem.tag.split('}')[-1]
                    children = list(elem)
                    if children:
                        dd = {}
                        for c in children:
                            cd = elem_to_dict(c)
                            for k, v in cd.items():
                                if k in dd:
                                    if not isinstance(dd[k], list):
                                        dd[k] = [dd[k]]
                                    dd[k].append(v)
                                else:
                                    dd[k] = v
                        return {tag: dd}
                    else:
                        return {tag: elem.text or ""}
                parsed_dict = elem_to_dict(root)
            except Exception:
                pass

        return JsonResponse({
            "success": True,
            "status_code": r.status_code,
            "response_text": r.text,
            "parsed_data": parsed_dict
        })
    except Exception as e:
        friendly_err = format_camera_isapi_error(e, ip=getattr(camera, 'ip', '') if 'camera' in locals() else '', port=getattr(camera, 'port', 80) if 'camera' in locals() else 80, username=getattr(camera, 'username', 'admin') if 'camera' in locals() else 'admin')
        return JsonResponse({"success": False, "message": f"ISAPI so'rovi bajarilmadi: {friendly_err}"}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_detect_face(request):
    """Joriy kamera kadrida AI (InsightFace / RetinaFace) orqali yuzlarni aniqlash va tekshirish."""
    try:
        data = json.loads(request.body or "{}")
        device_id = data.get("device_id")
        camera_id = data.get("camera_id")

        frame_bytes = None
        if device_id:
            try:
                r = requests.get(f"{ISUP_API_BASE}/devices/{device_id}/snapshot.jpg", timeout=3.0)
                if r.status_code == 200 and r.content.startswith(b'\xff\xd8'):
                    frame_bytes = r.content
            except Exception:
                pass

        if not frame_bytes and camera_id:
            try:
                cam = Camera.objects.get(id=int(camera_id))
                from camera.views import capture_snapshot_from_camera
                frame_bytes = capture_snapshot_from_camera(cam)
            except Exception:
                pass

        if not frame_bytes:
            return JsonResponse({"success": False, "message": "Kameradan jonli kadr olib bo'lmadi"}, status=400)

        import numpy as np
        import cv2
        nparr = np.frombuffer(frame_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        from camera.ai_face import get_face_analyzer
        app = get_face_analyzer()
        if not app:
            return JsonResponse({"success": False, "message": "AI model yuklanmagan"}, status=500)

        faces = app.get(img)
        faces_count = len(faces) if faces else 0

        return JsonResponse({
            "success": True,
            "message": f"Kadr GPU da tahlil qilindi: {faces_count} ta yuz aniqlandi.",
            "faces_count": faces_count
        })
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)


@login_required(login_url='login')
@never_cache
def isup_camera_detail_view(request, camera_id):
    """Tanlangan kameraning to'liq ISUP va texnik tafsilotlar sahifasi."""
    camera = get_object_or_404(Camera, id=camera_id)
    health = get_isup_health()
    raw_devices = get_isup_devices()

    # Match with ISUP device
    matched_dev = None
    for d in raw_devices:
        if not isinstance(d, dict):
            continue
        dev_id = str(d.get("device_id") or d.get("id") or "")
        dev_ip = str(d.get("remote_ip") or d.get("ip") or "")
        dev_sn = str(d.get("serial") or "")
        if dev_ip and dev_ip == camera.ip:
            matched_dev = d
            break
        if camera.serial_number and ((dev_id and dev_id in camera.serial_number) or (dev_sn and dev_sn in camera.serial_number)):
            matched_dev = d
            break

    is_online = bool(matched_dev.get("online")) if matched_dev else False
    isup_device_id = matched_dev.get("device_id") if matched_dev else (camera.serial_number[-9:] if camera.serial_number and len(camera.serial_number) >= 9 else camera.ip)
    isup_status = "online" if is_online else ("offline" if matched_dev else "not_connected")

    # Ushbu kamera orqali qayd etilgan oxirgi davomatlar va yuz qirqishlar (FaceLogs)
    from attendance.models import Attendance
    from camera.models import FaceLog
    from django.core.paginator import Paginator
    from django.db.models import Q

    search_query = request.GET.get('q', '').strip()
    filter_date = request.GET.get('date', '').strip()

    qs = Attendance.objects.filter(
        Q(entry_camera=camera) | Q(exit_camera=camera) | Q(last_seen_camera=camera)
    ).select_related('user')

    if search_query:
        qs = qs.filter(
            Q(user__username__icontains=search_query) |
            Q(user__first_name__icontains=search_query) |
            Q(user__second_name__icontains=search_query) |
            Q(user__employee_id_number__icontains=search_query) |
            Q(user__student_id_number__icontains=search_query)
        )
    if filter_date:
        qs = qs.filter(date=filter_date)

    qs = qs.order_by('-date', '-last_seen')

    paginator = Paginator(qs, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # Kameradan kelgan so'nggi barcha yuz rasmlari (Face Capture Logs)
    from datetime import date as date_cls
    import datetime

    sn_filter = Q(device_id=camera.serial_number) if camera.serial_number else Q()
    if camera.serial_number and 'AAWR' in camera.serial_number:
        sn_filter |= Q(device_id__icontains=camera.serial_number.split('AAWR')[-1])

    # Face galereya sanasi (default: bugun)
    face_date_str = request.GET.get('face_date', '').strip()
    if face_date_str:
        try:
            face_date = datetime.datetime.strptime(face_date_str, '%Y-%m-%d').date()
        except ValueError:
            face_date = date_cls.today()
    else:
        face_date = date_cls.today()
        face_date_str = str(face_date)

    face_qs = FaceLog.objects.filter(
        Q(camera=camera) | Q(camera_ip=camera.ip) | sn_filter,
        captured_at__date=face_date,
    ).select_related('matched_user').order_by('-captured_at')

    # Pagination for face logs (120 per page for compact view)
    face_page_number = request.GET.get('face_page', 1)
    from django.core.paginator import Paginator as FacePaginator
    face_paginator = FacePaginator(face_qs, 120)
    face_page_obj = face_paginator.get_page(face_page_number)

    recent_face_logs = list(face_page_obj)
    matched_face_logs = [l for l in recent_face_logs if l.matched_user is not None]
    unknown_face_logs = [l for l in recent_face_logs if l.matched_user is None]


    # Kamera sozlamalarini yangilash (POST)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            camera.name = name
        camera.port = int(request.POST.get('port', 80))
        camera.username = request.POST.get('username', 'admin').strip()
        pwd = request.POST.get('password', '').strip()
        if pwd:
            camera.password = pwd
        camera.is_entry_camera = 'is_entry_camera' in request.POST
        camera.is_exit_camera = 'is_exit_camera' in request.POST
        camera.enable_face_detection = 'enable_face_detection' in request.POST
        camera.is_lesson_camera = 'is_lesson_camera' in request.POST
        camera.save()
        messages.success(request, "Kamera sozlamalari muvaffaqiyatli saqlandi!")
        return redirect('isup_camera_detail', camera_id=camera.id)

    breadcrumbs = [
        {"name": "Asosiy sahifa", "url": "/"},
        {"name": "Sayt sozlamalari", "url": "/settings/site/"},
        {"name": "ISUP Boshqaruvi", "url": "/settings/isup/"},
        {"name": f"{camera.name or camera.ip} (Tafsilotlar)", "url": ""},
    ]

    context = {
        "camera": camera,
        "is_online": is_online,
        "isup_device_id": isup_device_id,
        "isup_status": isup_status,
        "matched_dev": matched_dev,
        "health": health,
        "page_obj": page_obj,
        "recent_face_logs": recent_face_logs,
        "matched_face_logs": matched_face_logs,
        "unknown_face_logs": unknown_face_logs,
        "face_page_obj": face_page_obj,
        "face_date_str": face_date_str,
        "face_date": face_date,
        "today_str": str(date_cls.today()),
        "search_query": search_query,
        "filter_date": filter_date,
        "breadcrumbs": breadcrumbs,
        "page_title": f"{camera.name or camera.ip} — Kamera Tafsilotlari",
    }
    return render(request, "settings/isup_camera_detail.html", context)


@login_required(login_url='login')
def isup_api_get_pairs(request):
    """Barcha kamera juftliklari (Nazorat punktlari) va ularning real-vaqt ma'lumotlarini JSON qaytarish."""
    from camera.models import CameraPair, Building
    from attendance.models import Attendance
    from django.utils import timezone

    today = timezone.now().date()
    raw_devices = get_isup_devices()
    pairs = CameraPair.objects.select_related('building', 'entry_camera', 'exit_camera').filter(is_active=True)

    result = []
    for p in pairs:
        in_online = False
        in_dev_id = None
        if p.entry_camera:
            in_match = next((d for d in raw_devices if (d.get("remote_ip") == p.entry_camera.ip or (p.entry_camera.serial_number and d.get("device_id") in p.entry_camera.serial_number))), None)
            in_online = bool(in_match and in_match.get("online"))
            in_dev_id = in_match.get("device_id") if in_match else (p.entry_camera.serial_number[-9:] if p.entry_camera.serial_number else p.entry_camera.ip)

        out_online = False
        out_dev_id = None
        if p.exit_camera:
            out_match = next((d for d in raw_devices if (d.get("remote_ip") == p.exit_camera.ip or (p.exit_camera.serial_number and d.get("device_id") in p.exit_camera.serial_number))), None)
            out_online = bool(out_match and out_match.get("online"))
            out_dev_id = out_match.get("device_id") if out_match else (p.exit_camera.serial_number[-9:] if p.exit_camera.serial_number else p.exit_camera.ip)

        entry_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera).count() if p.entry_camera else 0
        exit_count = Attendance.objects.filter(date=today, exit_camera=p.exit_camera).count() if p.exit_camera else 0
        inside_count = Attendance.objects.filter(date=today, entry_camera=p.entry_camera, is_present=True).count() if p.entry_camera else 0

        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description or "",
            "building_id": p.building_id,
            "building_name": p.building.name if p.building else "",
            "entry_camera_id": p.entry_camera_id,
            "entry_camera_name": p.entry_camera.name if p.entry_camera else "--",
            "entry_camera_ip": p.entry_camera.ip if p.entry_camera else "--",
            "entry_camera_model": p.entry_camera.device_model if p.entry_camera else "",
            "entry_camera_sn": p.entry_camera.serial_number if p.entry_camera else "",
            "entry_camera_mac": p.entry_camera.mac_address if p.entry_camera else "",
            "entry_online": in_online,
            "entry_dev_id": in_dev_id,
            "exit_camera_id": p.exit_camera_id,
            "exit_camera_name": p.exit_camera.name if p.exit_camera else "--",
            "exit_camera_ip": p.exit_camera.ip if p.exit_camera else "--",
            "exit_camera_model": p.exit_camera.device_model if p.exit_camera else "",
            "exit_camera_sn": p.exit_camera.serial_number if p.exit_camera else "",
            "exit_camera_mac": p.exit_camera.mac_address if p.exit_camera else "",
            "exit_online": out_online,
            "exit_dev_id": out_dev_id,
            "entry_count_today": entry_count,
            "exit_count_today": exit_count,
            "inside_count_today": inside_count,
        })
    return JsonResponse({"success": True, "pairs": result})


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_save_pair(request):
    """Kamera juftligini (Bino / Nazorat Punkti) yaratish yoki tahrirlash."""
    from camera.models import Camera, CameraPair, Building
    try:
        data = json.loads(request.body or "{}")
        pair_id = data.get("id")
        name = data.get("name", "").strip()
        building_id = data.get("building_id")
        entry_cam_id = data.get("entry_camera_id")
        exit_cam_id = data.get("exit_camera_id")
        description = data.get("description", "").strip()

        if not name:
            return JsonResponse({"success": False, "message": "Juftlik yoki bino nomi kiritilishi shart!"}, status=400)

        building = Building.objects.filter(id=int(building_id)).first() if building_id else None
        entry_cam = Camera.objects.filter(id=int(entry_cam_id)).first() if entry_cam_id else None
        exit_cam = Camera.objects.filter(id=int(exit_cam_id)).first() if exit_cam_id else None

        if pair_id:
            pair = CameraPair.objects.filter(id=int(pair_id)).first()
            if not pair:
                return JsonResponse({"success": False, "message": "Juftlik topilmadi!"}, status=404)
            pair.name = name
            pair.building = building
            pair.entry_camera = entry_cam
            pair.exit_camera = exit_cam
            pair.description = description
            pair.save()
        else:
            pair = CameraPair.objects.create(
                name=name,
                building=building,
                entry_camera=entry_cam,
                exit_camera=exit_cam,
                description=description,
                is_active=True
            )

        # Kameralarning rollarini avtomatik yangilash
        if entry_cam:
            entry_cam.is_entry_camera = True
            entry_cam.enable_face_detection = True
            entry_cam.save()
        if exit_cam:
            exit_cam.is_exit_camera = True
            exit_cam.enable_face_detection = True
            exit_cam.save()

        return JsonResponse({
            "success": True,
            "message": f"'{pair.name}' muvaffaqiyatli saqlandi!",
            "pair_id": pair.id
        })
    except Exception as exc:
        logger.exception("Error saving camera pair: %s", exc)
        return JsonResponse({"success": False, "message": str(exc)}, status=500)


@csrf_exempt
@login_required(login_url='login')
@require_POST
def isup_api_delete_pair(request, pair_id):
    """Kamera juftligini o'chirish."""
    from camera.models import CameraPair
    try:
        pair = CameraPair.objects.filter(id=int(pair_id)).first()
        if not pair:
            return JsonResponse({"success": False, "message": "Juftlik topilmadi!"}, status=404)
        pair_name = pair.name
        pair.delete()
        return JsonResponse({"success": True, "message": f"'{pair_name}' juftligi o'chirildi!"})
    except Exception as exc:
        return JsonResponse({"success": False, "message": str(exc)}, status=500)


