import asyncio
import json
import logging
import shutil
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import cv2
import numpy as np

try:
    from channels.db import database_sync_to_async
    from channels.generic.websocket import AsyncWebsocketConsumer
    CHANNELS_AVAILABLE = True
except ImportError:
    CHANNELS_AVAILABLE = False
    
from django.utils import timezone

from attendance.models import Attendance
from camera.device import get_face_runtime
from camera.models import Camera

logger = logging.getLogger(__name__)

# ================== DEVICE ==================

FACE_RUNTIME = get_face_runtime()
DEVICE_TYPE = FACE_RUNTIME["device_type"]
logger.info(
    "[DEVICE] requested=%s resolved=%s providers=%s",
    FACE_RUNTIME["requested"],
    DEVICE_TYPE.upper(),
    FACE_RUNTIME["providers"],
)

FFMPEG_BIN = shutil.which("ffmpeg") or r"C:\Users\Izzatbek\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"
logger.info("[FFMPEG] binary: %s", FFMPEG_BIN)

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
            DET_SIZE = 640
            app = FaceAnalysis(
                name="buffalo_l",
                providers=FACE_RUNTIME["providers"],
            )
            app.prepare(ctx_id=FACE_RUNTIME["ctx_id"], det_size=(DET_SIZE, DET_SIZE))
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

        self.width = 1280
        self.height = 720
        self.target_fps = 12
        self.jpeg_q = 8

        self.stats = CamStats(started_at=time.monotonic(), last_report=time.monotonic())
        self._last_face_log = 0.0
        self.ai_enabled = True  # Default

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
        if "ai" in params:
            val = params["ai"][0]
            self.ai_enabled = (val == "1" or val.lower() == "true")

        await self.accept()

        self.camera = await get_camera_safe(self.camera_id)
        if not self.camera:
            await self.send(text_data=json.dumps({"type": "error", "message": "Kamera topilmadi yoki faol emas"},
                                                 ensure_ascii=False))
            await self.close(code=4003)
            return

        logger.info(
            "[CAM %s] connected name=%s ip=%s DEVICE=%s ffmpeg=%s AI=%s",
            self.camera_id,
            self.camera.name or "-",
            self.camera.ip,
            DEVICE_TYPE.upper(),
            FFMPEG_BIN,
            self.ai_enabled,
        )

        self._running = True
        asyncio.create_task(self.stream_pipeline())

    async def disconnect(self, close_code):
        logger.info("[CAM %s] disconnected code=%s", self.camera_id, close_code)
        self._running = False
        await self._stop_ffmpeg()

    async def _stop_ffmpeg(self):
        cam_id = self.camera_id
        proc = self._proc or (active_ffmpeg_processes.pop(cam_id, None) if cam_id else None)

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
        except Exception:
            pass

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
            "-analyzeduration", "1000000",
            "-probesize", "1000000",
        ]

        # GPU decode OOM bo'lishi mumkin: UI uchun ko'p kamera ochilsa GPU decode og'irlashadi.
        # Shuning uchun default: GPU decode ON bo'lsa ham, muammo bo'lsa kameraning o'zi CPU decodega tushadi.
        if DEVICE_TYPE == "cuda":
            cmd += ["-hwaccel", "cuda"]

        cmd += [
            "-i", rtsp_url,
            "-an", "-sn",
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
        # reset per-window counters (statsni 5s oynada ko'rsatamiz)
        self.stats = CamStats(started_at=now, last_report=now)

    # ---------- main loop ----------

    async def stream_pipeline(self):
        import subprocess
        import queue
        import threading

        cam_id = self.camera_id or 0
        candidates = self.build_rtsp_candidates()
        loop = asyncio.get_running_loop()

        while self._running:
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
                frame_queue = queue.Queue(maxsize=4)
                stop_event = threading.Event()

                def _reader_thread(p, q, stop_ev):
                    """FFmpeg stdout'dan JPEG kadrlarni o'qib queue'ga qo'yadi."""
                    buf = bytearray()
                    try:
                        while not stop_ev.is_set():
                            chunk = p.stdout.read(32768)
                            if not chunk:
                                break
                            buf.extend(chunk)

                            # JPEG kadrlarni ajratib olish
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
                                        buf.clear()
                                    break
                                frame = bytes(buf[:eoi + 2])
                                del buf[:eoi + 2]
                                if len(frame) < 5_000:
                                    continue
                                # Queue'ga qo'yish
                                try:
                                    q.put_nowait(frame)
                                except queue.Full:
                                    try:
                                        q.get_nowait()
                                    except queue.Empty:
                                        pass
                                    q.put_nowait(frame)
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
                            # FFmpeg tugadi
                            err_text = ""
                            try:
                                err_data = proc.stderr.read()
                                err_text = err_data.decode(errors="ignore").strip() if err_data else ""
                            except Exception:
                                pass
                            logger.warning("[CAM %s] ffmpeg ended url=%s err=%s", cam_id, rtsp_url, err_text[:500])
                            break

                        got_any_frame = True
                        self.stats.frames_decoded += 1

                        processed = await loop.run_in_executor(
                            None, self.process_frame_with_models, frame_bytes
                        )
                        if not processed:
                            continue

                        await self.send(bytes_data=processed)
                        self._bump_metrics(len(processed))
                        self._report_stats_if_needed(cam_id, False)

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
            if self._running:
                await asyncio.sleep(1.0)

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

    def process_frame_with_models(self, jpeg_bytes: bytes) -> Optional[bytes]:
        face_app = get_face_app()
        # Agar AI o'chiq bo'lsa yoki FaceApp yo'q bo'lsa -> shunchaki frame qaytaramiz (CPU tejash)
        if not self.ai_enabled or face_app is None:
            return jpeg_bytes

        try:
            arr = np.frombuffer(jpeg_bytes, np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return None

            # InsightFace ko'pincha BGR ham ishlaydi, lekin ayrim holatda RGB yaxshoreq bo'ladi.
            # Agar sizda det yomon bo'lsa, quyidagi 2 qatorni ishlating:
            # frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            # faces = FACE_APP.get(frame_rgb)
            faces = face_app.get(frame)

            self.stats.faces_seen += len(faces)
            if len(faces) > 0:
                now = time.monotonic()
                if now - self._last_face_log > 2.0:
                    logger.info("[CAM %s] ODAM: faces=%s", self.camera_id, len(faces))
                    self._last_face_log = now

            h, w = frame.shape[:2]
            color_main = (0, 255, 128)

            boxed = 0
            for face in faces:
                x1, y1, x2, y2 = face.bbox.astype(int)

                left = max(int(x1), 0)
                top = max(int(y1), 0)
                right = min(int(x2), w - 1)
                bottom = min(int(y2), h - 1)

                if (bottom - top) < MIN_FACE_SIZE or (right - left) < MIN_FACE_SIZE:
                    continue

                boxed += 1

                cv2.rectangle(frame, (left, top), (right, bottom), color_main, 2)

                label_text = "Yuz"
                label_h = 22
                label_top = max(top - label_h - 4, 0)
                label_bottom = label_top + label_h

                cv2.rectangle(frame, (left, label_top), (right, label_bottom), (10, 10, 10), thickness=-1)
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

            self.stats.faces_boxed += boxed

            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return encoded.tobytes() if ok else None

        except Exception:
            return None
