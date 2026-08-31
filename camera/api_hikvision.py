import re
import json
import uuid
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction, connections
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from camera.models import Camera, FaceLog
from users.models import CustomUser

logger = logging.getLogger(__name__)


def _parse_hikvision_datetime(dt_str: Optional[str]) -> timezone.datetime:
    """Hikvision datetime formatlarini Django datetime formatiga o'tkazish."""
    if not dt_str:
        return timezone.now()
    
    try:
        # ISO 8601: 2026-08-18T11:45:30+05:00
        parsed = parse_datetime(dt_str)
        if parsed is not None:
            if timezone.is_naive(parsed):
                return timezone.make_aware(parsed)
            return parsed
    except Exception:
        pass

    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y%m%d%H%M%S",
        "%Y/%m/%d %H:%M:%S"
    ]:
        try:
            naive = datetime.strptime(dt_str.split('.')[0].replace('Z', ''), fmt)
            return timezone.make_aware(naive)
        except Exception:
            continue

    return timezone.now()


def _extract_xml_data(xml_text: str) -> Dict[str, Any]:
    """Hikvision XML EventNotificationAlert yoki FaceCapture XML ni parse qilish."""
    data = {}
    if not xml_text or not xml_text.strip():
        return data

    try:
        # Namespace prefikslarini tozalash
        clean_xml = re.sub(r'xmlns(:\w+)?="[^"]+"', '', xml_text)
        root = ET.fromstring(clean_xml)

        def get_text(tag_name: str) -> Optional[str]:
            elem = root.find(f".//{tag_name}")
            return elem.text.strip() if elem is not None and elem.text else None

        data['ip_address'] = get_text('ipAddress') or get_text('ipv4Address')
        data['mac_address'] = get_text('macAddress') or get_text('mac') or get_text('MAC') or get_text('deviceMac') or get_text('devMAC')
        data['serial_number'] = get_text('serialNumber') or get_text('subSerialNumber') or get_text('deviceSerial')
        data['device_id'] = get_text('deviceID') or get_text('channelName') or get_text('deviceName') or data.get('mac_address')
        data['channel_name'] = get_text('channelName') or get_text('deviceName')
        data['channel_id'] = get_text('channelID') or "1"
        data['date_time'] = get_text('dateTime') or get_text('faceTime') or get_text('time')
        data['event_type'] = get_text('eventType') or "faceCapture"
        data['event_description'] = get_text('eventDescription') or "faceCapture"
        
        # Face capture detallari
        score_str = get_text('faceScore') or get_text('similarity') or get_text('confidence')
        if score_str:
            try:
                data['confidence'] = float(score_str)
            except ValueError:
                data['confidence'] = 0.0

        age_str = get_text('age')
        if age_str and age_str.isdigit():
            data['age'] = int(age_str)

        data['gender'] = get_text('gender') or get_text('sex')

        glasses_str = (get_text('glasses') or "").lower()
        if glasses_str in ['yes', 'true', '1']:
            data['has_glasses'] = True
        elif glasses_str in ['no', 'false', '0']:
            data['has_glasses'] = False

        mask_str = (get_text('mask') or "").lower()
        if mask_str in ['yes', 'true', '1']:
            data['has_mask'] = True
        elif mask_str in ['no', 'false', '0']:
            data['has_mask'] = False

        # Face Rect (X, Y, width, height)
        face_rect = {}
        for coord in ['X', 'Y', 'width', 'height']:
            val = get_text(coord) or get_text(coord.lower())
            if val:
                try:
                    face_rect[coord.lower()] = float(val)
                except ValueError:
                    pass
        if face_rect:
            data['face_rect'] = face_rect

        data['raw_root_tag'] = root.tag
    except Exception as exc:
        logger.warning("[HIKVISION XML] XML parse xatoligi: %s", exc)

    return data


