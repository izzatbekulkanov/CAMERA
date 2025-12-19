# camera/rtsp_runner.py
from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import signal
import time
import urllib.parse
from collections import defaultdict
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

logger = logging.getLogger(__name__)

# =========================
# CONFIG (production)
# =========================

STREAM_W, STREAM_H = 1280, 720      # 720p: uzoq yuzlar uchun yaxshi balans
JPEG_QV = 4                         # 2..5 (2=quality yuqori)
RECOG_FRAME_STEP = 1                # 1: maksimal sezgirlik (keyin 2 qilsa bo'ladi)
MAX_FACES_PER_FRAME = 3

MIN_FACE_SIZE = 14                  # uzoq yuzlar uchun kichraytirildi
SHARPNESS_MIN = 0.0                 # debug uchun 0; keyin 15..35 qilib ko'taring

MAX_BUFFER_BYTES = 10_000_000

RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
RECONNECT_JITTER = 0.25

# Debug reports
STATS_EVERY_S = 5.0                 # har 5 sekundda stats log
LOG_FACE_COOLDOWN = 2.0
LOG_USER_COOLDOWN = 8.0

# =========================
# FFMPEG / CUDA
# =========================

FFMPEG_BIN = shutil.which("ffmpeg-gpu") or shutil.which("ffmpeg") or "ffmpeg"
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
logger.info("[RTSP] FFMPEG=%s DEVICE=%s", FFMPEG_BIN, DEVICE.type.upper())

# =========================
# InsightFace init (once)
# =========================

