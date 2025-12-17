# users/consumers.py
import json
import aiohttp
import cv2
import numpy as np
import os
import asyncio
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.conf import settings

from attendance.models import SiteSettings
from users.models import CustomUser, FaceEncoding

User = get_user_model()

# ============================
# INSIGHTFACE GLOBAL MODEL
# ============================
try:
    from insightface.app import FaceAnalysis

    print("[INSIGHTFACE] buffalo_l modeli yuklanmoqda...")
    face_app = FaceAnalysis(name="buffalo_l", providers=['CUDAExecutionProvider', 'CPUExecutionProvider'])
    face_app.prepare(ctx_id=0, det_size=(640, 640))
    print("[INSIGHTFACE] Model yuklandi (L40S GPU)")
except Exception as e:
    print(f"[INSIGHTFACE XATO] {e}")
    face_app = None


async def send_json(consumer, data):
    """Yordamchi funksiya — json yuborish"""
    await consumer.send(text_data=json.dumps(data, ensure_ascii=False))


class FaceEncodingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        if face_app is None:
            await self.close(code=1011)
            return
        self.user_id = self.scope["user"].id if self.scope["user"].is_authenticated else None
        await self.accept()
        print(f"[FACE WS] Ulanish: {self.channel_name} (user {self.user_id})")
        await send_json(self, {
            "status": "ready",
            "message": "InsightFace tayyor! Encoding yaratish boshlanishi mumkin.",
            "model": "buffalo_l"
        })

    async def disconnect(self, close_code):
        print(f"[FACE WS] Uzildi: {close_code}")

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            await send_json(self, {"status": "error", "error": "Noto‘g‘ri JSON"})
            return

        action = data.get("action")
        user_id = data.get("user_id")
        role = data.get("role")

        if action == "generate_single" and user_id:
            await self.generate_single(int(user_id))
        elif action == "generate_all" and role:
            await self.generate_all(role)
        else:
            await send_json(self, {"status": "error", "error": "Noto‘g‘ri action"})

    async def generate_single(self, user_id: int):
        # Progressni boshlash
        cache_key = f"sync_progress_{self.user_id}"
        cache.set(cache_key, {"percent": 0, "message": "Boshlanmoqda..."}, timeout=300)
        await send_json(self, {"status": "started", "user_id": user_id})

        user = await database_sync_to_async(
            lambda: CustomUser.objects.filter(id=user_id).first()
        )()
        if not user or not user.image:
            cache.set(cache_key, {"percent": 100, "message": "Rasm yo‘q"}, timeout=300)
            await send_json(
                self,
                {"status": "error", "user_id": user_id, "error": "Foydalanuvchi rasmsiz"},
            )
            return

        # Agar allaqachon buffalo_l encoding bo‘lsa – o‘tkazib yuboramiz
        exists = await database_sync_to_async(
            lambda: user.face_encodings.filter(
                model_version="insightface_buffalo_l"
            ).exists()
        )()
        if exists:
            cache.set(
                cache_key,
                {"percent": 100, "message": "Encoding allaqachon mavjud"},
                timeout=300,
            )
            await send_json(
                self,
                {
                    "status": "skipped",
                    "user_id": user_id,
                    "reason": "Encoding allaqachon mavjud",
                },
            )
            return

        cache.set(
            cache_key,
            {"percent": 50, "message": "Encoding yaratilmoqda..."},
            timeout=300,
        )
        success, message = await self.create_insightface_encoding(user)

        if success:
            cache.set(
                cache_key,
                {"percent": 100, "message": "Muvaffaqiyatli!"},
                timeout=300,
            )
            await send_json(
                self,
                {"status": "success", "user_id": user_id, "message": message},
            )
        else:
            cache.set(cache_key, {"percent": 100, "message": "Xato"}, timeout=300)
            await send_json(
                self,
                {"status": "failed", "user_id": user_id, "error": message},
            )

    async def generate_all(self, role: str):
        """
        Berilgan role uchun:
        - Rasmli userlar
        - VA buffalo_l encodingi hali yo‘q bo‘lganlarini tanlaymiz
        - Har biriga InsightFace encoding yaratamiz
        """
        # 1) Encoding yo‘q userlarni DB darajasida filtrlaymiz
        users = await database_sync_to_async(list)(
            CustomUser.objects
            .filter(role=role, image__isnull=False)
            .exclude(face_encodings__model_version="insightface_buffalo_l")
            .only("id", "image")
            .distinct()
        )

        total = len(users)
        if total == 0:
            await send_json(self, {
                "status": "completed",
                "message": "Bu rol uchun encoding yaratilmagan yangi foydalanuvchi topilmadi"
            })
            return

        cache.set(
            f"sync_progress_{self.user_id}",
            {"percent": 0, "message": f"0/{total} tayyorlanmoqda..."},
            timeout=600,
        )
        await send_json(self, {"status": "started", "total": total, "role": role})

        processed = 0
        success_count = 0

        for user in users:
            processed += 1
            percent = int((processed / total) * 100)

            cache.set(
                f"sync_progress_{self.user_id}",
                {
                    "percent": percent,
                    "message": f"{processed}/{total} — ID {user.id} ishlanmoqda..."
                },
                timeout=600,
            )

            # Asosan bu userlar uchun encoding yo‘q, lekin baribir double-check qilamiz
            has_enc = await database_sync_to_async(
                lambda: user.face_encodings.filter(
                    model_version="insightface_buffalo_l"
                ).exists()
            )()
            if has_enc:
                await send_json(
                    self,
                    {
                        "status": "skipped",
                        "user_id": user.id,
                        "reason": "Encoding allaqachon mavjud",
                        "progress": f"{processed}/{total}",
                    },
                )
                continue

            success, msg = await self.create_insightface_encoding(user)
            if success:
                success_count += 1
                await send_json(
                    self,
                    {
                        "status": "success",
                        "user_id": user.id,
                        "message": msg,
                        "progress": f"{processed}/{total}",
                    },
                )
            else:
                await send_json(
                    self,
                    {
                        "status": "failed",
                        "user_id": user.id,
                        "error": msg,
                        "progress": f"{processed}/{total}",
                    },
                )

            # Katta ro‘yxatda event loopni "nafas oldirish" uchun kichik pauza
            await asyncio.sleep(0.01)

        cache.set(
            f"sync_progress_{self.user_id}",
            {
                "percent": 100,
                "message": f"Yakunlandi! {success_count} ta user uchun encoding yaratildi",
            },
            timeout=600,
        )
        await send_json(
            self,
            {
                "status": "completed",
                "total_processed": processed,
                "success_count": success_count,
                "message": f"{role} roli uchun bulk encoding tayyor bo‘ldi!",
            },
        )

    async def create_insightface_encoding(self, user):
        """
        Bitta foydalanuvchi uchun:
        - Rasmni diskdan (yoki kerak bo‘lsa HTTP’dan) o‘qiydi
        - InsightFace (buffalo_l, GPU) bilan eng yaxshi yuzni topadi
        - FaceEncoding.update_or_create(...) bilan 512D encoding saqlaydi
        """
        if face_app is None:
            return False, "InsightFace modeli yuklanmagan"

        try:
            # 1) Rasmni olish – avval lokal diskdan, keyin HTTP fallback
            img = None

            # Agar FileField bo‘lsa -> .path ishlatish eng tez variat
            try:
                if user.image and hasattr(user.image, "path") and os.path.exists(user.image.path):
                    img = cv2.imread(user.image.path)
            except Exception:
                img = None

            if img is None:
                image_url = user.image.url
                # /media/... bo'lsa baribir lokal file
                if image_url.startswith("/media/") and hasattr(user.image, "name"):
                    rel_path = user.image.name  # masalan: users/images/xxx.jpg
                    path = os.path.join(settings.MEDIA_ROOT, rel_path)
                    if not os.path.exists(path):
                        return False, f"Fayl topilmadi: {path}"
                    img = cv2.imread(path)
                else:
                    # To'liq URL holatida HTTP orqali o‘qish
                    async with aiohttp.ClientSession() as session:
                        async with session.get(image_url) as resp:
                            if resp.status != 200:
                                return False, f"HTTP {resp.status}"
                            img_bytes = await resp.read()
                    nparr = np.frombuffer(img_bytes, np.uint8)
                    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if img is None:
                return False, "Rasmni o‘qib bo‘lmadi"

            # 2) InsightFace bilan yuzlarni olish (GPU)
            faces = face_app.get(img)
            if len(faces) == 0:
                return False, "Yuz topilmadi"

            # Eng ishonchli yuzni tanlaymiz
            best_face = max(faces, key=lambda x: x.det_score)
            if best_face.det_score < 0.4:
                return False, f"Ishonchlilik past: {best_face.det_score:.2f}"

            embedding = best_face.normed_embedding  # 512D

            # 3) DB ga yozish – bitta user + model_version uchun bitta row
            await database_sync_to_async(FaceEncoding.objects.update_or_create)(
                user=user,
                model_version="insightface_buffalo_l",
                defaults={
                    "encoding_data": embedding.tolist(),
                    "confidence": float(best_face.det_score),
                },
            )

            return True, f"Muvaffaqiyatli (conf: {best_face.det_score:.3f})"
        except Exception as e:
            return False, str(e)


