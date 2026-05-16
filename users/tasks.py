# users/tasks.py
import uuid
import os

try:
    from celery import shared_task
except ImportError:
    def shared_task(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

from users.models import CustomUser, FaceEncoding

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None
from django.conf import settings
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.db.models import Exists, OuterRef
from django.core.files.base import ContentFile
from django.db import transaction, IntegrityError

from attendance.models import SiteSettings
from users.models import CustomUser
from django.core.cache import cache
from django.utils import timezone

from camera.device import get_face_runtime

# progress cache helpers (sizda users/view/progress.py ichida)
from users.view import progress as prog

logger = logging.getLogger(__name__)

MODEL_VERSION = "insightface_buffalo_l"


def _should_skip_encoding(user: CustomUser) -> tuple[bool, str]:
    """
    True qaytsa -> encoding yaratishni skip qilamiz.
    Qoidalar:
      - encoding yo'q -> yaratish kerak
      - encoding bor:
          - user.updated_at <= enc.created_at -> rasm/foydalanuvchi yangilanmagan -> skip
          - user.updated_at  > enc.created_at -> yangilash kerak
    """
    enc = (
        FaceEncoding.objects
        .filter(user_id=user.id, model_version=MODEL_VERSION)
        .only("id", "created_at")
        .first()
    )
    if not enc:
        return False, "encoding yo'q (create)"

    # user.updated_at rasm o‘zgarsa ham yangilanadi deb qabul qilamiz
    user_updated = getattr(user, "updated_at", None)
    if user_updated and enc.created_at and user_updated <= enc.created_at:
        return True, "oldin yaratilgan (skip)"

    # yangilash kerak
    return False, "rasm yangilangan (refresh)"


try:
    from insightface.app import FaceAnalysis

    face_runtime = get_face_runtime()
    print("[INSIGHTFACE] Model yuklanmoqda (buffalo_l, Celery)...")

    app = FaceAnalysis(
        name="buffalo_l",
        providers=face_runtime["providers"]
    )
    app.prepare(ctx_id=face_runtime["ctx_id"], det_size=(640, 640))
    print(f"[INSIGHTFACE] Model yuklandi! (Celery worker, {face_runtime['device_type'].upper()} mode)")
except Exception as e:
    print(f"[INSIGHTFACE XATO][Celery] {e}")
    app = None


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_insightface_encoding(self, user_id: int, force: bool = False, run_id: str | None = None):
    """
    Batch progressni buzmaslik uchun:
    - run_id berilgan bo'lsa -> progress tick/update qilamiz
    - run_id berilmagan bo'lsa -> progressga umuman tegmaymiz (silent)
    """
    def tick(step=1, message=""):
        if run_id:  # ✅ faqat batch run bo'lsa
            face_progress_tick(step=step, message=message)

    def err(message=""):
        if run_id:
            face_progress_error(message)

    if app is None:
        tick(step=1, message="Model yuklanmagan (skip)")
        return f"[ERROR] user {user_id} — InsightFace modeli yuklanmagan"

    if cv2 is None or np is None:
        tick(step=1, message="Kutubxonalar yo'q (cv2/numpy)")
        return f"[ERROR] user {user_id} — cv2 yoki numpy o'rnatilmagan"

    session = _make_session()

    try:
        user = (
            CustomUser.objects
            .only("id", "image", "updated_at")
            .get(id=user_id)
        )

        if not user.image or not user.image.name:
            tick(step=1, message="Rasm yo'q (skip)")
            return f"[SKIP] user {user_id} — rasm yo‘q"

        if not force:
            skip, reason = _should_skip_encoding(user)
            if skip:
                tick(step=1, message=f"Skip: {reason}")
                return f"[SKIP] user {user_id} — {reason}"

        img = None
        try:
            if hasattr(user.image, "path") and user.image.path and os.path.exists(user.image.path):
                img = cv2.imread(user.image.path)
        except Exception:
            img = None

        if img is None:
            image_url = user.image.url or ""
            if image_url.startswith("/media/"):
                rel_path = user.image.name
                path = os.path.join(settings.MEDIA_ROOT, rel_path)
                if not os.path.exists(path):
                    tick(step=1, message="Fayl topilmadi (skip)")
                    return f"[ERROR] user {user_id} — Fayl topilmadi: {path}"
                img = cv2.imread(path)
                if img is None:
                    tick(step=1, message="cv2.imread None (skip)")
                    return f"[ERROR] user {user_id} — Rasmni o‘qib bo‘lmadi (cv2.imread None)"
            else:
                try:
                    resp = session.get(image_url, timeout=(5, 45))
                except Exception as ex:
                    tick(step=1, message="HTTP timeout/xato (skip)")
                    return f"[ERROR] user {user_id} — HTTP xato: {ex}"

                if resp.status_code != 200:
                    tick(step=1, message=f"HTTP {resp.status_code} (skip)")
                    return f"[ERROR] user {user_id} — HTTP {resp.status_code}"

                img_bytes = np.frombuffer(resp.content, np.uint8)
                img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
                if img is None:
                    tick(step=1, message="HTTP rasm ochilmadi (skip)")
                    return f"[ERROR] user {user_id} — HTTP rasmni ochib bo‘lmadi"

        if img is None or not hasattr(img, "shape"):
            tick(step=1, message="Rasm None (skip)")
            return f"[ERROR] user {user_id} — Rasmni o‘qib bo‘lmadi"

        faces = app.get(img)
        if not faces:
            tick(step=1, message="Yuz topilmadi (skip)")
            return f"[WARN] user {user_id} — yuz topilmadi"

        best_face = max(faces, key=lambda x: float(x.det_score or 0.0))
        conf = float(best_face.det_score or 0.0)

        if conf < 0.3:
            tick(step=1, message="Confidence past (skip)")
            return f"[WARN] user {user_id} — ishonchlilik past ({conf:.2f})"

        embedding_512d = best_face.normed_embedding
        if embedding_512d is None or len(embedding_512d) == 0:
            tick(step=1, message="Embedding yo'q (skip)")
            return f"[ERROR] user {user_id} — embedding olinmadi"

        try:
            with transaction.atomic():
                FaceEncoding.objects.update_or_create(
                    user_id=user.id,
                    model_version=MODEL_VERSION,
                    defaults={
                        "encoding_data": embedding_512d.tolist(),
                        "confidence": conf,
                    },
                )
        except IntegrityError:
            with transaction.atomic():
                FaceEncoding.objects.filter(user_id=user.id, model_version=MODEL_VERSION).update(
                    encoding_data=embedding_512d.tolist(),
                    confidence=conf,
                )

        tick(step=1, message="Encoding yaratildi")
        return f"[SUCCESS] user {user_id} — encoding yaratildi (conf: {conf:.3f})"

    except CustomUser.DoesNotExist:
        tick(step=1, message="User topilmadi (skip)")
        return f"[ERROR] user {user_id} — user topilmadi"

    except Exception as e:
        err(str(e))
        raise self.retry(exc=e)


FACE_PROGRESS_KEY = "face_encoding_progress_v1"
FACE_PROGRESS_TOTAL_KEY = "face_encoding_progress_total_v1"
FACE_PROGRESS_TTL = 60 * 60  # 1 soat


def _now():
    return timezone.now().isoformat()


def face_progress_reset(total: int, message: str = "Face encoding boshlandi"):
    total = int(total or 0)
    run_id = uuid.uuid4().hex
    now = _now()

    cache.set(FACE_PROGRESS_TOTAL_KEY, total, timeout=FACE_PROGRESS_TTL)
    cache.set(
        FACE_PROGRESS_KEY,
        {
            "status": "running",
            "processed": 0,
            "total": total,
            "message": message,
            "updated_at": now,
            "started_at": now,
            "run_id": run_id,
        },
        timeout=FACE_PROGRESS_TTL,
    )


def _get_total_fallback(raw: dict, processed: int) -> int:
    """
    total=0 bo'lib qolsa:
    1) total key'dan olib kelamiz
    2) agar u ham 0 bo'lsa va processed>0 bo'lsa -> total=processed (UI 103/103 bo'lib turadi)
    """
    total = int((raw or {}).get("total") or 0)
    if total <= 0:
        total = int(cache.get(FACE_PROGRESS_TOTAL_KEY) or 0)
    if total <= 0 and processed > 0:
        total = processed
    return total


def face_progress_tick(step: int = 1, message: str = ""):
    raw = cache.get(FACE_PROGRESS_KEY) or {}
    processed = int(raw.get("processed") or 0) + int(step or 0)

    total = _get_total_fallback(raw, processed)

    # face_progress_tick ichida:
    status = raw.get("status") or "running"
    if status in ("completed", "error", "idle"):
        status = "running"

    # ✅ run_id/started_at bo‘lmasa ham generatsiya qilamiz
    run_id = raw.get("run_id") or uuid.uuid4().hex
    started_at = raw.get("started_at") or _now()

    payload = {
        "status": status,
        "processed": processed,
        "total": total,
        "message": message or (raw.get("message") or ""),
        "updated_at": _now(),
        "started_at": started_at,
        "run_id": run_id,
    }

    # complete
    if total and processed >= total:
        payload["status"] = "completed"
        payload["processed"] = total
        if not payload["message"]:
            payload["message"] = "Yakunlandi"

    cache.set(FACE_PROGRESS_KEY, payload, timeout=FACE_PROGRESS_TTL)
    cache.set(FACE_PROGRESS_TOTAL_KEY, int(total or 0), timeout=FACE_PROGRESS_TTL)


def face_progress_error(err: str):
    raw = cache.get(FACE_PROGRESS_KEY) or {}
    processed = int(raw.get("processed") or 0)
    total = _get_total_fallback(raw, processed)

    cache.set(
        FACE_PROGRESS_KEY,
        {
            "status": "error",
            "processed": processed,
            "total": total,
            "message": str(err),
            "updated_at": _now(),
            "started_at": raw.get("started_at"),
            "run_id": raw.get("run_id"),
        },
        timeout=FACE_PROGRESS_TTL,
    )
    cache.set(FACE_PROGRESS_TOTAL_KEY, int(total or 0), timeout=FACE_PROGRESS_TTL)


@shared_task(bind=True)
def start_face_encoding_batch(self):
    s = SiteSettings.get_settings()
    if not getattr(s, "enable_auto_face_encoding", False):
        cache.set(
            FACE_PROGRESS_KEY,
            {
                "status": "idle",
                "processed": 0,
                "total": 0,
                "message": "Auto face encoding o‘chirilgan",
                "updated_at": timezone.now().isoformat(),
                "started_at": None,
                "run_id": None,
            },
            timeout=FACE_PROGRESS_TTL,
        )
        cache.set(FACE_PROGRESS_TOTAL_KEY, 0, timeout=FACE_PROGRESS_TTL)
        return {"success": False, "message": "Auto face encoding o‘chirilgan"}

    # ✅ buffalo_l encoding bor-yo‘qligini EXISTS bilan tekshiramiz (JOIN/duplicate muammosiz)
    has_model_enc = FaceEncoding.objects.filter(
        user_id=OuterRef("pk"),
        model_version=MODEL_VERSION,
    )

    qs = (
        CustomUser.objects
        .filter(image__isnull=False)
        .exclude(image="")
        .exclude(image__exact="")
        .annotate(_has_enc=Exists(has_model_enc))
        .filter(_has_enc=False)
        .only("id")
    )

    total = qs.count()

    # ✅ progress reset
    face_progress_reset(total=total, message=f"Navbatga qo‘yildi: {total} ta")

    raw = cache.get(FACE_PROGRESS_KEY) or {}
    run_id = raw.get("run_id")

    # total=0 bo'lsa UI "completed" bo'lib turishi uchun
    if total == 0:
        face_progress_tick(step=0, message="Encoding kerak bo‘lgan user topilmadi")
        return {"success": True, "total": 0, "message": "Encoding kerak bo‘lgan user topilmadi"}

    # ✅ Celery’ga yuboramiz
    for u in qs.iterator(chunk_size=300):
        create_insightface_encoding.delay(u.id, force=False, run_id=run_id)

    return {"success": True, "total": total, "run_id": run_id}


IMG_MIN_BYTES = 200


def _safe_str(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    return s if s else None


def _ts_to_date(ts: Any):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts)).date()
    except Exception:
        return None


