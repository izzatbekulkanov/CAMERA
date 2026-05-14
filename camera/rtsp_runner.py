# camera/rtsp_runner
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import signal
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import torch
from asgiref.sync import sync_to_async

from camera.models import Camera
from camera.recognition import (
    auto_exit_detector_loop,
    process_recognition_sync,
    recognize_user_from_embedding,
    warmup_encodings_cache,
)
from camera.hikvision import enqueue_hikvision_event

logger = logging.getLogger(__name__)

# =========================
# CONFIG (stable)
# =========================

STREAM_W, STREAM_H = 1280, 720
JPEG_QV = 4               # quality 2..10 (lower = better quality)
RECOG_FRAME_STEP = 1
MAX_FACES_PER_FRAME = 3

MAX_BUFFER_BYTES = 12_000_000
READ_TIMEOUT_S = 4.0      # ffmpeg stdout.read timeout
CAMERA_HEARTBEAT_S = 25.0 # per-camera: agar video kelmasa restart
STATS_EVERY_S = 10.0

RECONNECT_BASE = 0.8
RECONNECT_MAX = 20.0
RECONNECT_JITTER = 0.30

# =========================
# FFMPEG / CPU (GPU o'chirilgan)
# =========================

# CPU-only mode: ffmpeg-gpu o'chiq, faqat ffmpeg
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
DEVICE = torch.device("cpu")  # Har doim CPU
logger.info("[RTSP] FFMPEG=%s DEVICE=%s (CPU-only mode)", FFMPEG_BIN, DEVICE.type.upper())

# =========================
# InsightFace init (once)
# =========================

FACE_APP = None
try:
    from insightface.app import FaceAnalysis

    # CPU-only: faqat CPUExecutionProvider
    providers = ['CPUExecutionProvider']
    ctx_id = -1  # CPU uchun -1

    FACE_APP = FaceAnalysis(
        name="buffalo_l",
        providers=providers,
    )
    FACE_APP.prepare(ctx_id=ctx_id, det_size=(960, 960))
    logger.info("[RTSP] InsightFace ready (buffalo_l, CPU-only, ctx_id=%s)", ctx_id)
except Exception as exc:
    FACE_APP = None
    logger.exception("[RTSP] InsightFace load failed: %s", exc)

# =========================
# DB helpers
# =========================

@sync_to_async
def fetch_active_cameras() -> list[Camera]:
    return list(
        Camera.objects.filter(is_active=True, enable_face_detection=True)
        .order_by("id")
    )

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
        "-vf", f"scale={STREAM_W}:{STREAM_H}:flags=bicubic",
        "-an", "-sn",
        "-vcodec", "mjpeg",
        "-q:v", str(JPEG_QV),
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
        if FACE_APP is None:
            logger.error("[DAEMON] FACE_APP is not initialized. Exiting.")
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
            if cam.id not in self._workers:
                w = CameraWorker(cam.id, cam.ip, cam.name or cam.ip)
                w.task = asyncio.create_task(self._camera_loop(cam, w))
                self._workers[cam.id] = w
                logger.info("[DAEMON] camera started id=%s ip=%s name=%s", cam.id, cam.ip, cam.name or "-")

    async def _camera_loop(self, cam: Camera, w: CameraWorker):
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
                    cmd = build_ffmpeg_cmd(url, w.use_hwaccel)
                    logger.info("[CAM %s] ffmpeg start hwaccel=%s url=%s", cam.id, "ON" if w.use_hwaccel else "OFF", url)

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
                        logger.warning("[CAM %s] ffmpeg spawn failed url=%s err=%s", cam.id, url, exc)
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

                        faces = FACE_APP.get(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                        w.faces += len(faces)

                        for face in faces[:MAX_FACES_PER_FRAME]:
                            x1, y1, x2, y2 = face.bbox.astype(int)
                            crop = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)].copy()
                            if crop.size == 0:
                                continue

                            emb = face.normed_embedding.astype(np.float32)
                            user, sim = await loop.run_in_executor(None, recognize_user_from_embedding, emb)

                            if user is not None:
                                await loop.run_in_executor(None, process_recognition_sync, user, crop)

                                person_id = user.employee_id_number if user.role == "employee" else user.student_id_number

                                enqueue_hikvision_event(
                                    camera_ip=cam.ip,
                                    full_name=(user.full_name or user.username or "Unknown"),
                                    person_id=person_id or "",
                                    user_id=user.id,
                                    similarity=sim,
                                )

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