# SyncProgressConsumer — progressni har soniyada yangilab turadi
class SyncProgressConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        # Faqat login bo'lgan foydalanuvchiga ruxsat
        if not self.scope["user"].is_authenticated:
            await self.close(code=4001)
            return

        self.user_id = self.scope["user"].id
        self._progress_task: asyncio.Task | None = None

        await self.accept()
        print(f"[PROGRESS WS] Ulanish: user {self.user_id}")

        # 🔥 Cheksiz loopni connect ichida emas, alohida taskda ishga tushiramiz
        self._progress_task = asyncio.create_task(self.progress_loop())

    async def disconnect(self, close_code):
        print(f"[PROGRESS WS] Uzildi: user {self.user_id}, code={close_code}")

        # 🔥 Background taskni toza cancel qilamiz
        if self._progress_task is not None:
            self._progress_task.cancel()
            try:
                await self._progress_task
            except asyncio.CancelledError:
                pass

    async def receive(self, text_data=None, bytes_data=None):
        """
        Agar kerak bo'lsa, kelajakda clientdan xabar qabul qilish uchun.
        Hozircha WS bir yoqlama (faqat server → client), shuning uchun ignore qilamiz.
        """
        return

    async def progress_loop(self):
        """
        Har 1 sekundda foydalanuvchiga progressni yuboradigan background loop.
        Connection yopilganda cancel qilinadi.
        """
        try:
            while True:
                progress = cache.get(
                    f"sync_progress_{self.user_id}",
                    {"percent": 0, "message": "Kutilmoqda..."},
                )
                await self.send(text_data=json.dumps(progress, ensure_ascii=False))
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            # Task bekor qilinsa, sokin chiqib ketamiz
            print(f"[PROGRESS WS] progress_loop cancel qilindi: user {self.user_id}")
        except Exception as e:
            # Debug uchun
            print(f"[PROGRESS WS] progress_loop xato: {e}")

