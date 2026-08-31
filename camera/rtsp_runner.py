# camera/rtsp_runner
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import signal
import time
import urllib.parse
import threading
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import torch
from asgiref.sync import sync_to_async

from camera.device import get_face_runtime
from camera.models import Camera
from camera.recognition import (
    auto_exit_detector_loop,
    process_recognition_sync,
    recognize_user_from_embedding,
    warmup_encodings_cache,
)
from camera.hikvision import enqueue_hikvision_event

try:
    from channels.layers import get_channel_layer
    CHANNELS_AVAILABLE = True
except ImportError:
    CHANNELS_AVAILABLE = False

logger = logging.getLogger(__name__)

# =========================
# CONFIG (stable)
# =========================

import os
STREAM_W, STREAM_H = 1280, 720
JPEG_QV = 4               # quality 2..10 (lower = better quality)
RECOG_FRAME_STEP = 1
MAX_FACES_PER_FRAME = 6
MAX_DETECTION_FPS = float(os.getenv("CAMERA_MAX_DETECTION_FPS", "6.0"))

MAX_BUFFER_BYTES = 12_000_000
READ_TIMEOUT_S = 4.0      # ffmpeg stdout.read timeout
CAMERA_HEARTBEAT_S = 25.0 # per-camera: agar video kelmasa restart
STATS_EVERY_S = 10.0

RECONNECT_BASE = 0.8
RECONNECT_MAX = 20.0
RECONNECT_JITTER = 0.30

# =========================
# FFMPEG / DEVICE
# =========================

FACE_RUNTIME = get_face_runtime()
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
DEVICE = torch.device("cuda" if FACE_RUNTIME["device_type"] == "cuda" and torch.cuda.is_available() else "cpu")
logger.info(
    "[RTSP] FFMPEG=%s requested=%s resolved=%s providers=%s",
    FFMPEG_BIN,
    FACE_RUNTIME["requested"],
    DEVICE.type.upper(),
    FACE_RUNTIME["providers"],
)

# =========================
# InsightFace init (once)
# =========================
# InsightFace (Disabled: on-device ISUP face recognition is used)
# =========================

FACE_APPS = []

def detect_faces_sync(frame) -> list:
    return []

def check_active_lesson_sync(camera_id: int) -> bool:
    """
    Checks if there is an active lesson schedule currently running for the camera's auditorium.
    """
    try:
        from camera.models import LessonSchedule
        from django.utils import timezone
        now_dt = timezone.localtime()
        weekday = now_dt.isoweekday()
        if weekday == 7:  # Sunday, no lessons
            return False
        time_now = now_dt.time()
        return LessonSchedule.objects.filter(
            auditorium__camera_id=camera_id,
            weekday=weekday,
            lesson_pair__start_time__lte=time_now,
            lesson_pair__end_time__gte=time_now
        ).exists()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("[RTSP] check_active_lesson_sync failed: %s", e)
        return False

# =========================
# DB helpers
# =========================

@sync_to_async
def fetch_active_cameras() -> list[Camera]:
    from django.db.models import Q
    return list(
        Camera.objects.filter(
            Q(enable_face_detection=True) | Q(enable_infraction_detection=True),
            is_active=True
        ).order_by("id")
    )


