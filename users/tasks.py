from celery import shared_task
from users.models import CustomUser, FaceEncoding
from PIL import Image
import face_recognition
import numpy as np
from io import BytesIO
import requests


@shared_task
def create_face_encoding(user_id):
    """
    Background task: rasmni yuklab, encoding yaratadi.
    Agar encoding mavjud bo‘lsa, skip qiladi.
    """
    try:
        user = CustomUser.objects.get(id=user_id)

        # Agar rasm yo‘q bo‘lsa
        if not user.image or not user.image.name:
            return f"[SKIP] ID {user_id} — rasm yo‘q."

        # Agar foydalanuvchida allaqachon encoding mavjud bo‘lsa
        if user.face_encodings.exists():
            return f"[SKIP] ID {user_id} — encoding allaqachon mavjud."

        # Rasmni yuklash
        response = requests.get(user.image.url, timeout=15)
        if response.status_code != 200:
            return f"[ERROR] ID {user_id} — HTTP {response.status_code}"

        img = Image.open(BytesIO(response.content))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img = img.resize((400, 400))
        img_array = np.array(img)

        # Yuzni kodlash
        encodings = face_recognition.face_encodings(img_array, num_jitters=1, model='small')
        if not encodings:
            return f"[WARN] ID {user_id} — Yuz topilmadi"

        # Encoding yaratish
        FaceEncoding.objects.create(
            user=user,
            encoding_data=encodings[0].tolist()
        )
        return f"[OK] ID {user_id} — encoding yaratildi"

    except Exception as e:
        return f"[ERROR] ID {user_id} — {e}"
