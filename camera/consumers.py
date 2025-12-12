# camera/consumers.py

import asyncio
import json
import logging
import threading
import time
import urllib.parse
from datetime import timedelta
from pathlib import Path
import shutil

import cv2
import numpy as np
import torch
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from attendance.models import Attendance
from camera.models import Camera
from camera.tasks import process_recognition
from users.models import CustomUser, FaceEncoding

logger = logging.getLogger(__name__)

# ================== GPU / FFMPEG ==================

if torch.cuda.is_available():
    DEVICE = torch.device("cuda")
    try:
        gpu_name = torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001
        gpu_name = "CUDA device"
    logger.info("[GPU] %s (CUDA)", gpu_name)
else:
    DEVICE = torch.device("cpu")
    logger.info("[GPU] CPU")

FFMPEG_BIN = shutil.which("ffmpeg-gpu") or shutil.which("ffmpeg") or "ffmpeg"
logger.info("[FFMPEG] binary: %s", FFMPEG_BIN)

# ================== FAISS & encodings cache ==================

FAISS_AVAILABLE = False

try:
    import faiss  # type: ignore[import]
    FAISS_AVAILABLE = True
    logger.info("[FAISS] yuklandi, CPU IndexFlatIP rejimida ishlaymiz")
except Exception:  # noqa: BLE001
    logger.warning("[FAISS] topilmadi, NumPy matmul bilan ishlaymiz")

ENCODING_TTL_SECONDS = 120  # kesh yashash vaqti

_faiss_lock = threading.Lock()
_faiss_index = None              # faiss.IndexFlatIP yoki np.ndarray
_faiss_users: list[CustomUser] | None = None
_faiss_expires_at: float = 0.0
_faiss_building = False
_FAISS_DIM = 512


def _build_faiss_index_sync():
    """FaceEncoding dan FAISS indexni sinxron ravishda quradi (CPU IndexFlatIP yoki NumPy)."""
    global _faiss_index, _faiss_users, _faiss_expires_at

    qs = (
        FaceEncoding.objects
        .filter(model_version="insightface_buffalo_l")
        .select_related("user")
    )

    embeddings_list: list[np.ndarray] = []
    users: list[CustomUser] = []

    for fe in qs:
        vec = np.array(fe.encoding_data, dtype=np.float32)
        if vec.ndim != 1 or vec.shape[0] != _FAISS_DIM:
            continue

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        embeddings_list.append(vec)
        users.append(fe.user)

    if embeddings_list:
        embeddings = np.stack(embeddings_list, axis=0).astype(np.float32)  # [N x 512]
    else:
        embeddings = np.empty((0, _FAISS_DIM), dtype=np.float32)

    if FAISS_AVAILABLE and embeddings.shape[0] > 0:
        # 🔥 FAISS CPU index (GPU emas!)
        index = faiss.IndexFlatIP(_FAISS_DIM)  # cosine similarity = dot product
        index.add(embeddings)
        _faiss_index = index
        logger.info("[FAISS] CPU index yangilandi: %s embedding", embeddings.shape[0])
    else:
        # fallback: NumPy matritsa
        _faiss_index = embeddings
        if not FAISS_AVAILABLE:
            logger.info(
                "[FAISS] mavjud emas, NumPy matmul keshlandi: %s embedding",
                embeddings.shape[0],
            )

    _faiss_users = users
    _faiss_expires_at = time.monotonic() + ENCODING_TTL_SECONDS

    logger.info(
        "[ENCODING] cache yangilandi: %s foydalanuvchi, TTL=%s s",
        embeddings.shape[0],
        ENCODING_TTL_SECONDS,
    )


def load_encodings_for_search():
    """
    FAISS index (yoki NumPy matritsa) va users ro'yxatini qaytaradi.
    TTL o'tgan bo'lsa, keshni LOCK bilan sinxron yangilaydi.
    """
    now = time.monotonic()

    # Tez yo'l: kesh hali valid bo'lsa, lockga ham kirmaymiz
    if _faiss_index is not None and _faiss_expires_at > now:
        return _faiss_index, _faiss_users or []

    # Aks holda – keshni xavfsiz yangilaymiz
    with _faiss_lock:
        now = time.monotonic()
        if _faiss_index is None or _faiss_expires_at <= now:
            _build_faiss_index_sync()

        return _faiss_index, _faiss_users or []


