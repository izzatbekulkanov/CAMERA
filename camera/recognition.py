# camera/recognition.py
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import timedelta
from typing import Dict, Optional, Tuple, List, Any

import cv2
import numpy as np
from asgiref.sync import sync_to_async
from django.core.files.base import ContentFile
from django.db import connections, transaction
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
FACE_COSINE_THRESHOLD = 0.43

# encodings cache TTL (seconds)
ENCODING_TTL_SECONDS = 120

# DB write throttling
USER_DB_UPDATE_COOLDOWN_S = 2.0   # bir user uchun Attendance update minimal interval
PHOTO_INTERVAL_S = 20.0           # bir user uchun rasm saqlash minimal interval

# active schedule users cache
_schedule_users_cache = {}  # {schedule_id: (expires_at, list_of_user_ids)}
_schedule_users_cache_lock = threading.Lock()

def get_allowed_ids_for_schedule(schedule_id: int) -> List[int]:
    global _schedule_users_cache
    now = time.monotonic()
    
    with _schedule_users_cache_lock:
        if schedule_id in _schedule_users_cache:
            exp, ids = _schedule_users_cache[schedule_id]
            if exp > now:
                return ids

    try:
        from camera.models import LessonSchedule
        from users.models import CustomUser
        
        sched = LessonSchedule.objects.select_related('academic_group').filter(id=schedule_id).first()
        if not sched:
            return []
            
        allowed_ids = []
        if sched.academic_group:
            student_ids = list(CustomUser.objects.filter(
                academic_group=sched.academic_group,
                role=CustomUser.Role.STUDENT,
                is_superuser=False
            ).values_list('id', flat=True))
            allowed_ids.extend(student_ids)
            
        if sched.teacher_name:
            teacher = CustomUser.objects.filter(
                full_name__icontains=sched.teacher_name,
                role=CustomUser.Role.EMPLOYEE
            ).first()
            if teacher:
                allowed_ids.append(teacher.id)
                
        allowed_ids = list(set(allowed_ids))
        
        with _schedule_users_cache_lock:
            _schedule_users_cache[schedule_id] = (now + 300.0, allowed_ids)
            
        return allowed_ids
    except Exception as exc:
        logger.error("[ALLOWED IDS] Failed to load allowed IDs for schedule=%s: %s", schedule_id, exc)
        return []

# exit detector
EXIT_TIMEOUT_S = 180.0            # user ko‘rinmasa -> chiqdi
EXIT_SCAN_INTERVAL_S = 30.0       # har necha sekundda tekshiradi

# restartga chidamli exit loop (tavsiya)
USE_DB_BASED_EXIT_LOOP = True

# =========================
# Qdrant client configuration (optional)
# =========================
QDRANT_ENABLED = os.getenv("QDRANT_ENABLED", "False").lower() in ("true", "1", "yes")
_qdrant_store = None
if QDRANT_ENABLED:
    try:
        from camera.qdrant_db import QdrantVectorStore
        _qdrant_store = QdrantVectorStore()
        if _qdrant_store.validate_collection():
            logger.info("[QDRANT] Vector store initialized successfully and collection validated.")
        else:
            logger.warning("[QDRANT] Vector store collection validation failed, Qdrant will not be used.")
            _qdrant_store = None
    except Exception as exc:
        logger.warning("[QDRANT] Failed to initialize Qdrant vector store: %s", exc)
        _qdrant_store = None

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
_faiss_matrix = None  # Always np.ndarray for allowed_ids NumPy slicing
_faiss_users: list[CustomUser] | None = None
_faiss_expires_at: float = 0.0


def warmup_encodings_cache() -> None:
    """Daemon startda bir marta chaqirib qo'ying (lag kamayadi)."""
    try:
        with _faiss_lock:
            _build_index_sync()
    finally:
        connections.close_all()


