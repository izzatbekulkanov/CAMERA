# camera/recognition.py
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from datetime import timedelta
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
from asgiref.sync import sync_to_async
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from attendance.models import Attendance, AttendancePhoto
from users.models import CustomUser, FaceEncoding

logger = logging.getLogger(__name__)

# =========================
# CONFIG (production)
# =========================

# embedding dim (InsightFace buffalo_l)
_FAISS_DIM = 512

# cosine threshold
FACE_COSINE_THRESHOLD = 0.48

# encodings cache TTL (seconds)
ENCODING_TTL_SECONDS = 120

# DB write throttling
USER_DB_UPDATE_COOLDOWN_S = 2.0   # bir user uchun Attendance update minimal interval
PHOTO_INTERVAL_S = 20.0           # bir user uchun rasm saqlash minimal interval

# exit detector
EXIT_TIMEOUT_S = 180.0            # user ko‘rinmasa -> chiqdi
EXIT_SCAN_INTERVAL_S = 30.0       # har necha sekundda tekshiradi

# restartga chidamli exit loop (tavsiya)
USE_DB_BASED_EXIT_LOOP = True

# =========================
# In-memory state
# =========================

# user_id -> last_seen datetime (in-memory)
ACTIVE_SESSIONS: Dict[int, timezone.datetime] = {}

# user_id -> last db update monotonic time
_last_db_update: Dict[int, float] = {}

# user_id -> last photo save monotonic time
_last_photo_time: Dict[int, float] = {}

# optional locks (dictlar thread-safe bo'lishi uchun)
_state_lock = threading.Lock()

# =========================
# Helpers
# =========================

def _mono() -> float:
    return time.monotonic()

def _allow_action(cache: Dict[int, float], user_id: int, cooldown_s: float) -> bool:
    """
    True => ruxsat, False => throttle
    """
    now = _mono()
    with _state_lock:
        last = cache.get(user_id)
        if last is not None and (now - last) < cooldown_s:
            return False
        cache[user_id] = now
        return True

def _remember_seen(user_id: int, when: timezone.datetime) -> None:
    with _state_lock:
        ACTIVE_SESSIONS[user_id] = when

# =========================
# FAISS / embeddings cache
# =========================

FAISS_AVAILABLE = False
try:
    import faiss  # type: ignore
    FAISS_AVAILABLE = True
    logger.info("[FAISS] available (CPU IndexFlatIP)")
except Exception:
    logger.warning("[FAISS] not available, using NumPy fallback")

_faiss_lock = threading.Lock()
_faiss_index = None  # faiss.IndexFlatIP yoki np.ndarray
_faiss_users: list[CustomUser] | None = None
_faiss_expires_at: float = 0.0


def warmup_encodings_cache() -> None:
    """Daemon startda bir marta chaqirib qo'ying (lag kamayadi)."""
    with _faiss_lock:
        _build_index_sync()


def _build_index_sync() -> None:
    global _faiss_index, _faiss_users, _faiss_expires_at

    qs = (
        FaceEncoding.objects
        .filter(model_version="insightface_buffalo_l")
        .select_related("user")
    )

    embeddings: list[np.ndarray] = []
    users: list[CustomUser] = []

    # iterator chunk bilan — katta DBda RAM yemaydi
    for fe in qs.iterator(chunk_size=2000):
        vec = np.array(fe.encoding_data, dtype=np.float32)
        if vec.ndim != 1 or vec.shape[0] != _FAISS_DIM:
            continue

        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec = vec / norm

        embeddings.append(vec)
        users.append(fe.user)

    if embeddings:
        mat = np.stack(embeddings, axis=0).astype(np.float32)
    else:
        mat = np.empty((0, _FAISS_DIM), dtype=np.float32)

    if FAISS_AVAILABLE and mat.shape[0] > 0:
        idx = faiss.IndexFlatIP(_FAISS_DIM)  # cosine = dot
        idx.add(mat)
        _faiss_index = idx
        logger.info("[FAISS] index built N=%s", mat.shape[0])
    else:
        _faiss_index = mat
        logger.info("[FAISS] fallback matrix built N=%s", mat.shape[0])

    _faiss_users = users
    _faiss_expires_at = time.monotonic() + ENCODING_TTL_SECONDS