def faiss_cosine_search(emb: np.ndarray) -> tuple[float, int] | None:
    """
    Bitta yuz embeddingi uchun:
      - Agar FAISS bor bo'lsa → index.search (CPU IndexFlatIP)
      - Bo'lmasa → NumPy matmul
    Natija: (cosine, index) yoki None (topilmadi / xato).
    """
    index, _ = load_encodings_for_search()

    # FAISS mavjud bo'lsa va index faiss.IndexFlatIP bo'lsa
    if FAISS_AVAILABLE and hasattr(index, "search"):
        try:
            v = emb.reshape(1, -1).astype(np.float32)
            if v.shape[1] != _FAISS_DIM:
                return None

            D, I = index.search(v, 1)  # k = 1
            if I.shape[0] == 0 or I[0][0] < 0:
                return None

            return float(D[0][0]), int(I[0][0])
        except Exception as exc:  # noqa: BLE001
            logger.error("[FAISS] search xato: %s", exc)
            return None

    # NumPy fallback: index = embeddings matritsa
    if isinstance(index, np.ndarray) and index.size > 0:
        sims = index @ emb.astype(np.float32)  # [N]
        best_idx = int(np.argmax(sims))
        return float(sims[best_idx]), best_idx

    return None


# ================== YOLO init (hozircha faqat DEVICE log uchun) ==================

FACE_MODEL = None
FACE_WEIGHTS = None

# try:
#     import ultralytics
#     from ultralytics import YOLO
#
#     logger.info("[YOLO] ultralytics=%s", getattr(ultralytics, "__version__", "unknown"))
#
#     BASE_DIR = Path(__file__).resolve().parent.parent
#     MODELS_DIR = BASE_DIR / "models"
#     MODELS_DIR.mkdir(exist_ok=True, parents=True)
#
#     FACE_WEIGHTS = MODELS_DIR / "yolov8n-face.pt"
#
#     if FACE_WEIGHTS.exists():
#         logger.info("[YOLO] FACE model: %s", FACE_WEIGHTS)
#         FACE_MODEL = YOLO(str(FACE_WEIGHTS))
#     else:
#         logger.warning("[YOLO] %s topilmadi, yolov8n.pt bilan ishlaymiz", FACE_WEIGHTS)
#         FACE_MODEL = YOLO("yolov8n.pt")
#
#     FACE_MODEL.to(DEVICE)
#     logger.info("[YOLO] yuklandi, DEVICE=%s", DEVICE.type.upper())
# except Exception as exc:  # noqa: BLE001
#     logger.error("[YOLO] yuklanmadi: %s", exc)
#     FACE_MODEL = None

# ================== InsightFace init ==================

try:
    from insightface.app import FaceAnalysis

    logger.info("[INSIGHTFACE] buffalo_l yuklanmoqda...")
    FACE_APP = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    FACE_APP.prepare(ctx_id=0, det_size=(640, 640))
    logger.info("[INSIGHTFACE] model tayyor (GPU/CPU)")
except Exception as exc:  # noqa: BLE001
    logger.error("[INSIGHTFACE] yuklanmadi: %s", exc)
    FACE_APP = None


# ================== DB helpers ==================

@database_sync_to_async
def get_camera_safe(camera_id: int) -> Camera | None:
    try:
        return Camera.objects.get(
            pk=camera_id,
            is_active=True,
            enable_face_detection=True,
        )
    except Camera.DoesNotExist:
        return None