def delegate_save_infraction_to_celery(camera_id: int, infraction_type: str, confidence: float, frame_with_box: np.ndarray, offender_name: str, video_frames: list, bbox: list) -> None:
    import base64
    import tempfile
    import os
    from camera.tasks import save_infraction_task

    try:
        # 1. Base64 encode the snapshot image
        snapshot_b64 = ""
        ok, buffer = cv2.imencode(".jpg", frame_with_box, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if ok:
            snapshot_b64 = base64.b64encode(buffer.tobytes()).decode('utf-8')

        # 2. Write video frames to a temporary directory as JPEGs
        temp_dir = ""
        if video_frames and len(video_frames) > 0:
            temp_dir = tempfile.mkdtemp(prefix="infraction_frames_")
            for idx, frame in enumerate(video_frames):
                frame_path = os.path.join(temp_dir, f"frame_{idx:04d}.jpg")
                cv2.imwrite(frame_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])

        # 3. Call Celery task
        save_infraction_task.delay(
            camera_id=camera_id,
            infraction_type=infraction_type,
            confidence=float(confidence),
            offender_name=offender_name,
            snapshot_b64=snapshot_b64,
            temp_dir=temp_dir,
            bbox=bbox
        )
        logger.info("[INFRACTION PROXY] Delegated infraction to Celery: camera=%s type=%s", camera_id, infraction_type)
    except Exception as exc:
        logger.exception("[INFRACTION PROXY] Failed to delegate to Celery: %s", exc)


