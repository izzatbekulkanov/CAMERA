# camera/views.py
import json
import logging
from django.db import transaction
import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, Http404
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from camera.models import Camera

logger = logging.getLogger(__name__)


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
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Jonli ko‘rish', 'url': None},
    ]
    context = {
        'cameras': cameras,
        'breadcrumbs': breadcrumbs,
        'total_cameras': cameras.count(),
    }
    return render(request, 'cameras/view_cameras.html', context)


@login_required(login_url='login')
def ip_camera_view_auto(request):
    """is_active=True va enable_face_detection=True kameralardan birinchisini ko‘rsatish."""
    qs = Camera.objects.filter(is_active=True, enable_face_detection=True)
    if not qs.exists():
        raise Http404("Faol va yuzni aniqlash yoqilgan kamera topilmadi.")

    camera = qs.first()
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Avto IP Kamera Ko‘rinishi', 'url': None},
    ]
    context = {
        'camera': camera,
        'breadcrumbs': breadcrumbs,
        'total_cameras': qs.count(),
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