@database_sync_to_async
def get_live_attendance_data():
    """Oxirgi 1 soatda ko‘ringan userlar (AI dashboard uchun)."""
    now = timezone.now()
    window_start = now - timedelta(hours=1)

    attendances = (
        Attendance.objects
        .filter(date=timezone.localdate(), last_seen__gte=window_start)
        .select_related("user")
        .prefetch_related("photos")
        .order_by("-last_seen")
    )

    result = []

    for att in attendances:
        user = att.user

        photos_qs = att.photos.all().order_by("-captured_at")
        photos = [p.image.url for p in photos_qs[:4] if p.image]
        total_photos = photos_qs.count()
        extra = total_photos - 4 if total_photos > 4 else 0

        visible_id = (
            user.student_id_number
            or user.employee_id_number
            or str(user.id)
        )

        role_display = (
            user.get_role_display()
            if hasattr(user, "get_role_display")
            else "Noma’lum"
        )

        # 🔥 Vaqtni lokalga o‘girib olayapmiz
        entry_local = timezone.localtime(att.entry_time) if att.entry_time else None
        last_local = timezone.localtime(att.last_seen) if att.last_seen else None

        result.append(
            {
                "user": {
                    "id": visible_id,
                    "full_name": user.full_name or user.username,
                    "short_name": user.short_name or user.full_name or user.username,
                    "role": role_display,
                    "role_code": user.role,
                    "department": user.department_name,
                    "group": user.group_name,
                    "position": user.position,
                    "specialty": user.specialty,
                    "photo": (
                        user.image.url
                        if getattr(user, "image", None) and user.image
                        else None
                    ),
                },
                "entry_time": entry_local.strftime("%H:%M") if entry_local else "-",
                "last_seen": last_local.strftime("%H:%M:%S") if last_local else "-",
                # JS dagi timeAgo uchun ham LOCAL ISO yuboramiz:
                "last_seen_iso": last_local.isoformat() if last_local else None,
                "duration_minutes": att.duration_minutes or 0,
                "is_present": att.is_present,
                "photos": photos,
                "extra_count": extra,
            }
        )

    return result

# ================== Live attendance WS ==================

class LiveAttendanceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        await self.accept()
        self.task = asyncio.create_task(self.broadcast_loop())
        logger.info("[ATTENDANCE] client ulandi")

    async def disconnect(self, close_code):
        if hasattr(self, "task"):
            self.task.cancel()
        logger.info("[ATTENDANCE] uzildi → %s", close_code)

    async def broadcast_loop(self):
        try:
            while True:
                users_data = await get_live_attendance_data()
                payload = {
                    "type": "live_attendance",
                    "count": len(users_data),
                    "users": users_data,
                }
                await self.send(text_data=json.dumps(payload, ensure_ascii=False))
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            logger.info("[ATTENDANCE] broadcast bekor qilindi")


# ================== IP camera WS ==================

MIN_FACE_SIZE = 40
RECOGNITION_FRAME_STEP = 1
MAX_FACES_PER_FRAME = 8
FACE_COSINE_THRESHOLD = 0.48


active_ffmpeg_processes: dict[int, asyncio.subprocess.Process] = {}
CAM_METRICS: dict[int, dict] = {}