def delegate_save_unknown_face_to_celery(camera_id: int, face_crop: np.ndarray, embedding: list) -> None:
    import base64
    from camera.tasks import save_unknown_face_task

    try:
        crop_b64 = ""
        ok, buffer = cv2.imencode(".jpg", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if ok:
            crop_b64 = base64.b64encode(buffer.tobytes()).decode('utf-8')

        save_unknown_face_task.delay(
            camera_id=camera_id,
            crop_b64=crop_b64,
            embedding=embedding
        )
        logger.info("[UNKNOWN PROXY] Delegated unknown face to Celery: camera=%s", camera_id)
    except Exception as exc:
        logger.exception("[UNKNOWN PROXY] Failed to delegate to Celery: %s", exc)

# =========================
# RTSP helpers
# =========================

def rtsp_candidates(cam: Camera) -> list[str]:
    pwd = urllib.parse.quote(cam.password or "", safe="")
    user = cam.username or "admin"
    ip = cam.ip
    return [
        f"rtsp://{user}:{pwd}@{ip}:554/Streaming/Channels/101",
        f"rtsp://{user}:{pwd}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{user}:{pwd}@{ip}:554/live/ch00_0",
        f"rtsp://{user}:{pwd}@{ip}:554",
    ]

def build_ffmpeg_cmd(rtsp_url: str, use_hwaccel: bool) -> list[str]:
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

    if use_hwaccel and DEVICE.type == "cuda":
        cmd += ["-hwaccel", "cuda"]

    cmd += [
        "-i", rtsp_url,
        "-vf", f"fps={MAX_DETECTION_FPS},scale={STREAM_W}:{STREAM_H}:flags=fast_bilinear",
        "-an", "-sn",
        "-vcodec", "mjpeg",
        "-q:v", str(JPEG_QV),
        "-threads", "1",
        "-f", "image2pipe",
        "pipe:1",
    ]
    return cmd

# =========================
# JPEG helpers
# =========================

def decode_jpeg(jpeg: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(jpeg, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def iter_jpegs(buf: bytearray):
    """
    Robust: MJPEG streamdan JPEG frame ajratib beradi.
    """
    while True:
        start = buf.find(b"\xff\xd8")
        if start == -1:
            if len(buf) > MAX_BUFFER_BYTES:
                buf.clear()
            return

        end = buf.find(b"\xff\xd9", start + 2)
        if end == -1:
            if len(buf) > MAX_BUFFER_BYTES:
                del buf[:start]
            return

        jpeg = bytes(buf[start:end + 2])
        del buf[:end + 2]
        if len(jpeg) < 1500:
            continue
        yield jpeg

async def sleep_backoff(attempt: int) -> None:
    base = min(RECONNECT_MAX, RECONNECT_BASE * (2 ** max(0, attempt - 1)))
    jitter = base * RECONNECT_JITTER * (random.random() * 2 - 1)
    await asyncio.sleep(max(0.35, base + jitter))

# =========================
# Reader: stderr drain
# =========================

async def _drain_stderr(proc: asyncio.subprocess.Process, cam_id: int) -> None:
    """
    STDERR buffer to'lib qolib process qotib qolmasligi uchun.
    """
    try:
        if not proc.stderr:
            return
        while proc.returncode is None:
            line = await proc.stderr.readline()
            if not line:
                await asyncio.sleep(0.2)
                continue
            # juda ko'p chiqmasin
            txt = line.decode(errors="ignore").strip()
            if txt:
                logger.debug("[CAM %s][FFMPEG] %s", cam_id, txt[:400])
    except asyncio.CancelledError:
        return
    except Exception:
        return

# =========================
# Worker model
# =========================

@dataclass
class CameraWorker:
    cam_id: int
    ip: str
    name: str
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None

    use_hwaccel: bool = True
    attempt: int = 0

    frames: int = 0
    faces: int = 0
    last_frame_m: float = field(default_factory=time.monotonic)
    last_stats_m: float = field(default_factory=time.monotonic)
    last_detection_m: float = field(default_factory=time.monotonic)

    enable_face_detection: bool = False
    enable_infraction_detection: bool = False
    rtsp_url: str = ""
    username: str = ""
    password: str = ""

# =========================
# Daemon
# =========================

class RtspDaemon:
    def __init__(self, poll_seconds: int = 0):
        self.poll_seconds = max(0, int(poll_seconds or 0))
        self._stop = asyncio.Event()
        self._workers: dict[int, CameraWorker] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._exit_task: Optional[asyncio.Task] = None

    def request_stop(self):
        self._stop.set()

    async def start(self):
        if not FACE_APPS:
            logger.error("[DAEMON] FACE_APPS is not initialized. Exiting.")
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, warmup_encodings_cache)

        self._exit_task = asyncio.create_task(auto_exit_detector_loop())
        await self._sync_cameras()

        if self.poll_seconds > 0:
            self._poll_task = asyncio.create_task(self._poll_loop())

        await self._stop.wait()
        await self.shutdown()

    async def shutdown(self):
        if self._poll_task:
            self._poll_task.cancel()

        for w in list(self._workers.values()):
            w.stop_event.set()
            if w.task:
                w.task.cancel()

        if self._exit_task:
            self._exit_task.cancel()

    async def _poll_loop(self):
        while not self._stop.is_set():
            await asyncio.sleep(self.poll_seconds)
            await self._sync_cameras()

    async def _sync_cameras(self):
        cams = await fetch_active_cameras()
        active_ids = {c.id for c in cams}

        for cam_id in list(self._workers.keys()):
            if cam_id not in active_ids:
                w = self._workers.pop(cam_id)
                w.stop_event.set()
                if w.task:
                    w.task.cancel()
                logger.info("[DAEMON] camera stopped id=%s", cam_id)

        for cam in cams:
            w = self._workers.get(cam.id)
            if w is None:
                w = CameraWorker(
                    cam_id=cam.id,
                    ip=cam.ip,
                    name=cam.name or cam.ip,
                    enable_face_detection=cam.enable_face_detection,
                    enable_infraction_detection=cam.enable_infraction_detection,
                    rtsp_url=cam.rtsp_url or "",
                    username=cam.username or "",
                    password=cam.password or "",
                )
                w.task = asyncio.create_task(self._camera_loop(cam, w))
                self._workers[cam.id] = w
                logger.info("[DAEMON] camera started id=%s ip=%s name=%s", cam.id, cam.ip, cam.name or "-")
            else:
                changed = (
                    w.enable_face_detection != cam.enable_face_detection or
                    w.enable_infraction_detection != cam.enable_infraction_detection or
                    w.ip != cam.ip or
                    w.rtsp_url != (cam.rtsp_url or "") or
                    w.username != (cam.username or "") or
                    w.password != (cam.password or "")
                )
                if changed:
                    w.stop_event.set()
                    if w.task:
                        w.task.cancel()
                    logger.info("[DAEMON] camera config changed, restarting worker id=%s ip=%s name=%s", cam.id, cam.ip, cam.name or "-")
                    
                    new_w = CameraWorker(
                        cam_id=cam.id,
                        ip=cam.ip,
                        name=cam.name or cam.ip,
                        enable_face_detection=cam.enable_face_detection,
                        enable_infraction_detection=cam.enable_infraction_detection,
                        rtsp_url=cam.rtsp_url or "",
                        username=cam.username or "",
                        password=cam.password or "",
                    )
                    new_w.task = asyncio.create_task(self._camera_loop(cam, new_w))
                    self._workers[cam.id] = new_w

    async def _camera_loop(self, cam: Camera, w: CameraWorker):
        frame_queue = asyncio.Queue(maxsize=1)
        ai_task = asyncio.create_task(self._ai_worker_loop(cam, w, frame_queue))

        try:
            loop = asyncio.get_running_loop()

            while not self._stop.is_set() and not w.stop_event.is_set():
                w.attempt += 1
                buffer = bytearray()
                proc = None
                stderr_task = None

                try:
                    # candidate urlsni navbat bilan sinaymiz
                    urls = rtsp_candidates(cam)
                    random.shuffle(urls)

                    connected = False
                    for url in urls:
                        # 1. go2rtc orqali ulashishga harakat qilamiz (multi-client uchun)
                        try:
                            from django.conf import settings
                            from camera.views import register_go2rtc_stream

                            go2rtc_rtsp_port = getattr(settings, "GO2RTC_RTSP_PORT", 8554)

                            # go2rtc ga stream qo'shamiz
                            logger.info("[CAM %s] Registering go2rtc stream for url=%s", cam.id, url)
                            await loop.run_in_executor(None, register_go2rtc_stream, cam.id, url, "_high")

                            # go2rtc oqimni yuklab olishi uchun biroz kutamiz
                            await asyncio.sleep(0.8)

                            # go2rtc-ning mahalliy RTSP manzilini quramiz
                            go2rtc_rtsp_url = f"rtsp://127.0.0.1:{go2rtc_rtsp_port}/camera_{cam.id}_high"
                            go2rtc_cmd = build_ffmpeg_cmd(go2rtc_rtsp_url, w.use_hwaccel)

                            logger.info("[CAM %s] Attempting ffmpeg start hwaccel=%s via go2rtc relay: %s", cam.id, "ON" if w.use_hwaccel else "OFF", go2rtc_rtsp_url)
                            proc = await asyncio.create_subprocess_exec(
                                *go2rtc_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            # Tezkor o'chib qolishini tekshirish
                            await asyncio.sleep(0.8)

                            if proc.returncode is None:
                                stderr_task = asyncio.create_task(_drain_stderr(proc, cam.id))
                                connected = True
                                logger.info("[CAM %s] Successfully connected to go2rtc relay stream.", cam.id)
                                break
                            else:
                                logger.warning("[CAM %s] go2rtc ffmpeg failed immediately (exit code %s). Falling back to direct RTSP.", cam.id, proc.returncode)
                                proc = None
                        except Exception as exc:
                            logger.warning("[CAM %s] go2rtc streaming integration failed: %s. Falling back to direct RTSP.", cam.id, exc)
                            proc = None

                        # 2. Agar go2rtc ulanishi muvaffaqiyatsiz bo'lsa, to'g'ridan-to'g'ri jismoniy kameraga ulanamiz
                        cmd = build_ffmpeg_cmd(url, w.use_hwaccel)
                        logger.info("[CAM %s] ffmpeg start direct hwaccel=%s url=%s", cam.id, "ON" if w.use_hwaccel else "OFF", url)
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                *cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            stderr_task = asyncio.create_task(_drain_stderr(proc, cam.id))
                            connected = True
                            break
                        except Exception as exc:
                            logger.warning("[CAM %s] Direct ffmpeg spawn failed url=%s err=%s", cam.id, url, exc)
                            await asyncio.sleep(0.6)

                    if not connected or not proc or not proc.stdout:
                        await sleep_backoff(w.attempt)
                        continue

                    w.last_frame_m = time.monotonic()
                    w.last_stats_m = time.monotonic()
                    buffer.clear()

                    while not self._stop.is_set() and not w.stop_event.is_set():
                        # ✅ ffmpeg stdout o'qish timeout (deadlock oldini oladi)
                        try:
                            chunk = await asyncio.wait_for(proc.stdout.read(32768), timeout=READ_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            logger.warning("[CAM %s] ffmpeg read timeout -> restarting", cam.id)
                            break

                        if not chunk:
                            logger.warning("[CAM %s] ffmpeg eof -> restarting", cam.id)
                            break

                        buffer.extend(chunk)

                        # buffer ketib qolmasin
                        if len(buffer) > MAX_BUFFER_BYTES:
                            logger.warning("[CAM %s] buffer overflow -> clearing", cam.id)
                            buffer.clear()
                            continue

                        for jpeg in iter_jpegs(buffer):
                            frame = await loop.run_in_executor(None, decode_jpeg, jpeg)
                            if frame is None:
                                continue

                            w.frames += 1
                            w.last_frame_m = time.monotonic()

                            if w.frames % RECOG_FRAME_STEP != 0:
                                continue

                            # ✅ Soniyasiga maksimal MAX_DETECTION_FPS ta kadrni AI deteksiyaga yuboramiz
                            now = time.monotonic()
                            if now - w.last_detection_m < (0.85 / MAX_DETECTION_FPS):
                                continue
                            w.last_detection_m = now

                            # Frame queue ga yangi kadrni joylaymiz, eski kadr bo'lsa uni tashlab yuboramiz (realtime)
                            if frame_queue.full():
                                try:
                                    frame_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            frame_queue.put_nowait(frame)

                        # ✅ kamera heartbeat: video kelmasa restart
                        if time.monotonic() - w.last_frame_m > CAMERA_HEARTBEAT_S:
                            logger.warning("[CAM %s] heartbeat timeout (no frames) -> restarting", cam.id)
                            break

                        # stats
                        if time.monotonic() - w.last_stats_m > STATS_EVERY_S:
                            w.last_stats_m = time.monotonic()
                            logger.info(
                                "[CAM %s][STATS] frames=%s faces=%s hwaccel=%s",
                                cam.id, w.frames, w.faces, "ON" if w.use_hwaccel else "OFF"
                            )
                            # reset counters per window
                            w.frames = 0
                            w.faces = 0

                except asyncio.CancelledError:
                    if proc:
                        proc.kill()
                    return
                except Exception as exc:
                    logger.exception("[CAM %s] loop error: %s", cam.id, exc)

                finally:
                    if stderr_task:
                        stderr_task.cancel()
                    if proc:
                        try:
                            if proc.returncode is None:
                                proc.terminate()
                                try:
                                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                                except asyncio.TimeoutError:
                                    proc.kill()
                                    await proc.wait()
                        except Exception:
                            pass

                await sleep_backoff(w.attempt)
        finally:
            ai_task.cancel()
            try:
                await ai_task
            except asyncio.CancelledError:
                pass

    async def _ai_worker_loop(self, cam: Camera, w: CameraWorker, queue: asyncio.Queue):
        loop = asyncio.get_running_loop()
        logger.info("[CAM %s][AI] Worker loop started", cam.id)

        last_infraction_time = {}
        last_unknown_log_time = {}
        INFRACTION_COOLDOWN_S = 30.0

        from collections import deque
        frame_history = deque(maxlen=40)

        try:
            while not self._stop.is_set() and not w.stop_event.is_set():
                frame = await queue.get()
                
                # Maintain frame history (keep original resolution for high definition)
                try:
                    frame_history.append(frame.copy())
                except Exception as re:
                    logger.error("[INFRACTION] Append history frame failed: %s", re)

                try:
                    # 1. Face recognition (only if enabled on camera)
                    if cam.enable_face_detection:
                        # ✅ AI face detection run on background
                        faces = await loop.run_in_executor(None, detect_faces_sync, frame)
                        w.faces += len(faces)

                        for face in faces[:MAX_FACES_PER_FRAME]:
                            # 1. Face detection confidence filter (removes low-scoring background false-positives)
                            det_score = getattr(face, "det_score", 0.0)
                            if det_score < 0.75:
                                continue

                            x1, y1, x2, y2 = face.bbox.astype(int)
                            
                            # 2. Face size filter (removes tiny background artifacts and noise)
                            fw = x2 - x1
                            fh = y2 - y1
                            if fw < 40 or fh < 40:
                                continue

                            crop = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)].copy()
                            if crop.size == 0:
                                continue

                            emb = face.normed_embedding.astype(np.float32)
                            user, sim = await loop.run_in_executor(None, recognize_user_from_embedding, emb, None, cam.name)

                            if user is not None:
                                # Liveness check (Anti-spoofing)
                                from camera.liveness import check_liveness
                                bbox_xyxy = [float(x1), float(y1), float(x2), float(y2)]
                                is_real, liveness_score = await loop.run_in_executor(None, check_liveness, frame, bbox_xyxy)
                                
                                if not is_real:
                                    logger.warning(
                                        "[LIVENESS DETECTED SPOOF] Foydalanuvchi %s uchun soxta davomat urinishi aniqlandi! Liveness score: %.2f",
                                        user.full_name or user.username,
                                        liveness_score
                                    )
                                    continue

                                await loop.run_in_executor(None, process_recognition_sync, user, crop, cam)

                                person_id = user.employee_id_number if user.role == "employee" else user.student_id_number

                                enqueued = enqueue_hikvision_event(
                                    camera_ip=cam.ip,
                                    full_name=(user.full_name or user.username or "Unknown"),
                                    person_id=person_id or "",
                                    user_id=user.id,
                                    similarity=sim,
                                )

                                # Broadcast to live_attendance_events channels group
                                if enqueued and CHANNELS_AVAILABLE:
                                    try:
                                        channel_layer = get_channel_layer()
                                        if channel_layer is not None:
                                            photo_url = user.image.url if getattr(user, "image", None) and user.image else None
                                            await channel_layer.group_send(
                                                "live_attendance_events",
                                                {
                                                    "type": "face_detected_event",
                                                    "camera_id": cam.id,
                                                    "user_id": user.id,
                                                    "full_name": user.full_name or user.username or "Unknown",
                                                    "photo_url": photo_url,
                                                    "role": getattr(user, "role", "unknown"),
                                                    "entry_time": time.strftime("%H:%M"),
                                                    "last_seen_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                    "is_present": True
                                                }
                                            )
                                    except Exception as e:
                                        logger.error("[RTSP Channels Broadcast] Failed to send to channels group: %s", e)
                            else:
                                # Bu noma'lum yuz (unknown face)
                                # Throttling: har bir kamerada noma'lum yuzni har 5.0 soniyada ko'pi bilan 1 marta saqlaymiz
                                nowm = time.monotonic()
                                if nowm - last_unknown_log_time.get(cam.id, 0.0) >= 5.0:
                                    last_unknown_log_time[cam.id] = nowm
                                    
                                    # Butun tana rasmini olish uchun kropni kengaytiramiz
                                    fw = x2 - x1
                                    fh = y2 - y1
                                    bx1 = max(0, int(x1 - fw))
                                    bx2 = min(frame.shape[1], int(x2 + fw))
                                    by1 = max(0, int(y1 - 0.5 * fh))
                                    by2 = min(frame.shape[0], int(y2 + 6 * fh))
                                    body_crop = frame[by1:by2, bx1:bx2].copy()
                                    
                                    target_crop = body_crop if body_crop.size > 0 else crop
                                    
                                    if target_crop.size > 0:
                                        await loop.run_in_executor(
                                            None,
                                            delegate_save_unknown_face_to_celery,
                                            cam.id,
                                            target_crop,
                                            emb.tolist()
                                        )

                    # 2. Infraction detection (only if enabled on camera)
                    if cam.enable_infraction_detection:
                        from camera.infractions import detect_infractions
                        violations = await loop.run_in_executor(None, detect_infractions, frame)
                        for vio in violations:
                            v_type = vio["type"]
                            
                            # Filter out 'sleeping' infractions on non-classroom cameras or outside active lesson hours
                            if v_type == "sleeping":
                                if not getattr(cam, "is_lesson_camera", False):
                                    continue
                                has_active_lesson = await loop.run_in_executor(None, check_active_lesson_sync, cam.id)
                                if not has_active_lesson:
                                    continue

                            nowm = time.monotonic()
                            if nowm - last_infraction_time.get(v_type, 0.0) >= INFRACTION_COOLDOWN_S:
                                last_infraction_time[v_type] = nowm
                                x1, y1, x2, y2 = vio["bbox"]
                                crop_vio = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)].copy()
                                
                                # Check offender identity if it's smoking or sleeping
                                offender_name = "Shaxsi aniqlanmadi"
                                if v_type in ("smoking", "sleeping"):
                                    try:
                                        faces = await loop.run_in_executor(None, detect_faces_sync, frame)
                                        for face in faces:
                                            fx1, fy1, fx2, fy2 = face.bbox.astype(int)
                                            fcx = (fx1 + fx2) / 2
                                            fcy = (fy1 + fy2) / 2
                                            scx = (x1 + x2) / 2
                                            scy = (y1 + y2) / 2
                                            distance = np.sqrt((fcx - scx)**2 + (fcy - scy)**2)
                                            if distance < 250:
                                                emb = face.normed_embedding.astype(np.float32)
                                                user, sim = await loop.run_in_executor(None, recognize_user_from_embedding, emb)
                                                if user is not None:
                                                    offender_name = user.full_name or user.username or "Shaxsi aniqlanmadi"
                                                    break
                                    except Exception as fe:
                                        logger.error("[INFRACTION] Face recognition for %s failed: %s", v_type, fe)
                                
                                # Take snapshot of current history (save video for all infraction types)
                                video_frames = list(frame_history)
                                
                                # Create a copy of the full frame and draw the infraction bounding box
                                frame_with_box = frame.copy()
                                box_color = (0, 0, 255)  # Red
                                cv2.rectangle(frame_with_box, (x1, y1), (x2, y2), box_color, 3)
                                
                                label_text = f"{'Sigaret' if v_type == 'smoking' else 'Janjal' if v_type == 'fight' else 'Uxlash' if v_type == 'sleeping' else 'Boshqa'} ({int(vio['confidence']*100)}%)"
                                cv2.putText(
                                    frame_with_box,
                                    label_text,
                                    (x1, max(y1 - 10, 0)),
                                    cv2.FONT_HERSHEY_DUPLEX,
                                    0.7,
                                    box_color,
                                    2,
                                    lineType=cv2.LINE_AA
                                )
                                
                                await loop.run_in_executor(
                                    None,
                                    delegate_save_infraction_to_celery,
                                    cam.id,
                                    v_type,
                                    vio["confidence"],
                                    frame_with_box,
                                    offender_name,
                                    video_frames,
                                    [x1, y1, x2, y2]
                                )
                except Exception as exc:
                    logger.exception("[CAM %s][AI] Worker error: %s", cam.id, exc)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            logger.info("[CAM %s][AI] Worker loop stopped", cam.id)

# =========================
# Entrypoints
# =========================

async def run_forever(poll_seconds: int = 0):
    daemon = RtspDaemon(poll_seconds=poll_seconds)
    loop = asyncio.get_running_loop()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, daemon.request_stop)
        except NotImplementedError:
            pass

    await daemon.start()

def run_blocking(poll_seconds: int = 0):
    asyncio.run(run_forever(poll_seconds=poll_seconds))
