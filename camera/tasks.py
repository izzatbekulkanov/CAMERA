# camera/tasks.py
import logging
import base64
import os
import uuid
import tempfile
import cv2
import numpy as np
import requests
from celery import shared_task
from django.utils import timezone
from django.db import transaction, connections
from django.core.files.base import ContentFile

from camera.models import Camera, UnknownFaceLog
from attendance.models import Attendance, AttendancePhoto
from users.models import CustomUser

logger = logging.getLogger(__name__)


@shared_task
def check_camera_health() -> None:
    """
    Celery beat orqali kameralarning ulanishini tekshiradi.
    """
    cams = Camera.objects.filter(is_active=True)

    for cam in cams:
        ok = False
        url = f"http://{cam.ip}:{cam.port}/"

        try:
            r = requests.get(url, auth=(cam.username, cam.password), timeout=5)
            if r.status_code in (200, 401, 302):
                ok = True
        except Exception as exc:
            logger.warning("[HEALTH] %s ulanmagan: %s", cam.ip, exc)

        cam.last_checked = timezone.now()
        update_fields = ["last_checked"]

        if hasattr(cam, "is_online"):
            if getattr(cam, "is_online") != ok:
                setattr(cam, "is_online", ok)
                update_fields.append("is_online")

        cam.save(update_fields=update_fields)