FACE_PROGRESS_KEY = "face_encoding_progress_v1"
FACE_PROGRESS_TOTAL_KEY = "face_encoding_progress_total_v1"


def _percent(processed: int, total: int) -> float:
    if not total:
        return 0.0
    return round((processed / total) * 100.0, 1)


class FaceEncodingProgressConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def disconnect(self, close_code):
        self._running = False
        if hasattr(self, "_task"):
            self._task.cancel()

    @database_sync_to_async
    def _auto_enabled(self) -> bool:
        s = SiteSettings.get_settings()
        return bool(getattr(s, "enable_auto_face_encoding", False))

    @database_sync_to_async
    def _get_total_fallback(self) -> int:
        return int(cache.get(FACE_PROGRESS_TOTAL_KEY) or 0)

    async def _loop(self):
        while self._running:
            enabled = await self._auto_enabled()

            raw = cache.get(FACE_PROGRESS_KEY) or {
                "status": "idle",
                "processed": 0,
                "total": 0,
                "message": "",
                "updated_at": None,
                "started_at": None,
                "run_id": None,
            }

            processed = int(raw.get("processed") or 0)
            total = int(raw.get("total") or 0)

            # fallback
            if total <= 0:
                total = await self._get_total_fallback()
            if total <= 0 and processed > 0:
                total = processed

            payload = {
                "enabled": enabled,
                "status": raw.get("status", "idle"),
                "processed": processed,
                "total": total,
                "percent": _percent(processed, total),
                "message": raw.get("message", "") or "",
                "updated_at": raw.get("updated_at"),
                "started_at": raw.get("started_at"),
                "run_id": raw.get("run_id"),
            }

            try:
                await self.send(text_data=json.dumps(payload))
            except Exception:
                break

            await asyncio.sleep(1)