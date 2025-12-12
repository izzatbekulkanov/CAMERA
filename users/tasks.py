# users/tasks.py

from celery import shared_task
from users.models import CustomUser, FaceEncoding
import numpy as np
import cv2
import os
from django.conf import settings

try:
    from insightface.app import FaceAnalysis
    print("[INSIGHTFACE] Model yuklanmoqda (buffalo_l, Celery)...")
    app = FaceAnalysis(
        name="buffalo_l",
        providers=['CUDAExecutionProvider', 'CPUExecutionProvider']
    )
    app.prepare(ctx_id=0, det_size=(640, 640))
    print("[INSIGHTFACE] Model yuklandi! (Celery worker)")
except Exception as e:
    print(f"[INSIGHTFACE XATO][Celery] {e}")
    app = None


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_insightface_encoding(self, user_id: int):
    """
    Celery background task:
    - Foydalanuvchining asosiy suratidan InsightFace (buffalo_l) 512D encoding yaratadi
    - Rasm serverda /media/... ko‘rinishida bo‘lsa → diskdan o‘qiydi (HTTP emas!)
    - user + model_version uchun FaceEncoding.update_or_create(...) qiladi
    """
    if app is None:
        return f"[ERROR] user {user_id} — InsightFace modeli yuklanmagan"

    try:
        user = CustomUser.objects.get(id=user_id)

        if not user.image or not user.image.name:
            return f"[SKIP] user {user_id} — rasm yo‘q"

        # --- RASMNI YUKLASH ---
        img = None

        # Avval FileField.path orqali ogamiz
        try:
            if hasattr(user.image, "path") and os.path.exists(user.image.path):
                img = cv2.imread(user.image.path)
        except Exception:
            img = None

        if img is None:
            image_url = user.image.url

            if image_url.startswith("/media/"):
                rel_path = user.image.name
                path = os.path.join(settings.MEDIA_ROOT, rel_path)
                if not os.path.exists(path):
                    return f"[ERROR] user {user_id} — Fayl topilmadi: {path}"
                img = cv2.imread(path)
                if img is None:
                    return f"[ERROR] user {user_id} — Rasmni o‘qib bo‘lmadi (cv2.imread None)"
            else:
                import requests

                resp = requests.get(image_url, timeout=20)
                if resp.status_code != 200:
                    return f"[ERROR] user {user_id} — HTTP {resp.status_code}"

                img_bytes = np.frombuffer(resp.content, np.uint8)
                img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
                if img is None:
                    return f"[ERROR] user {user_id} — HTTP rasmni ochib bo‘lmadi"

        # --- INSIGHTFACE BILAN ENCODING ---
        faces = app.get(img)

        if len(faces) == 0:
            return f"[WARN] user {user_id} — yuz topilmadi (yorug‘lik/burchakni tekshiring)"

        best_face = max(faces, key=lambda x: x.det_score)

        if best_face.det_score < 0.3:
            return f"[WARN] user {user_id} — ishonchlilik past ({best_face.det_score:.2f})"

        embedding_512d = best_face.normed_embedding  # numpy array (512)

        # Eski strategiya: .all().delete() + .create()
        # Yangi strategiya: update_or_create → bitta row, dublikat yo‘q
        FaceEncoding.objects.update_or_create(
            user=user,
            model_version="insightface_buffalo_l",
            defaults={
                "encoding_data": embedding_512d.tolist(),
                "confidence": float(best_face.det_score),
            },
        )

        return f"[SUCCESS] user {user_id} — encoding yaratildi (conf: {best_face.det_score:.3f})"

    except Exception as e:
        # xato bo‘lsa retry
        raise self.retry(exc=e)