def _build_index_sync() -> None:
    global _faiss_index, _faiss_users, _faiss_expires_at, _faiss_matrix

    qs = (
        FaceEncoding.objects
        .filter(model_version="insightface_buffalo_l")
        .select_related("user", "user__academic_group")
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

    _faiss_matrix = mat

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

    # Sync to Qdrant if enabled
    if _qdrant_store is not None and embeddings:
        try:
            points_to_upsert = []
            for u, emb_vec in zip(users, embeddings):
                payload = {
                    "name": u.username or "",
                    "group_id": u.academic_group_id or 0,
                    "faculty_id": u.academic_group.faculty_id if u.academic_group else 0,
                    "education_year": u.education_year or ""
                }
                points_to_upsert.append((u.id, emb_vec, payload))
            if points_to_upsert:
                _qdrant_store.upsert(points_to_upsert)
                logger.info("[QDRANT] Synced %d embeddings with payload filters during index rebuild", len(points_to_upsert))
        except Exception as exc:
            logger.error("[QDRANT] Sync failed during index rebuild: %s", exc)


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


def recognize_user_from_embedding(emb: np.ndarray, schedule_id: Optional[int] = None, camera_name: Optional[str] = None) -> Tuple[Optional[CustomUser], float]:
    """
    emb: (512,) float32 normed embedding
    schedule_id: active schedule ID for filtering users (performance optimization)
    camera_name: optional name of camera to adjust thresholds dynamically
    returns: (user_or_none, similarity)
    """
    threshold = FACE_COSINE_THRESHOLD
    if camera_name and any(kw in camera_name.lower() for kw in ["kirish", "eshik", "entrance", "gate", "exit"]):
        threshold = 0.42  # standard threshold for entrance cameras to prevent false positives while allowing minor angle variations

    try:
        allowed_ids = None
        if schedule_id is not None:
            allowed_ids = get_allowed_ids_for_schedule(schedule_id)

        # Try Qdrant matching first if enabled
        if _qdrant_store is not None:
            try:
                qdrant_group_id = None
                if schedule_id is not None:
                    from camera.models import LessonSchedule
                    sched = LessonSchedule.objects.select_related('academic_group').filter(id=schedule_id).first()
                    if sched and sched.academic_group:
                        qdrant_group_id = sched.academic_group.id

                qdrant_res = _qdrant_store.search(
                    emb, 
                    limit=1, 
                    allowed_ids=allowed_ids,
                    group_id=qdrant_group_id
                )
                if qdrant_res:
                    user_id, sim, payload = qdrant_res[0]
                    if sim >= threshold:
                        found_user = None
                        with _faiss_lock:
                            if _faiss_users:
                                for u in _faiss_users:
                                    if u.id == user_id:
                                        found_user = u
                                        break
                        if found_user is None:
                            found_user = CustomUser.objects.filter(id=user_id).first()
                        if found_user:
                            logger.info("[QDRANT MATCH] Found matching user=%s score=%s (filtered=%s, threshold=%s)", found_user.id, sim, allowed_ids is not None, threshold)
                            return found_user, float(sim)
            except Exception as exc:
                logger.error("[QDRANT] Search failed: %s", exc)

        # Fallback to FAISS/NumPy search
        index, users_db = _load_index()
        if index is None or not users_db:
            return None, 0.0

        # NumPy custom lookup for filtered IDs (extremely fast when allowed_ids is small)
        if allowed_ids is not None:
            indices = [i for i, u in enumerate(users_db) if u.id in allowed_ids]
            global _faiss_matrix
            mat = _faiss_matrix if _faiss_matrix is not None else index
            best_sim = -1.0
            best_user = None
            if indices and isinstance(mat, np.ndarray) and mat.size > 0:
                for idx in indices:
                    if idx < mat.shape[0]:
                        sim = float(np.dot(mat[idx], emb.astype(np.float32)))
                        if sim > best_sim:
                            best_sim = sim
                            best_user = users_db[idx]
                if best_user and best_sim >= threshold:
                    logger.info("[FILTERED NUMPY MATCH] Best match user=%s name=%s similarity=%s (threshold=%s)", best_user.id, best_user.full_name or best_user.username, best_sim, threshold)
                    return best_user, best_sim
            # Do NOT fall back to regular search if allowed_ids is specified (enforce dars filtering)
            return None, best_sim

        # Regular search if no schedule filtering is applied
        res = _search_best(emb)
        if res is None:
            return None, 0.0

        sim, idx = res
        if 0 <= idx < len(users_db):
            matched_user = users_db[idx]
            logger.info("[FAISS MATCH] Best match user=%s name=%s similarity=%s (threshold=%s, camera=%s)", matched_user.id, matched_user.full_name or matched_user.username, sim, threshold, camera_name)
            if sim >= threshold:
                return matched_user, float(sim)

        return None, float(sim)
    finally:
        connections.close_all()


# =========================
# Attendance update + photo
# =========================

def process_recognition_sync(user: CustomUser, face_crop: np.ndarray, camera: Optional[Camera] = None, attendance_status: Optional[str] = None) -> None:
    """
    Raqamli yuz tanish davomatini Celery orqali fon rejimida qayd etish.
    """
    import base64
    from camera.tasks import process_face_recognition_task

    try:
        now_dt = timezone.now()

        # in-memory last seen yangilash
        _remember_seen(user.id, now_dt)

        # 1. Throttling tekshirish (In-memory dict orqali daemon xotirasida)
        allow_db = _allow_action(_last_db_update, user.id, USER_DB_UPDATE_COOLDOWN_S)
        allow_photo = _allow_action(_last_photo_time, user.id, PHOTO_INTERVAL_S)

        # Agar ikkala amal ham faollik oralig'ida (cooldown) bo'lsa - chiqib ketamiz
        if not allow_db and not allow_photo:
            return

        # 2. Rasm kadrini base64 formatiga o'tkazish (faqat rasm saqlash ruxsat berilgan bo'lsa)
        crop_b64 = None
        if allow_photo and face_crop is not None and face_crop.size > 0:
            ok, buffer = cv2.imencode(".jpg", face_crop, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            if ok:
                crop_b64 = base64.b64encode(buffer.tobytes()).decode('utf-8')

        # 3. Celery taskni ishga tushiramiz (hech qanday DB blokirovkasisiz)
        process_face_recognition_task.delay(
            user_id=user.id,
            crop_b64=crop_b64,
            camera_id=camera.id if camera else None,
            attendance_status=attendance_status,
            update_db=allow_db,
            save_photo=allow_photo
        )
    except Exception as exc:
        logger.exception("[RTSP Recognition Proxy] Failed to delegate to Celery: %s", exc)
    finally:
        connections.close_all()


def mark_exit_sync(user: CustomUser, when: Optional[timezone.datetime] = None) -> None:
    try:
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
    finally:
        connections.close_all()

# =========================
# Exit detector (async-safe)
# =========================

@sync_to_async
def _close_stale_attendances_sync(now_dt, cutoff, today) -> int:
    """
    DB-based exit:
    last_seen cutoff'dan eski bo'lgan is_present=True attendancelarni yopadi.
    """
    try:
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
    finally:
        connections.close_all()


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