# camera/views.py
import json
import socket
import cv2
import requests
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from camera.models import Camera


def get_usb_cameras(max_cams=5):
    cams = []
    for i in range(max_cams):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            cams.append({'id': i, 'name': f'USB Kamera {i}'})
            cap.release()
    return cams


@login_required(login_url='login')
def usb_camera_view(request):
    cameras = get_usb_cameras()
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Kameralar', 'url': '/cameras/list/'},
        {'name': 'USB Kamera', 'url': None},
    ]
    return render(request, 'cameras/usb_camera.html', {
        'cameras': cameras,
        'breadcrumbs': breadcrumbs
    })


@csrf_exempt
@login_required
@require_POST
def api_add_camera(request):
    try:
        data = json.loads(request.body)
    except Exception as e:
        print("[DEBUG] JSON parse xatolik:", e)
        return JsonResponse({"success": False, "message": "JSON format xatolik"})

    ip = data.get("ip")
    port = int(data.get("port", 80))
    username = data.get("username", "admin")
    password = data.get("password")

    if not ip or not password:
        return JsonResponse({"success": False, "message": "IP va parol majburiy"})

    print(f"[DEBUG] Formadan kelgan: IP={ip}, port={port}, username={username}, password={password}")
    url = f"http://{ip}:{port}"

    try:
        r = requests.get(url, auth=(username, password), timeout=6, verify=False)
        print(f"[DEBUG] {url} status_code={r.status_code}")

        if r.status_code in [200, 401, 302]:
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
            print(f"[DEBUG] Kamera saqlandi: {camera}, created={created}")
            return JsonResponse({
                "success": True,
                "message": "Kamera muvaffaqiyatli qo‘shildi!"
            })
    except Exception as e:
        print("[DEBUG] Kamera bilan bog‘lanishda xatolik:", e)

    return JsonResponse({
        "success": False,
        "message": "Kamera javob bermadi yoki login/parol noto‘g‘ri"
    })

# Faol kameralar ro‘yxati
@login_required
def api_active_cameras(request):
    cams = Camera.objects.filter(is_active=True).order_by("-added_at")
    data = [{
        "ip": c.ip,
        "port": c.port,
        "name": c.name or f"Kamera {c.ip}",
    } for c in cams]
    return JsonResponse({"cameras": data})

@csrf_exempt
@login_required
@require_POST
def api_remove_camera(request, ip):
    deleted_count, _ = Camera.objects.filter(ip=ip).delete()
    return JsonResponse({
        "success": True,
        "deleted": deleted_count,
        "message": f"{ip} o‘chirildi" if deleted_count else "Kamera topilmadi"
    })



@login_required(login_url='login')
def add_camera_view(request):
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Kameralar', 'url': '/cameras/list/'},
        {'name': 'Kamera qo‘shish', 'url': None},
    ]

    return render(request, 'cameras/add_camera.html', {
        'breadcrumbs': breadcrumbs,
    })

@login_required(login_url='login')
def view_cameras(request):
    """
    Jonli kameralarni grid ko‘rinishida ko‘rsatadi
    1, 4, 9, 16 ta kamera avto-grid (responsive)
    """
    cameras = Camera.objects.filter(is_active=True).order_by('name', 'ip')

    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Jonli ko‘rish', 'url': None},
    ]

    return render(request, 'cameras/view_cameras.html', {
        'cameras': cameras,
        'breadcrumbs': breadcrumbs,
        'total_cameras': cameras.count(),
    })

@csrf_exempt
@login_required(login_url='login')
@require_POST
def api_update_camera(request, ip):
    """
    Kameraning is_active va/yoki enable_face_detection maydonlarini yangilash uchun API.
    JSON body:
    {
        "is_active": true/false (ixtiyoriy),
        "enable_face_detection": true/false (ixtiyoriy)
    }
    """
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"success": False, "message": "JSON format xato"}, status=400)

    try:
        camera = Camera.objects.get(ip=ip)
    except Camera.DoesNotExist:
        return JsonResponse({"success": False, "message": "Kamera topilmadi"}, status=404)

    updated_fields = []

    if "is_active" in data:
        camera.is_active = bool(data["is_active"])
        updated_fields.append("is_active")

    if "enable_face_detection" in data:
        camera.enable_face_detection = bool(data["enable_face_detection"])
        updated_fields.append("enable_face_detection")

    if not updated_fields:
        return JsonResponse({"success": False, "message": "Yangilash uchun ma'lumot berilmagan"}, status=400)

    camera.save(update_fields=updated_fields)

    return JsonResponse({
        "success": True,
        "message": "Kamera sozlamalari yangilandi",
        "updated_fields": updated_fields,
    })


@login_required(login_url='login')
def camera_list_view(request):
    """
    Kameralar ro'yxati:
    - Barcha kameralar (faol / nofaol)
    - Yuqorida statistik kartalar
    - Pastda chiroyli jadval
    """
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

    return render(request, 'cameras/camera_list.html', {
        'breadcrumbs': breadcrumbs,
        'cameras': cameras,
        'stats': stats,
    })