def _make_session() -> requests.Session:
    """
    Celery task ichida requests'larni barqaror qilish uchun:
    - connection pool
    - retry/backoff
    - connect/read timeoutlarni alohida boshqaramiz
    """
    s = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _get_hemis_settings() -> SiteSettings:
    s = SiteSettings.objects.first()
    if not s or not s.hemis_url or not s.hemis_api_token:
        raise ValueError("HEMIS sozlamalari topilmadi")
    return s


def _hemis_headers(token: str) -> Dict[str, str]:
    return {"accept": "application/json", "Authorization": f"Bearer {token}"}


def _fetch_image_bytes(session: requests.Session, url: str, *, timeout=(5, 45)) -> Optional[bytes]:
    """
    Timeout:
      - connect 5s
      - read 45s
    """
    if not url:
        return None

    headers = {
        "Accept": "image/*,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0",
    }

    try:
        r = session.get(url, timeout=timeout, allow_redirects=True, headers=headers)
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status_code != 200:
            return None
        if "text/html" in ctype:
            return None
        content = r.content or b""
        if len(content) < IMG_MIN_BYTES:
            return None
        return content
    except Exception:
        return None


def _save_user_image(user: CustomUser, img_bytes: bytes, *, overwrite: bool) -> bool:
    if not img_bytes:
        return False
    if (not overwrite) and user.image and user.image.name:
        return False
    try:
        # filename: username.jpg (xohlasangiz uuid qo‘shamiz)
        filename = f"{user.username}.jpg"
        user.image.save(filename, ContentFile(img_bytes), save=True)
        return True
    except Exception:
        logger.exception("IMAGE SAVE FAIL user_id=%s", user.id)
        return False


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def hemis_sync_students_task(self, payload: dict):
    """
    payload:
      {
        "type": "group" | null,
        "group_id": 123 | null,
        "download_images": true|false,
        "overwrite_images": true|false
      }
    """
    sync_type = "students"
    session = _make_session()

    try:
        s = _get_hemis_settings()
        base_url = f"{s.hemis_url.rstrip('/')}/rest/v1/data/student-list"
        headers = _hemis_headers(s.hemis_api_token)

        mode = (payload or {}).get("type")
        group_id = (payload or {}).get("group_id")
        download_images = bool((payload or {}).get("download_images", True))
        overwrite_images = bool((payload or {}).get("overwrite_images", False))

        params = {"page": 1, "limit": 200}
        if mode == "group" and group_id:
            params["_group"] = str(group_id)

        first = session.get(base_url, headers=headers, params=params, timeout=(5, 45))
        first.raise_for_status()
        p = first.json() or {}
        if not p.get("success"):
            raise ValueError("HEMIS success=False")

        pagination = (p.get("data") or {}).get("pagination") or {}
        page_count = int(pagination.get("pageCount") or 1)
        total_items = int(pagination.get("totalCount") or 0)

        prog.reset(sync_type, total_items, message="Talabalar sync task boshlandi")

        created = updated = images_saved = 0
        processed = 0

        for page in range(1, page_count + 1):
            params["page"] = page
            resp = session.get(base_url, headers=headers, params=params, timeout=(5, 60))
            if resp.status_code != 200:
                continue

            items = ((resp.json() or {}).get("data") or {}).get("items") or []
            if not isinstance(items, list):
                items = []

            # Page ichida DB + image save
            with transaction.atomic():
                for st in items:
                    sid = _safe_str(st.get("student_id_number"))
                    if not sid:
                        continue

                    defaults = {
                        "username": f"s_{sid}",
                        "role": CustomUser.Role.STUDENT,
                        "full_name": st.get("full_name"),
                        "short_name": st.get("short_name"),
                        "first_name": st.get("first_name"),
                        "second_name": st.get("second_name"),
                        "third_name": st.get("third_name"),
                        "gender": (st.get("gender") or {}).get("name"),
                        "birth_date": _ts_to_date(st.get("birth_date")),
                        "student_id_number": sid,
                        "department_name": (st.get("department") or {}).get("name"),
                        "department_code": (st.get("department") or {}).get("code"),
                        "specialty": (st.get("specialty") or {}).get("name"),
                        "group_name": (st.get("group") or {}).get("name"),
                        "education_year": (st.get("educationYear") or {}).get("name"),
                        "gpa": st.get("avg_gpa"),
                        "year_of_enter": st.get("year_of_enter"),
                        "active": True,
                    }
                    defaults = {k: v for k, v in defaults.items() if v is not None}

                    user, is_created = CustomUser.objects.update_or_create(
                        student_id_number=sid,
                        defaults=defaults,
                    )
                    if is_created:
                        created += 1
                    else:
                        updated += 1

                    # ✅ encoding YO‘Q. Faqat rasm.
                    if download_images and st.get("image"):
                        img = _fetch_image_bytes(session, st.get("image"), timeout=(5, 60))
                        if img and _save_user_image(user, img, overwrite=overwrite_images):
                            images_saved += 1

                    processed += 1

                    if processed % 50 == 0:
                        prog.update(
                            sync_type,
                            processed=min(processed, total_items) if total_items else processed,
                            total=total_items,
                            message=f"Page {page}/{page_count} | processed={processed} | img_saved={images_saved}",
                        )

            prog.update(
                sync_type,
                processed=min(processed, total_items) if total_items else processed,
                total=total_items,
                message=f"Page {page}/{page_count} | processed={processed} | img_saved={images_saved}",
            )

        prog.update(sync_type, total_items if total_items else processed, total=total_items, message="Yakunlandi")

        return {
            "success": True,
            "created": created,
            "updated": updated,
            "images_saved": images_saved,
            "total": total_items,
        }

    except Exception as e:
        logger.exception("students task failed")
        prog.error(sync_type, str(e))
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def hemis_sync_employees_task(self, payload: dict):
    """
    payload:
      {
        "department_id": 123 | null,
        "download_images": true|false,
        "overwrite_images": true|false
      }
    """
    sync_type = "employees"
    session = _make_session()

    try:
        s = _get_hemis_settings()
        base_url = f"{s.hemis_url.rstrip('/')}/rest/v1/data/employee-list"
        headers = _hemis_headers(s.hemis_api_token)

        department_id = (payload or {}).get("department_id")
        download_images = bool((payload or {}).get("download_images", True))
        overwrite_images = bool((payload or {}).get("overwrite_images", False))

        params = {"type": "all", "page": 1, "limit": 200}
        if department_id:
            params["_department"] = str(department_id)

        first = session.get(base_url, headers=headers, params=params, timeout=(5, 45))
        first.raise_for_status()
        p = first.json() or {}
        if not p.get("success", True) and p.get("success") is not None:
            # ba’zi API lar success bo‘lmasligi mumkin; sizning HEMIS’da odatda success bor.
            raise ValueError("HEMIS success=False")

        pagination = (p.get("data") or {}).get("pagination") or {}
        page_count = int(pagination.get("pageCount") or 1)
        total_items = int(pagination.get("totalCount") or 0)

        prog.reset(sync_type, total_items, message="Xodimlar sync task boshlandi")

        created = updated = images_saved = 0
        processed = 0

        for page in range(1, page_count + 1):
            params["page"] = page
            resp = session.get(base_url, headers=headers, params=params, timeout=(5, 60))
            if resp.status_code != 200:
                continue

            items = ((resp.json() or {}).get("data") or {}).get("items") or []
            if not isinstance(items, list):
                items = []

            with transaction.atomic():
                for emp in items:
                    eid = _safe_str(emp.get("employee_id_number"))
                    if not eid:
                        continue

                    dept = emp.get("department") or {}

                    defaults = {
                        "username": f"e_{eid}",
                        "role": CustomUser.Role.EMPLOYEE,
                        "full_name": emp.get("full_name"),
                        "first_name": emp.get("first_name"),
                        "second_name": emp.get("second_name"),
                        "third_name": emp.get("third_name"),
                        "gender": (emp.get("gender") or {}).get("name"),
                        "birth_date": _ts_to_date(emp.get("birth_date")),
                        "year_of_enter": _safe_str(emp.get("year_of_enter")) or "",
                        "employee_id_number": eid,
                        "department_name": dept.get("name"),
                        "department_code": dept.get("code"),
                        "specialty": emp.get("specialty"),
                        "position": (emp.get("staffPosition") or {}).get("name"),
                        "active": bool(emp.get("active", True)),
                    }
                    defaults = {k: v for k, v in defaults.items() if v is not None}

                    user, is_created = CustomUser.objects.update_or_create(
                        employee_id_number=eid,
                        defaults=defaults,
                    )
                    if is_created:
                        created += 1
                    else:
                        updated += 1

                    # ✅ encoding YO‘Q. Faqat rasm.
                    if download_images and emp.get("image"):
                        img = _fetch_image_bytes(session, emp.get("image"), timeout=(5, 60))
                        if img and _save_user_image(user, img, overwrite=overwrite_images):
                            images_saved += 1

                    processed += 1

                    if processed % 50 == 0:
                        prog.update(
                            sync_type,
                            processed=min(processed, total_items) if total_items else processed,
                            total=total_items,
                            message=f"Page {page}/{page_count} | processed={processed} | img_saved={images_saved}",
                        )

            prog.update(
                sync_type,
                processed=min(processed, total_items) if total_items else processed,
                total=total_items,
                message=f"Page {page}/{page_count} | processed={processed} | img_saved={images_saved}",
            )

        prog.update(sync_type, total_items if total_items else processed, total=total_items, message="Yakunlandi")

        return {
            "success": True,
            "created": created,
            "updated": updated,
            "images_saved": images_saved,
            "total": total_items,
        }

    except Exception as e:
        logger.exception("employees task failed")
        prog.error(sync_type, str(e))
        raise self.retry(exc=e)
