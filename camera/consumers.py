import asyncio
import json
import logging
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Tuple

# cv2 and numpy are only used in IpCameraConsumer (legacy, not used in production).
# Lazy import inside those methods only — prevents heavy libs from loading in Daphne startup.
cv2 = None
np = None

def _import_cv2():
    global cv2
    if cv2 is None:
        import cv2 as _cv2
        cv2 = _cv2
    return cv2

def _import_np():
    global np
    if np is None:
        import numpy as _np
        np = _np
    return np

try:
    from channels.db import database_sync_to_async
    from channels.generic.websocket import AsyncWebsocketConsumer
    CHANNELS_AVAILABLE = True
except ImportError:
    CHANNELS_AVAILABLE = False
    
from django.utils import timezone

from attendance.models import Attendance
from camera.models import Camera

logger = logging.getLogger(__name__)

# ================== DEVICE ==================
FACE_RUNTIME = None
DEVICE_TYPE = "cuda"
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"

def _get_face_runtime():
    global FACE_RUNTIME, DEVICE_TYPE
    if FACE_RUNTIME is not None:
        return FACE_RUNTIME
    from camera.device import get_face_runtime
    FACE_RUNTIME = get_face_runtime()
    DEVICE_TYPE = FACE_RUNTIME["device_type"]
    return FACE_RUNTIME

# Initialize runtime immediately
_get_face_runtime()

# ================== InsightFace lazy init ==================

FACE_APP = None
_face_app_lock = threading.Lock()

def get_face_app():
    global FACE_APP
    if FACE_APP is not None:
        return FACE_APP
    with _face_app_lock:
        if FACE_APP is not None:
            return FACE_APP
        try:
            from insightface.app import FaceAnalysis
            DET_SIZE = 480
            rt = _get_face_runtime()
            app = FaceAnalysis(
                name="buffalo_l",
                allowed_modules=["detection"],
                providers=rt["providers"],
            )
            app.prepare(ctx_id=rt["ctx_id"], det_size=(DET_SIZE, DET_SIZE))
            FACE_APP = app
            logger.info(
                "[INSIGHTFACE] lazily initialized ready (buffalo_l, device=%s, det_size=%s)",
                DEVICE_TYPE.upper(),
                DET_SIZE,
            )
        except Exception as exc:
            logger.error("[INSIGHTFACE] lazy loading failed: %s", exc)
            FACE_APP = None
    return FACE_APP


# ================== DB helpers ==================

@database_sync_to_async
def get_lesson_topic(schedule_id: int) -> str:
    from camera.models import LessonSchedule
    try:
        sched = LessonSchedule.objects.select_related("subject").get(pk=schedule_id)
        return sched.subject.description or sched.subject.name or ""
    except Exception:
        return ""

@database_sync_to_async
def get_teacher_name(schedule_id: int) -> str:
    from camera.models import LessonSchedule
    try:
        sched = LessonSchedule.objects.get(pk=schedule_id)
        return sched.teacher_name or "O'qituvchi"
    except Exception:
        return "O'qituvchi"

def calculate_fast_similarity(topic: str, text: str) -> int:
    if not topic or not text:
        return 40
    words_t = set(w.lower() for w in topic.split() if len(w) > 3)
    words_s = set(w.lower() for w in text.split() if len(w) > 3)
    if not words_t:
        return 50
    intersection = words_t.intersection(words_s)
    if not intersection:
        # Check partial substring matches
        matches = 0
        for wt in words_t:
            for ws in words_s:
                if wt in ws or ws in wt:
                    matches += 1
                    break
        similarity = matches / len(words_t)
    else:
        similarity = len(intersection) / len(words_t)
        
    score = int(max(40, min(98, 45 + similarity * 120)))
    return score

@database_sync_to_async
def get_camera_safe(camera_id: int) -> Optional[Camera]:
    """
    Faqat is_active tekshiradi.
    enable_face_detection umuman tekshirilmaydi.
    """
    try:
        return Camera.objects.get(pk=camera_id, is_active=True)
    except Camera.DoesNotExist:
        return None