def _extract_multipart_body(request) -> Tuple[Dict[str, Any], Optional[bytes], Optional[bytes], Optional[bytes]]:
    """
    Kelayotgan multipart so'rovdan XML/JSON matnini va rasm fayllarini ajratib olish.
    Django request.FILES/POST yoki xom request.body dan o'qiydi.
    Qaytaradi: (event_data, face_image_bytes, bg_image_bytes, raw_body_bytes)
    """
    event_data: Dict[str, Any] = {}
    face_image_bytes: Optional[bytes] = None
    bg_image_bytes: Optional[bytes] = None
    raw_body: Optional[bytes] = None

    try:
        raw_body = request.body
    except Exception:
        pass

    # 1. Django request.FILES orqali fayllarni tekshirish
    if request.FILES:
        for file_key, uploaded_file in request.FILES.items():
            content = uploaded_file.read()
            uploaded_file.seek(0)
            
            # Agar fayl XML yoki JSON matn bo'lsa
            if content.startswith(b'<?xml') or content.startswith(b'<') or b'<EventNotificationAlert' in content or b'<FaceCapture' in content:
                try:
                    xml_str = content.decode('utf-8', errors='ignore')
                    event_data.update(_extract_xml_data(xml_str))
                    event_data['raw_text'] = xml_str
                except Exception:
                    pass
            elif content.startswith(b'{') and b'}' in content:
                try:
                    json_str = content.decode('utf-8', errors='ignore')
                    event_data.update(json.loads(json_str))
                except Exception:
                    pass
            elif content.startswith(b'\xff\xd8'):
                # JPEG rasm
                if not face_image_bytes:
                    face_image_bytes = content
                elif not bg_image_bytes:
                    bg_image_bytes = content

    # 2. Django request.POST orqali XML/JSON ma'lumotlarini tekshirish
    if request.POST:
        for key in ['event_log', 'facedetection', 'FaceCapture', 'EventNotificationAlert', 'data', 'json', 'upload']:
            if key in request.POST:
                raw_val = request.POST[key].strip()
                if raw_val.startswith('<'):
                    event_data.update(_extract_xml_data(raw_val))
                    event_data['raw_text'] = raw_val
                elif raw_val.startswith('{'):
                    try:
                        event_data.update(json.loads(raw_val))
                    except Exception:
                        pass
                break

    # 3. Agar Django standart multipart parsi ishlamagan bo'lsa, xom request.body dan ajratamiz
    if raw_body and (not face_image_bytes or not event_data):
        body = raw_body

        # JPEG rasmlarini topish (\xff\xd8\xff dan \xff\xd9 gacha)
        jpeg_starts = [m.start() for m in re.finditer(b'\xff\xd8\xff', body)]
        found_images = []
        for start_idx in jpeg_starts:
            end_idx = body.find(b'\xff\xd9', start_idx)
            if end_idx != -1:
                img_data = body[start_idx:end_idx + 2]
                if len(img_data) > 1000: # Kamida 1KB bo'lgan rasm
                    found_images.append(img_data)

        if found_images:
            if not face_image_bytes and len(found_images) >= 1:
                face_image_bytes = found_images[0]
            if not bg_image_bytes and len(found_images) >= 2:
                bg_image_bytes = found_images[1]

        # XML / JSON matnini topish
        if not event_data:
            xml_match = re.search(rb'<EventNotificationAlert[\s\S]*?</EventNotificationAlert>', body) or \
                        re.search(rb'<[\w:]*?FaceCapture[\s\S]*?</[\w:]*?FaceCapture>', body) or \
                        re.search(rb'<\?xml[\s\S]*?</[\w:]+>', body)
            if xml_match:
                try:
                    xml_str = xml_match.group(0).decode('utf-8', errors='ignore')
                    event_data.update(_extract_xml_data(xml_str))
                    event_data['raw_text'] = xml_str
                except Exception as exc:
                    logger.debug("[HIKVISION BODY] XML parse error: %s", exc)

            if not event_data:
                json_match = re.search(rb'\{[\s\S]*?"dateTime"[\s\S]*?\}', body) or \
                             re.search(rb'\{[\s\S]*?"eventType"[\s\S]*?\}', body)
                if json_match:
                    try:
                        json_str = json_match.group(0).decode('utf-8', errors='ignore')
                        event_data.update(json.loads(json_str))
                    except Exception:
                        pass

    return event_data, face_image_bytes, bg_image_bytes, raw_body