def _load_index():
    now = time.monotonic()
    if _faiss_index is not None and _faiss_expires_at > now:
        return _faiss_index, _faiss_users or []

    with _faiss_lock:
        now = time.monotonic()
        if _faiss_index is None or _faiss_expires_at <= now:
            _build_index_sync()
        return _faiss_index, _faiss_users or []


def _search_best(emb: np.ndarray) -> Optional[Tuple[float, int]]:
    index, _ = _load_index()

    # FAISS
    if FAISS_AVAILABLE and hasattr(index, "search"):
        try:
            v = emb.reshape(1, -1).astype(np.float32)
            if v.shape[1] != _FAISS_DIM:
                return None
            D, I = index.search(v, 1)
            if I.shape[0] == 0 or I[0][0] < 0:
                return None
            return float(D[0][0]), int(I[0][0])
        except Exception as exc:
            logger.error("[FAISS] search error: %s", exc)
            return None

    # NumPy fallback
    if isinstance(index, np.ndarray) and index.size > 0:
        sims = index @ emb.astype(np.float32)
        best_idx = int(np.argmax(sims))
        return float(sims[best_idx]), best_idx

    return None


def recognize_user_from_embedding(emb: np.ndarray) -> Tuple[Optional[CustomUser], float]:
    """
    emb: (512,) float32 normed embedding
    returns: (user_or_none, similarity)
    """
    index, users_db = _load_index()
    if index is None or not users_db:
        return None, 0.0

    res = _search_best(emb)
    if res is None:
        return None, 0.0

    sim, idx = res
    if 0 <= idx < len(users_db) and sim >= FACE_COSINE_THRESHOLD:
        return users_db[idx], float(sim)

    return None, float(sim)

# =========================
# Attendance update + photo
# =========================