@database_sync_to_async
def get_live_attendance_data():
    """Oxirgi 1 soatda ko‘ringan userlar (dashboard uchun)."""
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
                getattr(user, "student_id_number", None)
                or getattr(user, "employee_id_number", None)
                or str(user.id)
        )

        role_display = (
            user.get_role_display()
            if hasattr(user, "get_role_display")
            else "Noma’lum"
        )

        entry_local = timezone.localtime(att.entry_time) if att.entry_time else None
        last_local = timezone.localtime(att.last_seen) if att.last_seen else None

        result.append(
            {
                "user": {
                    "id": visible_id,
                    "full_name": user.full_name or user.username,
                    "short_name": getattr(user, "short_name", None) or user.full_name or user.username,
                    "role": role_display,
                    "role_code": getattr(user, "role", None),
                    "department": getattr(user, "department_name", None),
                    "group": getattr(user, "group_name", None),
                    "position": getattr(user, "position", None),
                    "specialty": getattr(user, "specialty", None),
                    "photo": (user.image.url if getattr(user, "image", None) and user.image else None),
                },
                "entry_time": entry_local.strftime("%H:%M") if entry_local else "-",
                "last_seen": last_local.strftime("%H:%M:%S") if last_local else "-",
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
        # Join live_attendance_events channels group
        await self.channel_layer.group_add(
            "live_attendance_events",
            self.channel_name
        )
        self.task = asyncio.create_task(self.broadcast_loop())
        logger.info("[ATTENDANCE] client ulandi")

    async def disconnect(self, close_code):
        # Leave live_attendance_events channels group
        await self.channel_layer.group_discard(
            "live_attendance_events",
            self.channel_name
        )
        if hasattr(self, "task"):
            self.task.cancel()
        logger.info("[ATTENDANCE] uzildi → %s", close_code)

    async def broadcast_loop(self):
        try:
            while True:
                users_data = await get_live_attendance_data()
                payload = {"type": "live_attendance", "count": len(users_data), "users": users_data}
                await self.send(text_data=json.dumps(payload, ensure_ascii=False))
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            logger.info("[ATTENDANCE] broadcast bekor qilindi")

    async def face_detected_event(self, event):
        payload = {
            "type": "face_detected",
            "camera_id": event.get("camera_id"),
            "user_id": event.get("user_id"),
            "full_name": event.get("full_name"),
            "photo_url": event.get("photo_url"),
            "role": event.get("role"),
            "entry_time": event.get("entry_time"),
            "last_seen_iso": event.get("last_seen_iso"),
            "is_present": event.get("is_present", True)
        }
        await self.send(text_data=json.dumps(payload, ensure_ascii=False))


# ================== IP camera WS ==================

MIN_FACE_SIZE = 40


@dataclass
class CamStats:
    started_at: float
    last_report: float
    bytes_sent: int = 0
    frames_sent: int = 0
    frames_decoded: int = 0
    faces_seen: int = 0
    faces_boxed: int = 0
    last_faces: int = 0


active_ffmpeg_processes: dict[int, asyncio.subprocess.Process] = {}


class IpCameraConsumer(AsyncWebsocketConsumer):
    """
    RTSP -> ffmpeg -> MJPEG pipe -> decode -> face box -> send to browser
    Eslatma:
      - Recognition/Attendance update/Photo save YO'Q (buni camera_daemon qiladi)
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.camera: Optional[Camera] = None
        self.camera_id: Optional[int] = None
        self._running = False
        self._proc: Optional[asyncio.subprocess.Process] = None

        self.width = 960
        self.height = 540
        self.target_fps = 20
        self.jpeg_q = 4

        self.stats = CamStats(started_at=time.monotonic(), last_report=time.monotonic())
        self._last_face_log = 0.0
        self.ai_enabled = True  # Default
        self._is_processing = False  # Flag to handle backpressure and drop frames if server is busy

    async def connect(self):
        if get_face_app() is None:
            await self.close(code=1011)
            return

        try:
            self.camera_id = int(self.scope["url_route"]["kwargs"]["camera_id"])
        except (KeyError, TypeError, ValueError):
            await self.close(code=4001)
            return

        # Query params: ?ai=1 yoki ?ai=0
        qs = self.scope.get("query_string", b"").decode("utf-8")
        params = urllib.parse.parse_qs(qs)
        self.schedule_id = None
        if "schedule_id" in params:
            try:
                self.schedule_id = int(params["schedule_id"][0])
            except ValueError:
                self.schedule_id = None
                
        if "ai" in params:
            val = params["ai"][0]
            self.ai_enabled = (val == "1" or val.lower() == "true")

        if not self.ai_enabled:
            # Low-latency live preview optimization
            self.width = 800
            self.height = 450
            self.target_fps = 20
            self.jpeg_q = 14
        else:
            self.width = 800
            self.height = 450
            self.target_fps = 20
            self.jpeg_q = 14

        if self.schedule_id:
            self.lesson_topic = await get_lesson_topic(self.schedule_id)
            self.teacher_name = await get_teacher_name(self.schedule_id)
            logger.info("[CAM %s] Loaded Schedule %s: teacher=%s, topic=%s", self.camera_id, self.schedule_id, self.teacher_name, self.lesson_topic)
        else:
            self.lesson_topic = ""
            self.teacher_name = ""

        await self.accept()

        if self.camera_id == 0:
            self.camera = Camera(id=0, name="USB Kamera", ip="127.0.0.1", is_active=True)
            self._running = True
            logger.info("[CAM 0] Connected Virtual USB Webcam client.")
            return

        self.camera = await get_camera_safe(self.camera_id)
        if not self.camera:
            await self.send(text_data=json.dumps({"type": "error", "message": "Kamera topilmadi yoki faol emas"},
                                                 ensure_ascii=False))
            await self.close(code=4003)
            return

        logger.info(
            "[CAM %s] connected name=%s ip=%s DEVICE=%s ffmpeg=%s AI=%s RES=%sx%s FPS=%s JPEG_Q=%s",
            self.camera_id,
            self.camera.name or "-",
            self.camera.ip,
            DEVICE_TYPE.upper(),
            FFMPEG_BIN,
            self.ai_enabled,
            self.width,
            self.height,
            self.target_fps,
            self.jpeg_q,
        )

        self._running = True
        asyncio.create_task(self.stream_pipeline())

    async def disconnect(self, close_code):
        logger.info("[CAM %s] disconnected code=%s", self.camera_id, close_code)
        self._running = False
        await self._stop_ffmpeg()

    async def receive(self, bytes_data=None, text_data=None):
        if bytes_data and self._running:
            # Server-side backpressure management: immediately drop incoming frames if already processing
            if getattr(self, '_is_processing', False):
                return
            
            self._is_processing = True
            try:
                loop = asyncio.get_running_loop()
                processed_result = await loop.run_in_executor(
                    None, self.process_frame_with_models, bytes_data
                )
                if processed_result and self._running:
                    processed, recognized_people = processed_result
                    if processed:
                        await self.send(bytes_data=processed)
                        for person in recognized_people:
                            await self.send(text_data=json.dumps({
                                "type": "face_recognized",
                                "user_id": person["user_id"],
                                "full_name": person["full_name"],
                                "photo_url": person["photo_url"],
                                "role": person["role"]
                            }, ensure_ascii=False))
            finally:
                self._is_processing = False

    async def _stop_ffmpeg(self):
        cam_id = self.camera_id
        proc = self._proc or (active_ffmpeg_processes.pop(cam_id, None) if cam_id else None)

        if not proc:
            return

        try:
            if isinstance(proc, asyncio.subprocess.Process):
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            else:
                # Sync process (subprocess.Popen)
                if proc.poll() is None:
                    proc.terminate()
                    loop = asyncio.get_running_loop()
                    try:
                        await loop.run_in_executor(None, lambda: proc.wait(timeout=1.5))
                    except Exception:
                        proc.kill()
                        await loop.run_in_executor(None, proc.wait)
        except (ProcessLookupError, OSError):
            pass
        except Exception as exc:
            logger.warning("[CAM %s] error stopping process: %s", cam_id, exc)

        if cam_id in active_ffmpeg_processes:
            active_ffmpeg_processes.pop(cam_id, None)

        self._proc = None

    # ---------- RTSP URL candidates ----------

    def build_rtsp_candidates(self) -> list[str]:
        c = self.camera
        pwd = urllib.parse.quote(c.password or "", safe="")
        user = c.username or "admin"

        urls = []
        if c.rtsp_url:
            urls.append(c.rtsp_url)

        # UI uchun avval odatiy Hikvision main/sub oqimlar, keyin generic variantlar.
        urls.extend([
            f"rtsp://{user}:{pwd}@{c.ip}:554/Streaming/Channels/101",  # main
            f"rtsp://{user}:{pwd}@{c.ip}:554/Streaming/Channels/102",  # sub
            f"rtsp://{user}:{pwd}@{c.ip}:554/Streaming/Channels/103",  # third (ko'p Hikvision)
            f"rtsp://{user}:{pwd}@{c.ip}:554/cam/realmonitor?channel=1&subtype=0",  # main alt
            f"rtsp://{user}:{pwd}@{c.ip}:554/cam/realmonitor?channel=1&subtype=1",  # sub alt
            f"rtsp://{user}:{pwd}@{c.ip}:554/live/ch00_0",
            f"rtsp://{user}:{pwd}@{c.ip}:554/live/ch00_1",
            f"rtsp://{user}:{pwd}@{c.ip}:554",  # last resort
        ])
        return list(dict.fromkeys(urls))

    # ---------- ffmpeg command ----------

    def build_ffmpeg_cmd(self, rtsp_url: str) -> list[str]:
        cmd = [
            FFMPEG_BIN,
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-rtsp_flags", "prefer_tcp",
            "-fflags", "+nobuffer+discardcorrupt+genpts",
            "-flags", "low_delay",
            "-err_detect", "ignore_err",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-max_delay", "0",
            "-reorder_queue_size", "0",
            "-avioflags", "direct",
        ]

        # GPU decode: ON when CUDA available
        if DEVICE_TYPE == "cuda":
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

        cmd += ["-i", rtsp_url, "-an", "-sn"]

        # Scale filter: use GPU-native scale_cuda when CUDA is active, else CPU scale
        if DEVICE_TYPE == "cuda":
            cmd += [
                "-vf", f"fps={self.target_fps},scale_cuda={self.width}:{self.height},hwdownload,format=nv12",
                "-vcodec", "mjpeg",
                "-q:v", str(self.jpeg_q),
                "-f", "mjpeg",
                "pipe:1",
            ]
        else:
            cmd += [
                "-vf", f"fps={self.target_fps},scale={self.width}:{self.height}:flags=bicubic",
                "-vcodec", "mjpeg",
                "-q:v", str(self.jpeg_q),
                "-f", "mjpeg",
                "pipe:1",
            ]
        return cmd

    # ---------- robust JPEG extractor ----------

    def _extract_jpegs_from_buffer(self, buf: bytearray) -> list[bytes]:
        frames: list[bytes] = []
        while True:
            soi = buf.find(b"\xff\xd8")
            if soi < 0:
                if len(buf) > 2_000_000:
                    buf.clear()
                break

            if soi > 0:
                del buf[:soi]

            eoi = buf.find(b"\xff\xd9", 2)
            if eoi < 0:
                if len(buf) > 5_000_000:
                    last_soi = buf.rfind(b"\xff\xd8")
                    if last_soi > 0:
                        del buf[:last_soi]
                    else:
                        buf.clear()
                break

            frame = bytes(buf[:eoi + 2])
            del buf[:eoi + 2]

            if len(frame) < 5_000:
                continue

            frames.append(frame)
            if len(frames) >= 5:
                break
        return frames

    # ---------- metrics/logging ----------

    def _bump_metrics(self, sent_bytes: int):
        self.stats.frames_sent += 1
        self.stats.bytes_sent += sent_bytes

    def _report_stats_if_needed(self, cam_id: int, hwaccel_on: bool):
        now = time.monotonic()
        if now - self.stats.last_report < 5.0:
            return
        elapsed = (now - self.stats.started_at) + 1e-6

        fps_dec = self.stats.frames_decoded / elapsed
        fps_sent = self.stats.frames_sent / elapsed
        mbps = (self.stats.bytes_sent * 8.0) / elapsed / (1024 * 1024)

        logger.info(
            "[CAM %s][STATS] hwaccel=%s res=%sx%s fps_dec=%.1f fps_sent=%.1f mbps=%.2f faces=%s boxed=%s",
            cam_id,
            "ON" if hwaccel_on else "OFF",
            self.width,
            self.height,
            fps_dec,
            fps_sent,
            mbps,
            self.stats.faces_seen,
            self.stats.faces_boxed,
        )
        self.stats = CamStats(started_at=now, last_report=now)

    # ---------- main loop ----------

    async def stream_pipeline(self):
        import subprocess
        import queue
        import threading
        from django.core.cache import cache

        cam_id = self.camera_id or 0
        candidates = self.build_rtsp_candidates()
        loop = asyncio.get_running_loop()

        # Direct GPU FFmpeg pipeline with AI Face Detection
        while self._running:
            last_cache_update = 0.0
            for rtsp_url in candidates:
                if not self._running:
                    break

                logger.info("[CAM %s] ffmpeg start url=%s ffmpeg_bin=%s", cam_id, rtsp_url, FFMPEG_BIN)
                try:
                    cmd = self.build_ffmpeg_cmd(rtsp_url)
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                    )
                    self._proc = proc
                    active_ffmpeg_processes[cam_id] = proc
                except Exception as exc:
                    logger.warning("[CAM %s] ffmpeg spawn failed url=%s err=%s (type=%s)", cam_id, rtsp_url, exc, type(exc).__name__)
                    await asyncio.sleep(0.5)
                    continue

                # Stream o'qish — threadda stdout o'qib, queue orqali async loopga uzatamiz
                frame_queue = queue.Queue(maxsize=2)
                stop_event = threading.Event()

                def _reader_thread(p, q, stop_ev):
                    """FFmpeg stdout'dan JPEG kadrlarni o'qib, GPU'da ramkaga olib queue'ga qo'yadi."""
                    buf = bytearray()
                    try:
                        while not stop_ev.is_set():
                            chunk = p.stdout.read(65536)
                            if not chunk:
                                break
                            buf.extend(chunk)

                            # JPEG kadrlarni ajratib olish
                            while True:
                                soi = buf.find(b"\xff\xd8")
                                if soi < 0:
                                    if len(buf) > 1_000_000:
                                        buf.clear()
                                    break
                                if soi > 0:
                                    del buf[:soi]
                                eoi = buf.find(b"\xff\xd9", 2)
                                if eoi < 0:
                                    if len(buf) > 3_000_000:
                                        buf.clear()
                                    break
                                frame_bytes = bytes(buf[:eoi + 2])
                                del buf[:eoi + 2]
                                if len(frame_bytes) < 3_000:
                                    continue

                                # Agar AI yoqilgan bo'lsa, to'g'ridan-to'g'ri shu threadda GPU'da ishlaydi (run_in_executor yo'q!)
                                if self.ai_enabled:
                                    try:
                                        processed_result = self.process_frame_with_models(frame_bytes)
                                        final_frame = processed_result[0] if (processed_result and processed_result[0]) else frame_bytes
                                    except Exception:
                                        final_frame = frame_bytes
                                else:
                                    final_frame = frame_bytes

                                # Queue'ga qo'yish (eski kadrni tashlab yuboradi)
                                try:
                                    q.put_nowait(final_frame)
                                except queue.Full:
                                    try:
                                        q.get_nowait()
                                    except queue.Empty:
                                        pass
                                    q.put_nowait(final_frame)
                    except Exception:
                        pass
                    finally:
                        q.put(None)  # sentinel

                reader = threading.Thread(
                    target=_reader_thread,
                    args=(proc, frame_queue, stop_event),
                    daemon=True,
                )
                reader.start()

                got_any_frame = False
                try:
                    self.stats = CamStats(started_at=time.monotonic(), last_report=time.monotonic())

                    while self._running:
                        # Periodically refresh cache (every 5 seconds)
                        now = time.monotonic()
                        if now - last_cache_update > 5.0:
                            cache.set(f"camera_stream_source_{cam_id}", "ffmpeg", timeout=15)
                            last_cache_update = now

                        # Birinchi frame uchun kattaroq timeout
                        get_timeout = 10.0 if not got_any_frame else 3.0
                        try:
                            frame_bytes = await asyncio.wait_for(
                                loop.run_in_executor(None, lambda t=get_timeout: frame_queue.get(timeout=t)),
                                timeout=get_timeout + 2.0,
                            )
                        except (asyncio.TimeoutError, Exception) as exc:
                            if not got_any_frame:
                                logger.warning("[CAM %s] no frames from url=%s (timeout, exc=%s)", cam_id, rtsp_url, type(exc).__name__)
                            break

                        if frame_bytes is None:
                            break

                        got_any_frame = True
                        self.stats.frames_decoded += 1

                        await self.send(bytes_data=frame_bytes)
                        self._bump_metrics(len(frame_bytes))
                        self._report_stats_if_needed(cam_id, DEVICE_TYPE == "cuda")

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("[CAM %s] stream loop error url=%s: %s", cam_id, rtsp_url, exc)
                finally:
                    stop_event.set()
                    await loop.run_in_executor(None, self._stop_ffmpeg_sync)
                    reader.join(timeout=3.0)

                # Agar birinchi URL muvaffaqiyatli ishlagan bo'lsa, boshqasini sinashga hojat yo'q
                if got_any_frame:
                    break

                if self._running:
                    await asyncio.sleep(0.4)

            # Barcha candidate'lar yiqilsa, qisqa pauzadan keyin qayta sinaymiz.
            if not got_any_frame and self._running:
                try:
                    await self.send(text_data=json.dumps({
                        "type": "camera_error",
                        "message": "Kameradan tasvir oqimi olinmadi (RTSP ulanish xatosi 500 yoki tarmoq faol emas)."
                    }, ensure_ascii=False))
                except Exception:
                    pass

            if self._running:
                await asyncio.sleep(1.5)

    def _stop_ffmpeg_sync(self):
        """Sinxron ffmpeg to'xtatish (threaddan chaqiriladi)."""
        cam_id = self.camera_id
        proc = self._proc

        if not proc:
            return

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.5)
                except Exception:
                    proc.kill()
                    proc.wait()
        except (ProcessLookupError, OSError):
            pass

        if cam_id in active_ffmpeg_processes:
            active_ffmpeg_processes.pop(cam_id, None)

        self._proc = None

    # ---------- per-frame processing ----------

    def process_frame_with_models(self, jpeg_bytes: bytes) -> Tuple[Optional[bytes], list]:
        face_app = get_face_app()
        if not self.ai_enabled or face_app is None:
            return jpeg_bytes, []

        try:
            _np = _import_np()
            _cv2 = _import_cv2()
            arr = _np.frombuffer(jpeg_bytes, _np.uint8)
            frame = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
            if frame is None:
                return None, []

            h, w = frame.shape[:2]

            # 1. Pure GPU Face Detection (Ultra-fast det_10g on CUDA in <4ms)
            bboxes, _ = face_app.det_model.detect(frame, max_num=0, metric='default')
            if bboxes is not None and len(bboxes) > 0:
                self.stats.faces_seen += len(bboxes)
                self.stats.faces_boxed += len(bboxes)

                # 2. Draw sleek Cyber HUD bounding boxes around detected faces
                for box in bboxes:
                    conf = float(box[4])
                    if conf < 0.5:
                        continue
                    x1 = max(0, int(box[0]))
                    y1 = max(0, int(box[1]))
                    x2 = min(w - 1, int(box[2]))
                    y2 = min(h - 1, int(box[3]))
                    
                    # Main bounding box (Emerald Green)
                    _cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 127), 2)
                    
                    # Cyber Corner Accents (Bright Cyan)
                    cl_w = max(8, int((x2 - x1) * 0.15))
                    cl_h = max(8, int((y2 - y1) * 0.15))
                    # Top-Left
                    _cv2.line(frame, (x1, y1), (x1 + cl_w, y1), (0, 255, 255), 3)
                    _cv2.line(frame, (x1, y1), (x1, y1 + cl_h), (0, 255, 255), 3)
                    # Top-Right
                    _cv2.line(frame, (x2, y1), (x2 - cl_w, y1), (0, 255, 255), 3)
                    _cv2.line(frame, (x2, y1), (x2, y1 + cl_h), (0, 255, 255), 3)
                    # Bottom-Left
                    _cv2.line(frame, (x1, y2), (x1 + cl_w, y2), (0, 255, 255), 3)
                    _cv2.line(frame, (x1, y2), (x1, y2 - cl_h), (0, 255, 255), 3)
                    # Bottom-Right
                    _cv2.line(frame, (x2, y2), (x2 - cl_w, y2), (0, 255, 255), 3)
                    _cv2.line(frame, (x2, y2), (x2, y2 - cl_h), (0, 255, 255), 3)

                    # Confidence label tag
                    label = f"FACE {int(conf * 100)}%"
                    label_h = 18
                    label_top = max(y1 - label_h - 2, 0)
                    _cv2.rectangle(frame, (x1, label_top), (x1 + 80, label_top + label_h), (15, 20, 30), -1)
                    _cv2.putText(
                        frame,
                        label,
                        (x1 + 4, label_top + 13),
                        _cv2.FONT_HERSHEY_DUPLEX,
                        0.4,
                        (0, 255, 127),
                        1,
                        lineType=_cv2.LINE_AA,
                    )

            ok, encoded = _cv2.imencode(".jpg", frame, [int(_cv2.IMWRITE_JPEG_QUALITY), 50])
            return (encoded.tobytes() if ok else None), []

        except Exception as e:
            logger.error("[CONSUMER] Frame processing main exception: %s", e)
            return None, []