def resolve_camera_from_event(event_data: Dict[str, Any], request, raw_body: Optional[bytes] = None) -> Optional[Camera]:
    """
    Kamerani birinchi o'rinda MAC manzil, Seriya raqami, Device ID yoki IP bo'yicha 100% aniqlikda topish.
    Proksi (192.168.56.1 / 127.0.0.1) orqali kelsa ham, paket ichidagi MAC va Serial orqali aniqlaydi.
    """
    raw_mac = event_data.get('mac_address') or event_data.get('macAddress') or event_data.get('deviceMac') or event_data.get('mac')
    serial_number = event_data.get('serial_number') or event_data.get('serialNumber') or event_data.get('subSerialNumber')
    device_id = event_data.get('device_id') or event_data.get('deviceID') or event_data.get('devId')
    channel_name = event_data.get('channel_name') or event_data.get('channelName') or event_data.get('deviceName')
    ip_addr = event_data.get('ip_address') or event_data.get('ipAddress') or (request.GET.get('ip') if request else None)

    all_cams = list(Camera.objects.filter(is_active=True))

    # A) MAC manzil bo'yicha qat'iy tekshirish (1-o'rinda)
    if raw_mac:
        clean_mac = re.sub(r'[^0-9a-fA-F]', '', str(raw_mac)).lower()
        if len(clean_mac) == 12:
            for cam in all_cams:
                if cam.mac_address:
                    cam_clean_mac = re.sub(r'[^0-9a-fA-F]', '', cam.mac_address).lower()
                    if cam_clean_mac == clean_mac:
                        return cam

    # B) Seriya raqami bo'yicha qat'iy tekshirish (2-o'rinda)
    if serial_number:
        clean_sn = str(serial_number).strip().upper()
        for cam in all_cams:
            if cam.serial_number:
                cam_sn = cam.serial_number.strip().upper()
                if clean_sn in cam_sn or cam_sn in clean_sn:
                    return cam

    # C) Device ID / Channel Name bo'yicha
    if device_id:
        clean_dev = str(device_id).strip().upper()
        for cam in all_cams:
            if cam.serial_number and clean_dev in cam.serial_number.upper():
                return cam
            if cam.name and clean_dev in cam.name.upper():
                return cam

    if channel_name:
        clean_cn = str(channel_name).strip().lower()
        for cam in all_cams:
            if cam.name and (clean_cn in cam.name.lower() or cam.name.lower() in clean_cn):
                return cam

    # D) Xom paket (request.body, headers, query) ichidan chuqur qidirish
    search_text = ""
    if raw_body:
        search_text += raw_body.decode('utf-8', errors='ignore')
    if request:
        search_text += " " + str(request.META) + " " + str(request.GET)

    if search_text:
        search_text_upper = search_text.upper()
        search_clean_hex = re.sub(r'[^0-9a-fA-F]', '', search_text).lower()

        # 1. MAC manzil qidirish
        for cam in all_cams:
            if cam.mac_address:
                cam_clean_mac = re.sub(r'[^0-9a-fA-F]', '', cam.mac_address).lower()
                if len(cam_clean_mac) == 12 and cam_clean_mac in search_clean_hex:
                    return cam

        # 2. Seriya raqamlari qismlarini qidirish (masalan GU3411313, GU3411290, GU3411292, GU3411289)
        for cam in all_cams:
            if cam.serial_number:
                sn_clean = cam.serial_number.upper()
                sub_sns = [sn_clean]
                if 'AAWR' in sn_clean:
                    sub_sns.append(sn_clean.split('AAWR')[-1])
                if 'CCRRAX' in sn_clean:
                    sub_sns.append(sn_clean.split('CCRRAX')[-1])
                m_match = re.search(r'(GU\d+|GH\d+|DS-[0-9A-Z\-\(\)]+)', sn_clean)
                if m_match:
                    sub_sns.append(m_match.group(1))

                for s in sub_sns:
                    if s and s in search_text_upper:
                        return cam

        # 3. IP manzil qidirish
        for cam in all_cams:
            if cam.ip and cam.ip in search_text:
                return cam

    # E) Oxirgi navbatda IP manzil bo'yicha tekshirish
    if ip_addr and ip_addr not in ['127.0.0.1', 'localhost', '192.168.56.1', '10.10.0.40']:
        for cam in all_cams:
            if cam.ip == ip_addr:
                return cam

    return None