def process_recognition_sync(user: CustomUser, face_crop: np.ndarray) -> None:
    """
    Production:
    - DB update throttled
    - photo save throttled
    """
    now_dt = timezone.now()
    today = timezone.localdate()

    # in-memory last seen
    _remember_seen(user.id, now_dt)

    # DB throttle
    if not _allow_action(_last_db_update, user.id, USER_DB_UPDATE_COOLDOWN_S):
        return

    try:
        with transaction.atomic():
            att, created = Attendance.objects.get_or_create(
                user=user,
                date=today,
                defaults={"entry_time": now_dt, "last_seen": now_dt, "is_present": True},
            )
            if not created:
                if not att.entry_time:
                    att.entry_time = now_dt
                att.last_seen = now_dt
                att.is_present = True
                att.save(update_fields=["entry_time", "last_seen", "is_present"])
    except Exception as exc:
        logger.exception("[ATTENDANCE] update failed user=%s: %s", user.id, exc)
        return

    # Photo throttle
    if not _allow_action(_last_photo_time, user.id, PHOTO_INTERVAL_S):
        return

    try:
        ok, buffer = cv2.imencode(".jpg", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            return
        filename = f"{user.username or 'user'}_{uuid.uuid4().hex[:8]}.jpg"
        photo = AttendancePhoto(attendance=att)
        photo.image.save(filename, ContentFile(buffer.tobytes()))
        logger.info("[PHOTO] saved user=%s file=%s", user.id, filename)
    except Exception as exc:
        logger.exception("[PHOTO] save failed user=%s: %s", user.id, exc)


def mark_exit_sync(user: CustomUser, when: Optional[timezone.datetime] = None) -> None:
    now_dt = when or timezone.now()
    today = timezone.localdate()

    try:
        att = Attendance.objects.filter(user=user, date=today, is_present=True).first()
        if not att:
            return

        att.exit_time = now_dt
        att.is_present = False

        if att.entry_time:
            att.duration_minutes = max(1, int((att.exit_time - att.entry_time).total_seconds() // 60))
        else:
            att.duration_minutes = 0

        att.save(update_fields=["exit_time", "is_present", "duration_minutes"])
        logger.info("[EXIT] user=%s exit_time=%s", user.id, att.exit_time)
    except Exception as exc:
        logger.exception("[EXIT] mark_exit failed user=%s: %s", user.id, exc)

# =========================
# Exit detector (async-safe)
# =========================

@sync_to_async
def _close_stale_attendances_sync(now_dt, cutoff, today) -> int:
    """
    DB-based exit:
    last_seen cutoff'dan eski bo'lgan is_present=True attendancelarni yopadi.
    """
    qs = Attendance.objects.filter(
        date=today,
        is_present=True,
        last_seen__lt=cutoff,
    ).select_related("user")

    closed = 0
    for att in qs.iterator(chunk_size=500):
        att.exit_time = now_dt
        att.is_present = False
        if att.entry_time:
            att.duration_minutes = max(1, int((now_dt - att.entry_time).total_seconds() // 60))
        else:
            att.duration_minutes = 0
        att.save(update_fields=["exit_time", "is_present", "duration_minutes"])
        closed += 1
    return closed


async def auto_exit_detector_loop() -> None:
    """
    ✅ Production:
    - DB-based loop (restart-safe) default
    - optional memory-based
    """
    if USE_DB_BASED_EXIT_LOOP:
        await _auto_exit_db_loop()
    else:
        await _auto_exit_memory_loop()


async def _auto_exit_memory_loop() -> None:
    logger.info("[AUTO EXIT] memory loop started")
    while True:
        await asyncio.sleep(EXIT_SCAN_INTERVAL_S)
        now_dt = timezone.now()

        with _state_lock:
            items = list(ACTIVE_SESSIONS.items())

        expired = [
            uid for uid, last_seen in items
            if (now_dt - last_seen).total_seconds() > EXIT_TIMEOUT_S
        ]
        if not expired:
            continue

        for uid in expired:
            # ORM sync -> thread
            user = await sync_to_async(CustomUser.objects.filter(id=uid).first)()
            if not user:
                with _state_lock:
                    ACTIVE_SESSIONS.pop(uid, None)
                continue

            await sync_to_async(mark_exit_sync)(user, now_dt)

            with _state_lock:
                ACTIVE_SESSIONS.pop(uid, None)


async def _auto_exit_db_loop() -> None:
    """
    Restart-safe exit detector (DB-based).
    Har EXIT_SCAN_INTERVAL_S sekundda:
      - bugungi is_present=True attendancelarni tekshiradi
      - last_seen cutoff'dan eski bo'lsa -> exit_time qo'yadi va is_present=False qiladi

    Muhim:
      - ORM sync bo'lgani uchun _close_stale_attendances_sync sync_to_async bo'lishi shart.
      - CancelledError (shutdown/restart) normal holat, stacktrace chiqarmaymiz.
    """
    logger.info("[AUTO EXIT] DB loop started (restart-safe)")
    try:
        while True:
            await asyncio.sleep(EXIT_SCAN_INTERVAL_S)

            now_dt = timezone.now()
            today = timezone.localdate()
            cutoff = now_dt - timedelta(seconds=EXIT_TIMEOUT_S)

            try:
                closed = await _close_stale_attendances_sync(now_dt, cutoff, today)
                if closed:
                    logger.info("[AUTO EXIT][DB] closed=%s", closed)
            except Exception as exc:
                logger.exception("[AUTO EXIT][DB] loop error: %s", exc)

    except asyncio.CancelledError:
        # ✅ systemd restart/stop paytida normal
        logger.info("[AUTO EXIT] DB loop cancelled (shutdown)")
        raise