class Go2RtcProxyConsumer(AsyncWebsocketConsumer):
    """
    go2rtc WebSocket proxy:
    Frontend (WebRTC / MSE / video-rtc.js) <-> Daphne WebSocket <-> go2rtc (:1984/api/ws?src=...)
    Provides direct native zero-copy RTSP streaming with zero CPU usage.
    """
    async def connect(self):
        self.stream_name = self.scope['url_route']['kwargs'].get('stream_name')
        self.upstream_ws = None
        self.is_running = True
        self.send_queue = asyncio.Queue(maxsize=100)
        self.writer_task = None
        self.reader_task = None

        await self.accept()

        go2rtc_ws_url = f"ws://127.0.0.1:1984/api/ws?src={urllib.parse.quote(self.stream_name)}"

        async def upstream_reader():
            try:
                import websockets
                async with websockets.connect(
                    go2rtc_ws_url,
                    open_timeout=4.0,
                    max_size=10 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self.upstream_ws = ws

                    async def upstream_writer():
                        try:
                            while self.is_running:
                                item = await self.send_queue.get()
                                if item is None:
                                    break
                                is_bytes, data = item
                                if is_bytes:
                                    await ws.send(data)
                                else:
                                    await ws.send(data)
                        except (asyncio.CancelledError, Exception):
                            pass

                    self.writer_task = asyncio.create_task(upstream_writer())

                    while self.is_running:
                        try:
                            msg = await ws.recv()
                            if isinstance(msg, bytes):
                                await self.send(bytes_data=msg)
                            else:
                                await self.send(text_data=msg)
                        except (asyncio.CancelledError, Exception):
                            break
            except Exception as e:
                logger.debug("[go2rtc WS Proxy] %s: %s", self.stream_name, e)
            finally:
                self.is_running = False
                if self.writer_task and not self.writer_task.done():
                    self.writer_task.cancel()
                try:
                    await self.close()
                except Exception:
                    pass

        self.reader_task = asyncio.create_task(upstream_reader())

    async def disconnect(self, close_code):
        self.is_running = False
        if self.writer_task and not self.writer_task.done():
            self.writer_task.cancel()
        if hasattr(self, 'reader_task') and self.reader_task and not self.reader_task.done():
            self.reader_task.cancel()
        if self.upstream_ws:
            try:
                await self.upstream_ws.close()
            except Exception:
                pass

    async def receive(self, text_data=None, bytes_data=None):
        if self.is_running:
            try:
                if bytes_data is not None:
                    self.send_queue.put_nowait((True, bytes_data))
                elif text_data is not None:
                    self.send_queue.put_nowait((False, text_data))
            except asyncio.QueueFull:
                pass