import threading
_face_app_lock = threading.Lock()
_face_app_instance = None

def _get_face_analysis_app():
    global _face_app_instance
    if _face_app_instance is not None:
        return _face_app_instance
    with _face_app_lock:
        if _face_app_instance is None:
            from users.consumers import get_face_runtime
            from insightface.app import FaceAnalysis
            face_runtime = get_face_runtime()
            app = FaceAnalysis(name="buffalo_l", providers=face_runtime["providers"])
            app.prepare(ctx_id=0, det_size=(640, 640))
            _face_app_instance = app
        return _face_app_instance


def _match_face_and_update_attendance(face_log: FaceLog, face_bytes: bytes, camera: Optional[Camera], attendance_status: Optional[str] = None) -> None:
    """
    Yuz rasmidan InsightFace embedding olib, bazadagi xodim/talabalar bilan solishtirish (FAISS/NumPy)
    va davomatni (Attendance) yangilash.
    """
    try:
        import cv2
        import numpy as np

        # Rasm baytlaridan OpenCV formatiga o'tkazamiz
        np_arr = np.frombuffer(face_bytes, np.uint8)
        img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return

        # InsightFace orqali embedding olish (tezkor singleton)
        app = _get_face_analysis_app()
        faces = app.get(img)
        if not faces or len(faces) == 0:
            logger.info("[HIKVISION FACE] Yuz aniqlanmadi (embedding olinmadi)")
            return

        # Eng katta yuzni olamiz
        main_face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        emb = main_face.embedding
        norm = np.linalg.norm(emb)
        if norm > 0:
            emb = emb / norm

        # recognition.py orqali moslikni qidiramiz
        from camera.recognition import recognize_user_from_embedding, process_recognition_sync

        matched_user, similarity = recognize_user_from_embedding(
            emb,
            camera_name=camera.name if camera else None
        )

        if matched_user:
            face_log.matched_user = matched_user
            face_log.similarity = similarity
            face_log.confidence = similarity
            face_log.is_recognized = True
            if camera:
                face_log.camera = camera
                face_log.camera_ip = camera.ip
                face_log.device_id = camera.serial_number or camera.name
            face_log.save(update_fields=['matched_user', 'similarity', 'confidence', 'is_recognized', 'camera', 'camera_ip', 'device_id'])
            logger.info("[HIKVISION MATCH] User topildi: %s (%s), similarity=%.3f, Camera=%s", matched_user.full_name or matched_user.username, matched_user.id, similarity, camera.name if camera else "None")

            # Davomatni qayd etish
            process_recognition_sync(user=matched_user, face_crop=img, camera=camera, attendance_status=attendance_status)
        else:
            logger.info("[HIKVISION MATCH] Mos user topilmadi (eng yaqin sim=%.3f)", similarity)
            if camera:
                face_log.camera = camera
                face_log.camera_ip = camera.ip
                face_log.device_id = camera.serial_number or camera.name
                face_log.save(update_fields=['camera', 'camera_ip', 'device_id'])

    except Exception as exc:
        logger.exception("[HIKVISION RECOGNITION] Xatolik yuz berdi: %s", exc)
    finally:
        connections.close_all()


