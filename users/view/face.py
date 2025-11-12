from django.db.models import Q
import logging
from io import BytesIO
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.db import transaction
from PIL import Image
import face_recognition
import numpy as np
import requests
import traceback
from users.models import CustomUser, FaceEncoding

logger = logging.getLogger(__name__)
import concurrent.futures

MAX_THREADS = 8  # 16GB RAM uchun xavfsiz limit (8 parallel thread)


def _process_single_user(user, request):
    """Bitta foydalanuvchini qayta ishlovchi yordamchi funksiya."""
    try:
        if not user.image or not user.image.name:
            return None, f"[SKIP] ID {user.id} — rasm mavjud emas."

        image_url = request.build_absolute_uri(user.image.url)
        encoding, error = _get_encoding_from_image_url(image_url)

        if encoding:
            if not isinstance(encoding, list):
                encoding = encoding.tolist()

            FaceEncoding.objects.update_or_create(
                user=user,
                defaults={'encoding_data': encoding}
            )
            return True, f"[OK] ID {user.id} — encoding saqlandi."

        else:
            return None, f"[WARN] ID {user.id}: {error}"

    except Exception as e:
        return None, f"[ERROR] ID {user.id}: {e}"


def _get_encoding_from_image_url(image_url):
    """
    Rasm URL dan encoding yaratadi (DEBUG versiyasi).
    """
    print(f"\n[DEBUG] Yuzni kodlash jarayoni boshlandi: {image_url}")

    try:
        # 1. Rasmni yuklash
        print("[STEP 1] Rasm yuklanmoqda...")
        response = requests.get(image_url, timeout=15)

        if response.status_code != 200:
            print(f"[ERROR] Rasm yuklanmadi! HTTP {response.status_code}")
            return None, f"HTTP {response.status_code}"

        print(f"[OK] Rasm yuklandi ({len(response.content)} bayt).")

        # 2. Rasmni ochish va tayyorlash
        print("[STEP 2] Rasmni ochish...")
        img = Image.open(BytesIO(response.content))

        print(f"[INFO] Rasm rejimi: {img.mode}, o‘lchami: {img.size}")
        if img.mode != 'RGB':
            img = img.convert('RGB')
            print("[INFO] Rasm RGB formatga o‘tkazildi.")

        # Kattalikni kamaytirish (tezlik uchun)
        img = img.resize((400, 400))
        print(f"[INFO] Rasm {img.size} gacha o‘lchamga o‘tkazildi.")

        # Numpy massivga o‘tkazish
        img_array = np.array(img)
        print("[STEP 3] Numpy massivga o‘tkazildi.")

        # 3. Yuzni kodlash
        print("[STEP 4] Yuzni aniqlash va encoding yaratish...")
        encodings = face_recognition.face_encodings(img_array, num_jitters=1, model='small')

        if not encodings:
            print("[X] Yuz topilmadi.")
            return None, "Yuz topilmadi"

        print("[OK] Yuz topildi va encoding yaratildi.")
        print(f"[DEBUG] Encoding uzunligi: {len(encodings[0])}")

        return encodings[0].tolist(), None

    except Exception as e:
        print(f"[EXCEPTION] Xatolik yuz berdi: {e}")
        return None, str(e)


def _generate_face_encodings_by_role(request, role_name):
    """Talaba yoki xodimlar uchun umumiy parallel jarayon."""
    try:
        with transaction.atomic():
            users = CustomUser.objects.filter(
                Q(role=role_name),
                Q(image__isnull=False),
                ~Q(image='')
            ).only('id', 'image')

            print(f"[DEBUG] {role_name} uchun {users.count()} ta foydalanuvchi topildi.")
            created, skipped, errors = 0, 0, []

            # ⚡ Parallel bajarish
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = {
                    executor.submit(_process_single_user, user, request): user.id
                    for user in users
                }

                for future in concurrent.futures.as_completed(futures):
                    result, message = future.result()
                    print(message)
                    if result:
                        created += 1
                    else:
                        skipped += 1
                        if "[ERROR]" in message or "[WARN]" in message:
                            errors.append(message)

        return JsonResponse({
            "success": True,
            "yaratildi": created,
            "o‘tkazildi": skipped,
            "xatolar": errors[:10],
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("[FATAL ERROR]", tb)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@csrf_exempt
def generate_students_face_encodings(request):
    return _generate_face_encodings_by_role(request, CustomUser.Role.STUDENT)


@login_required
@csrf_exempt
def generate_employees_face_encodings(request):
    return _generate_face_encodings_by_role(request, CustomUser.Role.EMPLOYEE)


@login_required
@csrf_exempt
def generate_single_encoding(request, user_id):
    if request.method != 'POST':
        return JsonResponse({"error": "POST kerak"}, status=405)

    try:
        user = CustomUser.objects.filter(id=user_id, image__isnull=False).only('image').first()
        if not user:
            return JsonResponse({"success": False, "error": "Rasm yo‘q"}, status=404)

        image_url = request.build_absolute_uri(user.image.url)
        encoding, error = _get_encoding_from_image_url(image_url)

        if not encoding:
            return JsonResponse({"success": False, "error": error or "Yuz topilmadi"}, status=400)

        obj, created = FaceEncoding.objects.update_or_create(
            user=user,
            defaults={'encoding_data': encoding}
        )

        return JsonResponse({
            "success": True,
            "message": f"{'Yaratildi' if created else 'Yangilandi'}",
            "encoding_id": obj.id
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@login_required
@csrf_exempt
def clear_all_face_encodings(request):
    """
    Barcha foydalanuvchilarning encodinglarini o‘chiradi.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        deleted, _ = FaceEncoding.objects.all().delete()
        return JsonResponse({
            "success": True,
            "message": f"{deleted} encoding entries removed"
        })
    except Exception as exc:
        return JsonResponse({
            "success": False,
            "error": str(exc)
        }, status=500)