class IpCameraConsumer(AsyncWebsocketConsumer):
    """
    RTSP → ffmpeg → JPEG kadrlar → InsightFace + FAISS →
    Attendance.update (process_recognition) → brauzerga video.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.camera: Camera | None = None
        self.camera_id: int | None = None
        self._running = False
        self.frame_index = 0
        self.loop: asyncio.AbstractEventLoop | None = None

        self._bytes_sent = 0
        self._frames_sent = 0
        self._metrics_started_at = time.monotonic()
        self._metrics_last_report = self._metrics_started_at
        self._width = 1280
        self._height = 720

    async def connect(self):
        self.loop = asyncio.get_running_loop()

        if FACE_APP is None:
            await self.close(code=1011)
            return

        try:
            self.camera_id = int(self.scope["url_route"]["kwargs"]["camera_id"])
        except (KeyError, TypeError, ValueError):
            await self.close(code=4001)
            return

        await self.accept()

        self.camera = await get_camera_safe(self.camera_id)
        if not self.camera:
            await self.send(
                text_data=json.dumps(
                    {
                        "type": "error",
                        "message": "Kamera topilmadi yoki yuz aniqlash yoqilmagan",
                    },
                    ensure_ascii=False,
                )
            )
            await self.close(code=4003)
            return

        logger.info(
            "[CAM %s] %s ulandi (DEVICE=%s, ffmpeg=%s, FAISS=%s, INSIGHTFACE=%s)",
            self.camera_id,
            self.camera.name or self.camera.ip,
            DEVICE.type.upper(),
            FFMPEG_BIN,
            "ON" if FAISS_AVAILABLE else "OFF",
            "ON" if FACE_APP else "OFF",
        )

        self._running = True
        asyncio.create_task(self.stream_pipeline())

    async def disconnect(self, close_code):
        logger.info("[CAM %s] uzildi → %s", self.camera_id, close_code)
        self._running = False

        proc = active_ffmpeg_processes.pop(self.camera_id, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:  # noqa: BLE001
                pass

        CAM_METRICS.pop(self.camera_id, None)

    def build_best_rtsp_url(self) -> str:
        c = self.camera
        pwd = urllib.parse.quote(c.password or "", safe="")
        user = c.username or "admin"
        candidates = [
            f"rtsp://{user}:{pwd}@{c.ip}:554/cam/realmonitor?channel=1&subtype=0",
            f"rtsp://{user}:{pwd}@{c.ip}:554/Streaming/Channels/101",
            f"rtsp://{user}:{pwd}@{c.ip}:554/live/ch00_0",
            f"rtsp://{user}:{pwd}@{c.ip}:554",
        ]
        return candidates[0]

    def _update_metrics(self, frame_bytes_len: int):
        self._frames_sent += 1
        self._bytes_sent += frame_bytes_len

        now = time.monotonic()
        if now - self._metrics_last_report < 1.0:
            return

        elapsed = now - self._metrics_started_at + 1e-6
        fps = self._frames_sent / elapsed
        bitrate_mbps = (self._bytes_sent * 8.0) / elapsed / (1024 * 1024)

        CAM_METRICS[self.camera_id] = {
            "fps": fps,
            "bitrate_mbps": bitrate_mbps,
            "width": self._width,
            "height": self._height,
            "name": self.camera.name or self.camera.ip,
            "last_update": timezone.now(),
        }

        self._metrics_last_report = now

    async def stream_pipeline(self):
        rtsp_url = self.build_best_rtsp_url()

        cmd = [
            FFMPEG_BIN,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
        ]

        if DEVICE.type == "cuda":
            cmd += ["-hwaccel", "cuda"]

        self._width, self._height = 1920, 1080
        vf_filter = f"scale={self._width}:{self._height}:flags=lanczos"

        cmd += [
            "-i", rtsp_url,
            "-vf", vf_filter,
            "-an",
            "-sn",
            "-vcodec", "mjpeg",
            "-q:v", "3",
            "-f", "image2pipe",
            "pipe:1",
        ]

        logger.info("[FFMPEG CMD][CAM %s] %s", self.camera_id, " ".join(cmd))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            active_ffmpeg_processes[self.camera_id] = process
        except Exception as exc:  # noqa: BLE001
            msg = f"FFmpeg ishga tushmadi: {exc}"
            logger.error("[CAM %s] %s", self.camera_id, msg)
            await self.send(text_data=json.dumps({"type": "error", "message": msg}, ensure_ascii=False))
            await self.close()
            return

        buffer = bytearray()
        sent = 0
        loop = asyncio.get_event_loop()

        try:
            while self._running and process.stdout:
                chunk = await process.stdout.read(32768)
                if not chunk:
                    stderr_left = await process.stderr.read()
                    if stderr_left:
                        logger.error(
                            "[FFMPEG STDERR][CAM %s]: %s",
                            self.camera_id,
                            stderr_left.decode(errors="ignore")[:400],
                        )
                    break

                buffer.extend(chunk)

                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9")
                    if start == -1 or end == -1 or end <= start:
                        break

                    frame_bytes = bytes(buffer[start: end + 2])
                    del buffer[: end + 2]

                    self.frame_index += 1

                    processed = await loop.run_in_executor(
                        None, self.process_frame_with_models, frame_bytes
                    )

                    await self.send(bytes_data=processed)
                    sent += 1
                    self._update_metrics(len(processed))

                    if sent % 100 == 0:
                        logger.info("[CAM %s] %s kadr yuborildi", self.camera_id, sent)

        except Exception as exc:  # noqa: BLE001
            logger.error("[CAM %s] stream xato: %s", self.camera_id, exc)
        finally:
            active_ffmpeg_processes.pop(self.camera_id, None)
            if self._running:
                await self.close()

    # ========== Bitta kadrni qayta ishlash (InsightFace + FAISS) ==========

    def process_frame_with_models(self, jpeg_bytes: bytes) -> bytes:
        if FACE_APP is None:
            return jpeg_bytes

        try:
            arr = np.frombuffer(jpeg_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return jpeg_bytes

            h, w = frame.shape[:2]

            index, users_db = load_encodings_for_search()
            has_db = (FAISS_AVAILABLE and index is not None) or (
                isinstance(index, np.ndarray) and index.size > 0
            )

            do_recognition = has_db and (self.frame_index % RECOGNITION_FRAME_STEP == 0)
            faces = FACE_APP.get(frame)
            total_faces = len(faces)
            faces_processed = 0

            for face in faces:
                x1, y1, x2, y2 = face.bbox.astype(int)
                left = max(int(x1), 0)
                top = max(int(y1), 0)
                right = min(int(x2), w - 1)
                bottom = min(int(y2), h - 1)

                if (bottom - top) < MIN_FACE_SIZE or (right - left) < MIN_FACE_SIZE:
                    continue

                faces_processed += 1
                do_recog_here = do_recognition and faces_processed <= MAX_FACES_PER_FRAME

                label_text = "Yuz"
                conf_text = ""
                user_obj: CustomUser | None = None

                if do_recog_here and has_db:
                    emb = face.normed_embedding.astype(np.float32)
                    res = faiss_cosine_search(emb)
                    if res is not None:
                        best_sim, best_idx = res
                        if 0 <= best_idx < len(users_db) and best_sim >= FACE_COSINE_THRESHOLD:
                            user_obj = users_db[best_idx]
                            name = (
                                user_obj.full_name
                                or user_obj.username
                                or "Foydalanuvchi"
                            ).strip()
                            label_text = name[:22]
                            conf_text = f"{best_sim * 100.0:.1f}%"
                        else:
                            label_text = "Noma'lum"
                            conf_text = f"{best_sim * 100.0:.1f}%"

                if user_obj is not None and self.loop is not None:
                    face_crop = frame[top:bottom, left:right].copy()
                    try:
                        asyncio.run_coroutine_threadsafe(
                            process_recognition(user_obj, face_crop),
                            self.loop,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("[ATTENDANCE ERROR][CAM %s] %s", self.camera_id, exc)

                color_main = (0, 255, 128)
                color_shadow = (0, 128, 255)

                cv2.rectangle(frame, (left - 1, top - 1), (right + 1, bottom + 1), color_shadow, 1)
                cv2.rectangle(frame, (left, top), (right, bottom), color_main, 2)

                label_h = 22
                label_top = max(top - label_h - 4, 0)
                label_bottom = label_top + label_h

                cv2.rectangle(
                    frame,
                    (left, label_top),
                    (right, label_bottom),
                    (10, 10, 10),
                    thickness=-1,
                )

                cv2.putText(
                    frame,
                    label_text,
                    (left + 4, label_bottom - 7),
                    cv2.FONT_HERSHEY_DUPLEX,
                    0.5,
                    color_main,
                    1,
                    lineType=cv2.LINE_AA,
                )

                if conf_text:
                    cy = label_top - 4 if label_top - 4 > 10 else label_bottom + 14
                    cv2.putText(
                        frame,
                        conf_text,
                        (left + 4, cy),
                        cv2.FONT_HERSHEY_DUPLEX,
                        0.4,
                        (200, 200, 200),
                        1,
                        lineType=cv2.LINE_AA,
                    )

            if self.frame_index % 50 == 0:
                logger.info(
                    "[INSIGHTFACE][CAM %s] frame=%s, faces=%s, recog=%s",
                    self.camera_id,
                    self.frame_index,
                    total_faces,
                    "ON" if do_recognition else "OFF",
                )

            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            return encoded.tobytes() if ok else jpeg_bytes

        except Exception as exc:  # noqa: BLE001
            logger.error("[INSIGHTFACE ERROR][CAM %s] %s", self.camera_id, exc)
            return jpeg_bytes