@csrf_exempt
def hikvision_face_capture_view(request):
    """
    Hikvision DS-2CD2686G2-IZS va boshqa kameralar uchun Face Capture HTTP Listening API.
    
    Qabul qilinadigan metodlar: POST (asosiy), GET, HEAD, OPTIONS (Test / Healthcheck).
    Qabul qilinadigan marshrutlar:
      - POST /api/v1/face-capture/
      - POST /api/camera/face-capture/
      - POST /api/face-capture/
      - POST /attendance/api/camera/event/
    """
    if request.method in ['GET', 'HEAD', 'OPTIONS']:
        logger.info("[HIKVISION TEST/PING] Test so'rovi qabul qilindi (Metod: %s, IP: %s)", request.method, request.META.get('REMOTE_ADDR'))
        return _build_hikvision_response(request, success=True, log_id=0)

    client_ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')

    try:
        # 1. Multipart so'rovni tahlil qilish
        event_data, face_bytes, bg_bytes, raw_body = _extract_multipart_body(request)

        # 2. Kamerani MAC manzil, Seriya raqami va paket ma'lumotlari bo'yicha 100% aniqlikda topish
        camera = resolve_camera_from_event(event_data, request, raw_body)

        camera_ip = (camera.ip if camera else (event_data.get('ip_address') or client_ip))
        device_id = (camera.serial_number if camera else (event_data.get('device_id') or event_data.get('serial_number')))
        channel_id = event_data.get('channel_id') or event_data.get('channelID', '1')

        # 3. Vaqtni aniqlash
        captured_at = _parse_hikvision_datetime(event_data.get('date_time'))

        # 4. Yuz rasmini saqlash
        if not face_bytes:
            logger.warning("[HIKVISION] Yuz rasmi topilmadi (IP: %s, Device: %s)", camera_ip, device_id)
            return _build_hikvision_response(request, success=True)

        face_filename = f"hik_face_{uuid.uuid4().hex[:12]}.jpg"
        bg_filename = f"hik_bg_{uuid.uuid4().hex[:12]}.jpg" if bg_bytes else None

        # 5. FaceLog obyektini yaratish
        face_log = FaceLog(
            camera=camera,
            device_id=device_id or (camera.serial_number if camera else None),
            channel_id=channel_id,
            camera_ip=camera_ip,
            event_type=event_data.get('event_type', 'faceCapture'),
            event_description=f"Hikvision Face Capture ({camera.name if camera else 'Noma’lum'})",
            confidence=event_data.get('confidence', 0.0),
            age=event_data.get('age'),
            gender=event_data.get('gender'),
            has_glasses=event_data.get('has_glasses'),
            has_mask=event_data.get('has_mask'),
            face_rect=event_data.get('face_rect', {}),
            raw_metadata=event_data,
            captured_at=captured_at,
        )

        face_log.face_image.save(face_filename, ContentFile(face_bytes), save=False)
        if bg_bytes and bg_filename:
            face_log.background_image.save(bg_filename, ContentFile(bg_bytes), save=False)

        face_log.save()
        logger.info("[HIKVISION FACE CAPTURE] Yangi yuz saqlandi #%s (Kamera: %s [%s], Vaqt: %s)", face_log.id, camera.name if camera else "Noma'lum", camera_ip, captured_at)

        # 6. Yuz tanish va davomatni yangilash
        attendance_status = event_data.get("attendanceStatus") or event_data.get("attendance_status")
        _match_face_and_update_attendance(face_log, face_bytes, camera, attendance_status)

        # 7. Kameraga 200 OK javob qaytarish
        return _build_hikvision_response(request, success=True, log_id=face_log.id)

    except Exception as exc:
        logger.exception("[HIKVISION API] Qayta ishlashda xatolik: %s", exc)
        return _build_hikvision_response(request, success=False, error_msg=str(exc))


def _build_hikvision_response(request, success: bool = True, log_id: Optional[int] = None, error_msg: Optional[str] = None) -> HttpResponse:
    """Hikvision formati talab qiladigan XML yoki JSON 200 OK javobi."""
    accept_header = request.META.get('HTTP_ACCEPT', '')

    if 'application/json' in accept_header or request.GET.get('format') == 'json':
        return JsonResponse({
            "statusCode": 1 if success else 0,
            "statusString": "OK" if success else "Error",
            "subStatusCode": "ok" if success else (error_msg or "error"),
            "logId": log_id
        }, status=200)

    # Standart Hikvision ISAPI XML javobi
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ResponseStatus version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
    <requestURL>{request.path}</requestURL>
    <statusCode>{1 if success else 0}</statusCode>
    <statusString>{"OK" if success else "Error"}</statusString>
    <subStatusCode>{"ok" if success else (error_msg or "error")}</subStatusCode>
    <id>{log_id or 0}</id>
</ResponseStatus>"""

    return HttpResponse(xml_content, content_type="application/xml; charset=utf-8", status=200)