FACE_APP = None
try:
    from insightface.app import FaceAnalysis

    FACE_APP = FaceAnalysis(
        name="buffalo_l",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    # 🔥 Uzoq yuzlar uchun det_size kattaroq
    FACE_APP.prepare(ctx_id=0, det_size=(960, 960))

    logger.info("[RTSP] InsightFace ready (buffalo_l, det_size=960)")
except Exception as exc:  # noqa: BLE001
    FACE_APP = None
    logger.exception("[RTSP] InsightFace load failed: %s", exc)

# =========================
# DB helpers
# =========================

@sync_to_async
def fetch_active_cameras() -> list[Camera]:
    # ✅ talab bo'yicha: faqat enable_face_detection=True va is_active=True
    return list(
        Camera.objects.filter(is_active=True, enable_face_detection=True).order_by("id")
    )

# =========================
# RTSP / FFMPEG helpers
# =========================

def rtsp_candidates(cam: Camera) -> list[str]:
    pwd = urllib.parse.quote(cam.password or "", safe="")
    user = cam.username or "admin"
    ip = cam.ip
    return [
        f"rtsp://{user}:{pwd}@{ip}:554/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{user}:{pwd}@{ip}:554/Streaming/Channels/101",
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
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-err_detect", "ignore_err",
    ]

    if use_hwaccel and DEVICE.type == "cuda":
        cmd += ["-hwaccel", "cuda"]

    vf = f"scale={STREAM_W}:{STREAM_H}:flags=bicubic"
    cmd += [
        "-i", rtsp_url,
        "-vf", vf,
        "-an",
        "-sn",
        "-vcodec", "mjpeg",
        "-q:v", str(JPEG_QV),
        "-f", "image2pipe",
        "pipe:1",
    ]
    return cmd

# =========================
# JPEG parsing / decode
# =========================

def decode_jpeg(jpeg: bytes) -> Optional[np.ndarray]:
    arr = np.frombuffer(jpeg, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def iter_jpegs(buf: bytearray):
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
        yield jpeg

def sharpness_score(bgr: np.ndarray) -> float:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())

async def sleep_backoff(attempt: int) -> None:
    base = min(RECONNECT_MAX, RECONNECT_BASE * (2 ** max(0, attempt - 1)))
    jitter = base * RECONNECT_JITTER * (random.random() * 2 - 1)
    await asyncio.sleep(max(0.35, base + jitter))

# =========================
# Worker
# =========================

@dataclass
class CameraWorker:
    cam_id: int
    ip: str
    name: str
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None

    use_hwaccel: bool = True

    # stats
    started_m: float = field(default_factory=time.monotonic)
    last_report_m: float = field(default_factory=time.monotonic)

    bytes_out: int = 0              # stdout (jpeg pipe) bytes
    frames_decoded: int = 0         # decoded frames count
    frames_processed: int = 0       # frames that ran recognition step
    faces_seen: int = 0             # total faces detected
    faces_processed: int = 0        # faces attempted for recognition

# throttles
_last_seen_log: dict[int, float] = defaultdict(float)
_last_user_log: dict[int, dict[int, float]] = defaultdict(dict)

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

        # ✅ warmup async-safe
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, warmup_encodings_cache)
            logger.info("[DAEMON] encodings cache warmed")
        except Exception as exc:
            logger.warning("[DAEMON] warmup failed: %s", exc)

        logger.info("[DAEMON] started poll=%ss pid=%s", self.poll_seconds, os.getpid())

        # exit detector loop
        self._exit_task = asyncio.create_task(auto_exit_detector_loop())

        # initial sync
        await self._sync_cameras()

        # hot reload
        if self.poll_seconds > 0:
            self._poll_task = asyncio.create_task(self._poll_loop())

        await self._stop.wait()
        await self.shutdown()

    async def shutdown(self):
        logger.info("[DAEMON] shutting down workers=%s", len(self._workers))

        if self._poll_task:
            self._poll_task.cancel()

        # stop workers
        for w in list(self._workers.values()):
            w.stop_event.set()
            if w.task and not w.task.done():
                w.task.cancel()

        tasks = [w.task for w in self._workers.values() if w.task]
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)

        self._workers.clear()

        # stop exit loop
        if self._exit_task:
            self._exit_task.cancel()
            try:
                await self._exit_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        logger.info("[DAEMON] stopped.")

    async def _poll_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.poll_seconds)
                await self._sync_cameras()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.exception("[DAEMON] poll loop error: %s", exc)

    async def _sync_cameras(self):
        cams = await fetch_active_cameras()
        active_ids = {c.id for c in cams}

        # stop removed
        for cam_id in list(self._workers.keys()):
            if cam_id not in active_ids:
                logger.info("[DAEMON] camera removed/disabled -> stop cam_id=%s", cam_id)
                w = self._workers.pop(cam_id, None)
                if w:
                    w.stop_event.set()
                    if w.task and not w.task.done():
                        w.task.cancel()

        # start new
        for cam in cams:
            if cam.id not in self._workers:
                w = CameraWorker(cam_id=cam.id, ip=cam.ip, name=(cam.name or cam.ip))
                w.task = asyncio.create_task(self._camera_loop(cam, w))
                self._workers[cam.id] = w
                logger.info("[DAEMON] camera started cam_id=%s name=%s ip=%s", cam.id, w.name, w.ip)

    async def _terminate_proc(self, cam_id: int, proc: asyncio.subprocess.Process):
        try:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _report_stats(self, cam: Camera, w: CameraWorker):
        nowm = time.monotonic()
        dt = max(1e-6, nowm - w.last_report_m)

        fps_dec = w.frames_decoded / dt
        fps_proc = w.frames_processed / dt
        mbps = (w.bytes_out * 8.0) / dt / (1024 * 1024)

        logger.info(
            "[CAM %s][STATS] hwaccel=%s res=%sx%s fps_dec=%.1f fps_proc=%.1f mbps=%.2f faces=%s processed_faces=%s",
            cam.id,
            "ON" if w.use_hwaccel else "OFF",
            STREAM_W, STREAM_H,
            fps_dec, fps_proc, mbps,
            w.faces_seen, w.faces_processed,
        )

        # reset window counters
        w.last_report_m = nowm
        w.bytes_out = 0
        w.frames_decoded = 0
        w.frames_processed = 0
        w.faces_seen = 0
        w.faces_processed = 0

    async def _camera_loop(self, cam: Camera, w: CameraWorker):
        attempt = 0
        loop = asyncio.get_running_loop()

        while not self._stop.is_set() and not w.stop_event.is_set():
            attempt += 1
            proc: Optional[asyncio.subprocess.Process] = None
            err_text = ""
            buf = bytearray()

            try:
                url = rtsp_candidates(cam)[0]
                cmd = build_ffmpeg_cmd(url, use_hwaccel=w.use_hwaccel)

                logger.info(
                    "[CAM %s] ffmpeg start attempt=%s hwaccel=%s url=%s",
                    cam.id, attempt, "ON" if w.use_hwaccel else "OFF", url
                )

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                attempt = 0
                last_stats_m = time.monotonic()

                while not self._stop.is_set() and not w.stop_event.is_set():
                    if proc.stdout is None:
                        break

                    chunk = await proc.stdout.read(32768)
                    if not chunk:
                        if proc.stderr:
                            err = await proc.stderr.read()
                            if err:
                                err_text = err.decode(errors="ignore")[:2000]
                                logger.error("[CAM %s] ffmpeg stderr: %s", cam.id, err_text)
                        break

                    w.bytes_out += len(chunk)
                    buf.extend(chunk)

                    # stats log window
                    nowm = time.monotonic()
                    if nowm - last_stats_m >= STATS_EVERY_S:
                        self._report_stats(cam, w)
                        last_stats_m = nowm

                    for jpeg in iter_jpegs(buf):
                        # decode frame (har frame)
                        frame = await loop.run_in_executor(None, decode_jpeg, jpeg)
                        if frame is None:
                            continue
                        w.frames_decoded += 1

                        # recognition sampling
                        if w.frames_decoded % RECOG_FRAME_STEP != 0:
                            continue

                        w.frames_processed += 1

                        # ✅ InsightFace ko'pincha RGB kutadi
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        faces = FACE_APP.get(frame_rgb)  # type: ignore[union-attr]
                        w.faces_seen += len(faces)

                        nowm2 = time.monotonic()

                        if faces and (nowm2 - _last_seen_log[cam.id] >= LOG_FACE_COOLDOWN):
                            logger.info("[CAM %s] ODAM: faces=%s", cam.id, len(faces))
                            _last_seen_log[cam.id] = nowm2

                        if not faces:
                            continue

                        h, ww = frame.shape[:2]
                        processed_here = 0

                        for face in faces:
                            x1, y1, x2, y2 = face.bbox.astype(int)
                            left = max(int(x1), 0)
                            top = max(int(y1), 0)
                            right = min(int(x2), ww - 1)
                            bottom = min(int(y2), h - 1)

                            if (bottom - top) < MIN_FACE_SIZE or (right - left) < MIN_FACE_SIZE:
                                continue

                            crop = frame[top:bottom, left:right].copy()

                            if SHARPNESS_MIN > 0:
                                if sharpness_score(crop) < SHARPNESS_MIN:
                                    continue

                            w.faces_processed += 1

                            emb = face.normed_embedding.astype(np.float32)

                            user, sim = await loop.run_in_executor(
                                None, recognize_user_from_embedding, emb
                            )

                            if user is not None:
                                last_t = _last_user_log[cam.id].get(user.id, 0.0)
                                if nowm2 - last_t >= LOG_USER_COOLDOWN:
                                    logger.info(
                                        "[CAM %s] USER: id=%s name=%s role=%s dept=%s sim=%.3f",
                                        cam.id,
                                        getattr(user, "id", None),
                                        (getattr(user, "full_name", "") or getattr(user, "username", "")).strip(),
                                        getattr(user, "role", None),
                                        getattr(user, "department_name", None),
                                        float(sim),
                                    )
                                    _last_user_log[cam.id][user.id] = nowm2

                                await loop.run_in_executor(None, process_recognition_sync, user, crop)

                            processed_here += 1
                            if processed_here >= MAX_FACES_PER_FRAME:
                                break

                # end stream
                if proc is not None:
                    await self._terminate_proc(cam.id, proc)

                # GPU decode OOM -> CPU decode fallback (shu kamera)
                if w.use_hwaccel and (
                    "CUDA_ERROR_OUT_OF_MEMORY" in err_text
                    or "hwaccel initialisation returned error" in err_text
                    or "cuvidCreateDecoder" in err_text
                ):
                    w.use_hwaccel = False
                    logger.warning(
                        "[CAM %s] GPU decode OOM -> fallback CPU decode for this camera (AI stays GPU)",
                        cam.id,
                    )

                logger.warning("[CAM %s] stream ended, reconnecting...", cam.id)
                await sleep_backoff(1)

            except asyncio.CancelledError:
                if proc is not None:
                    await self._terminate_proc(cam.id, proc)
                return
            except Exception as exc:
                logger.exception("[CAM %s] loop error: %s", cam.id, exc)
                if proc is not None:
                    await self._terminate_proc(cam.id, proc)
                await sleep_backoff(max(1, attempt))

        logger.info("[CAM %s] loop stopped", cam.id)

# =========================
# Entrypoints
# =========================

async def run_forever(poll_seconds: int = 0):
    daemon = RtspDaemon(poll_seconds=poll_seconds)
    loop = asyncio.get_running_loop()

    def _handle(sig):
        logger.warning("[DAEMON] received signal %s -> stopping", sig)
        daemon.request_stop()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _handle, s)
        except NotImplementedError:
            pass

    await daemon.start()

def run_blocking(poll_seconds: int = 0):
    asyncio.run(run_forever(poll_seconds=poll_seconds))