@shared_task
def process_face_recognition_task(user_id: int, crop_b64: str = None, camera_id: int = None, update_db: bool = True, save_photo: bool = True, attendance_status: str = None) -> None:
    """
    Face recognition uchun davomat yozish va rasmni fon rejimida saqlash (Celery task).
    """
    try:
        user = CustomUser.objects.get(pk=user_id)
    except CustomUser.DoesNotExist:
        logger.warning("[Celery Attendance] User not found: %s", user_id)
        return

    camera = None
    if camera_id:
        try:
            camera = Camera.objects.get(pk=camera_id)
        except Camera.DoesNotExist:
            pass

    now_dt = timezone.now()
    today = timezone.localdate()

    att = None
    if update_db:
        try:
            with transaction.atomic():
                is_exit_only = bool(camera and camera.is_exit_camera and not camera.is_entry_camera)
                if attendance_status == "checkOut":
                    is_exit_only = True
                elif attendance_status == "checkIn":
                    is_exit_only = False
                att, created = Attendance.objects.get_or_create(
                    user=user,
                    date=today,
                    defaults={
                        "entry_time": now_dt if not is_exit_only else None,
                        "exit_time": now_dt if is_exit_only else None,
                        "last_seen": now_dt,
                        "is_present": not is_exit_only,
                        "entry_camera": camera if not is_exit_only else None,
                        "exit_camera": camera if is_exit_only else None,
                        "last_seen_camera": camera,
                        "detection_count": 1
                    },
                )
                if not created:
                    if is_exit_only:
                        att.exit_time = now_dt
                        att.is_present = False
                        att.exit_camera = camera
                        if att.entry_time:
                            att.duration_minutes = max(1, int((now_dt - att.entry_time).total_seconds() // 60))
                        att.last_seen = now_dt
                        att.last_seen_camera = camera
                        att.detection_count += 1
                        att.save(update_fields=["exit_time", "last_seen", "last_seen_camera", "is_present", "exit_camera", "duration_minutes", "detection_count"])
                    else:
                        if not att.entry_time:
                            att.entry_time = now_dt
                            att.entry_camera = camera
                        att.last_seen = now_dt
                        att.last_seen_camera = camera
                        att.is_present = True
                        att.detection_count += 1
            logger.info("[Celery Attendance] Updated attendance for user=%s (exit=%s)", user_id, is_exit_only)

            # Live Attendance WS broadcast to frontend UI
            try:
                from channels.layers import get_channel_layer
                from asgiref.sync import async_to_sync
                channel_layer = get_channel_layer()
                if channel_layer:
                    async_to_sync(channel_layer.group_send)(
                        "live_attendance_events",
                        {
                            "type": "face_detected_event",
                            "camera_id": camera.id if camera else None,
                            "user_id": user.id,
                            "full_name": user.full_name or user.username or "Noma’lum",
                            "photo_url": user.image.url if getattr(user, 'image', None) else None,
                            "role": getattr(user, 'role', 'Foydalanuvchi'),
                            "entry_time": att.entry_time.strftime("%H:%M:%S") if (att and att.entry_time) else now_dt.strftime("%H:%M:%S"),
                            "last_seen_iso": now_dt.isoformat(),
                            "is_present": not is_exit_only
                        }
                    )
            except Exception as b_err:
                logger.debug("[Celery Broadcast] failed: %s", b_err)
        except Exception as exc:
            logger.exception("[Celery Attendance] Update failed user=%s: %s", user_id, exc)
            return

    # Agar rasm saqlash kerak bo'lsa va crop berilgan bo'lsa
    if save_photo and crop_b64:
        try:
            if not att:
                # Agar ushbu taskda update_db bajarilmagan bo'lsa (muntazam throttled holatda bo'lishi mumkin)
                # joriy kundagi Attendance obyektini olamiz
                att = Attendance.objects.filter(user=user, date=today).first()

            if att:
                img_data = base64.b64decode(crop_b64)
                filename = f"{user.username or 'user'}_{uuid.uuid4().hex[:8]}.jpg"
                photo = AttendancePhoto(attendance=att)
                photo.image.save(filename, ContentFile(img_data))
                logger.info("[Celery Photo] Saved photo for user=%s file=%s", user_id, filename)

                # Psychological analysisni ishga tushiramiz
                try:
                    from attendance.tasks import analyze_attendance_psychology
                    analyze_attendance_psychology.delay(att.id)
                except Exception as exc:
                    logger.error("[Celery Psychology] Trigger failed for user=%s: %s", user_id, exc)
        except Exception as exc:
            logger.exception("[Celery Photo] Save failed user=%s: %s", user_id, exc)
    
    # DB ulanishlarini tozalash
    connections.close_all()


@shared_task
def save_unknown_face_task(camera_id: int, crop_b64: str, embedding: list) -> None:
    """
    Noma'lum shaxs yuzini fon rejimida saqlash va embeddingini yozish (Celery task).
    """
    try:
        camera = Camera.objects.filter(id=camera_id).first()
        if not camera:
            logger.warning("[Celery Unknown Face] Camera not found: %s", camera_id)
            return

        # Image parsing
        img_data = base64.b64decode(crop_b64)
        filename = f"unknown_{uuid.uuid4().hex[:8]}.jpg"
        
        log = UnknownFaceLog(
            camera=camera,
            encoding_data=embedding
        )
        log.image.save(filename, ContentFile(img_data), save=True)
        logger.info("[Celery Unknown Face] Successfully saved unknown face: camera=%s file=%s", camera_id, filename)
    except Exception as exc:
        logger.exception("[Celery Unknown Face] Failed to save unknown face: %s", exc)
    finally:
        connections.close_all()


@shared_task
def cluster_unknown_faces_task() -> str:
    """
    DBSCAN yordamida noma'lum yuzlarni klasterlaydi va guruhlaydi.
    """
    from sklearn.cluster import DBSCAN
    from camera.models import UnknownFaceLog, UnknownFaceCluster
    from django.db.models import Q
    import numpy as np

    try:
        # Faqat foydalanuvchiga bog'lanmagan klasterlardagi yuzlarni qayta guruhlaymiz
        logs = UnknownFaceLog.objects.filter(
            Q(cluster__isnull=True) | Q(cluster__associated_user__isnull=True)
        ).select_related('cluster')

        if not logs.exists():
            return "No unknown faces to cluster."

        log_list = list(logs)
        embeddings = []
        valid_logs = []

        for log in log_list:
            if log.encoding_data and len(log.encoding_data) == 512:
                vec = np.array(log.encoding_data, dtype=np.float32)
                # Normallashtirish
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                embeddings.append(vec)
                valid_logs.append(log)

        N = len(embeddings)
        if N < 3:
            return f"Too few faces to cluster: N={N} (minimum 3 required)"

        X = np.stack(embeddings, axis=0)

        # DBSCAN: eps=0.42 (similarity >= 0.58) yaqinligi uchun eng optimal chegara
        dbscan = DBSCAN(eps=0.42, min_samples=3, metric='cosine')
        labels = dbscan.fit_predict(X)

        unique_labels = set(labels)
        
        # Klasterlarni yangilash tranzaksiyasi
        with transaction.atomic():
            # Avvalgi bog'lanmagan klasterlarni tozalaymiz
            old_clusters = UnknownFaceCluster.objects.filter(associated_user__isnull=True)
            old_clusters.delete()

            # Yangi klasterlar yaratamiz
            clusters_map = {}
            for label in unique_labels:
                if label == -1:
                    continue
                cluster = UnknownFaceCluster.objects.create(
                    name=f"Noma'lum Guruh #{uuid.uuid4().hex[:6].upper()}"
                )
                clusters_map[label] = cluster

            # Yuzlarni klasterlarga biriktiramiz
            for idx, label in enumerate(labels):
                log = valid_logs[idx]
                if label == -1:
                    log.cluster = None
                else:
                    log.cluster = clusters_map[label]
                log.save(update_fields=['cluster'])

        return f"Clustering completed: N={N} faces, grouped into {len(clusters_map)} clusters."
    except Exception as exc:
        logger.exception("[Celery Clustering] Failed to cluster unknown faces: %s", exc)
        return f"Error: {exc}"
    finally:
        connections.close_all()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def hemis_sync_schedules_task(self, payload: dict):
    """
    HEMIS tizimidan dars jadvallarini yuklash asinxron vazifasi.
    payload: {
        "group_id": int | None,
        "mode": "all" | "group"
    }
    """
    from users.view import progress as prog
    from attendance.models import SiteSettings
    from users.models import AcademicGroup
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    from datetime import datetime
    import requests

    sync_type = "schedules"
    prog.reset(sync_type, 0, message="HEMIS dars jadvallari sinxronizatsiyasi boshlandi...")

    def parse_time(time_str):
        if not time_str:
            return None
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(time_str.strip(), fmt).time()
            except ValueError:
                continue
        return None

    try:
        settings_obj = SiteSettings.get_settings()
        if not settings_obj or not settings_obj.hemis_url or not settings_obj.hemis_api_token:
            raise ValueError("HEMIS sozlamalari topilmadi. Sayt sozlamalarida HEMIS URL va Tokenni kiriting.")

        base_url = f"{settings_obj.hemis_url.rstrip('/')}/rest/v1/data/schedule-list"
        headers = {
            "Authorization": f"Bearer {settings_obj.hemis_api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

        mode = (payload or {}).get("mode", "all")
        group_id = (payload or {}).get("group_id")

        params = {"page": 1, "limit": 200}
        if mode == "group" and group_id:
            params["_academic_group"] = str(group_id)

        first_resp = requests.get(base_url, headers=headers, params=params, timeout=30)
        first_resp.raise_for_status()
        first_data = first_resp.json() or {}

        if not first_data.get("success"):
            raise ValueError("HEMIS success=False qaytardi")

        block = first_data.get("data") or {}
        pagination = block.get("pagination") or {}
        page_count = int(pagination.get("pageCount") or 1)
        total_items = int(pagination.get("totalCount") or 0)

        prog.reset(sync_type, total_items, message="Ma'lumotlar o'qilmoqda...")

        created_count = 0
        updated_count = 0
        processed_count = 0

        for page in range(1, page_count + 1):
            params["page"] = page
            resp = requests.get(base_url, headers=headers, params=params, timeout=30)
            if resp.status_code != 200:
                continue
            
            page_data = resp.json() or {}
            items = (page_data.get("data") or {}).get("items") or []
            if not isinstance(items, list):
                continue

            with transaction.atomic():
                for item in items:
                    processed_count += 1
                    
                    # 1. Parse Group
                    group_data = item.get("academic_group") or item.get("group") or item.get("_academic_group")
                    if not group_data or not isinstance(group_data, dict):
                        continue
                    group_name = (group_data.get("name") or "").strip()
                    if not group_name:
                        continue
                    
                    group_obj, _ = AcademicGroup.objects.get_or_create(
                        name=group_name
                    )

                    # 2. Parse Subject
                    subject_data = item.get("subject") or item.get("_subject")
                    if not subject_data or not isinstance(subject_data, dict):
                        continue
                    subject_name = (subject_data.get("name") or "").strip()
                    subject_name = subject_name.replace("‘", "'").replace("’", "'").replace("`", "'").replace("'", "'").strip()
                    if not subject_name:
                        continue
                    subject_code = (subject_data.get("code") or "").strip()
                    
                    subject_obj, _ = Subject.objects.get_or_create(
                        name=subject_name,
                        defaults={"code": subject_code, "is_active": True}
                    )

                    # 3. Parse Auditorium
                    aud_data = item.get("auditorium") or item.get("_auditorium")
                    if not aud_data or not isinstance(aud_data, dict):
                        continue
                    aud_name = (aud_data.get("name") or "").strip()
                    if not aud_name:
                        continue
                    
                    auditorium_obj, _ = Auditorium.objects.get_or_create(
                        name=aud_name,
                        defaults={"is_active": True}
                    )

                    # 4. Parse Weekday
                    weekday_data = item.get("week_day") or item.get("weekDay") or item.get("weekday") or item.get("_week_day")
                    weekday_code = None
                    if isinstance(weekday_data, dict):
                        weekday_code = weekday_data.get("code")
                    elif isinstance(weekday_data, (str, int)):
                        weekday_code = weekday_data
                    
                    try:
                        weekday_val = int(weekday_code)
                    except (ValueError, TypeError):
                        continue
                    
                    if weekday_val not in range(1, 7):
                        continue

                    # 5. Parse Pair info
                    pair_data = item.get("pair") or item.get("lesson_pair") or item.get("_lesson_pair")
                    if not pair_data or not isinstance(pair_data, dict):
                        continue
                    
                    pair_number_raw = pair_data.get("number") or pair_data.get("id") or pair_data.get("pair_number")
                    try:
                        pair_number = int(pair_number_raw)
                    except (ValueError, TypeError):
                        # try parsing from name e.g. "1-para"
                        pair_name = (pair_data.get("name") or "").strip()
                        import re
                        match = re.search(r'\d+', pair_name)
                        if match:
                            pair_number = int(match.group())
                        else:
                            continue
                    
                    start_time_str = pair_data.get("start_time") or pair_data.get("startTime")
                    end_time_str = pair_data.get("end_time") or pair_data.get("endTime")
                    
                    start_time = parse_time(start_time_str) or parse_time("08:30:00")
                    end_time = parse_time(end_time_str) or parse_time("09:50:00")

                    pair_obj, _ = LessonPair.objects.get_or_create(
                        shift=1,
                        pair_number=pair_number,
                        defaults={
                            "start_time": start_time,
                            "end_time": end_time
                        }
                    )

                    # 6. Parse Teacher Name
                    teacher_data = item.get("employee") or item.get("teacher") or item.get("_employee")
                    teacher_name = ""
                    if isinstance(teacher_data, dict):
                        teacher_name = (teacher_data.get("name") or "").strip()
                    elif isinstance(teacher_data, str):
                        teacher_name = teacher_data.strip()

                    # 7. Save LessonSchedule
                    schedule_obj, created_s = LessonSchedule.objects.update_or_create(
                        academic_group=group_obj,
                        weekday=weekday_val,
                        lesson_pair=pair_obj,
                        defaults={
                            "subject": subject_obj,
                            "auditorium": auditorium_obj,
                            "teacher_name": teacher_name or None
                        }
                    )
                    
                    if created_s:
                        created_count += 1
                    else:
                        updated_count += 1

            prog.update(sync_type, processed_count, total_items, f"Jadvallar: +{created_count} ta yangi, ~{updated_count} ta yangilandi")

        prog.update(sync_type, total_items, total_items, f"Muvaffaqiyatli yakunlandi! Jami: {processed_count} ta dars jadvali sinxron qilindi.")
        return f"Schedules synced: created={created_count}, updated={updated_count}"

    except Exception as e:
        logger.exception("[HEMIS Schedule Sync] Error: %s", e)
        prog.error(sync_type, f"Xatolik yuz berdi: {str(e)}")
        return f"Error: {e}"
    finally:
        connections.close_all()
