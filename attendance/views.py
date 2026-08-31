# attendance/views.py
import random
from collections import Counter
import json
from datetime import datetime
import os

from django.views.decorators.csrf import csrf_exempt
from django.http import FileResponse, Http404, JsonResponse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.utils._os import safe_join
from django.db.models import Q
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.shortcuts import render
from django.views import View
from django.utils import timezone
import subprocess
from attendance.data import generate_psychology_comment
from attendance.models import Attendance, PsychologicalProfile
from attendance.services import get_all_services_status, ALLOWED_SERVICES, ALLOWED_ACTIONS
from django.contrib.auth.mixins import LoginRequiredMixin


THUMBNAIL_PRESETS = {
    "avatar": {"size": (96, 96), "crop": True, "quality": 82},
    "stack": {"size": (96, 96), "crop": True, "quality": 78},
    "modal": {"size": (128, 128), "crop": True, "quality": 84},
}


@login_required(login_url="login")
def media_thumbnail(request, preset, path):
    """
    Kichik preview rasmlar uchun keshli thumbnail endpoint.
    Katta original rasmlarni jadvalda to'g'ridan-to'g'ri yuklash sahifani sekinlashtiradi.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    config = THUMBNAIL_PRESETS.get(preset)
    if not config:
        raise Http404("Thumbnail preset topilmadi")

    try:
        source_path = safe_join(settings.MEDIA_ROOT, path)
    except ValueError:
        raise Http404("Media path noto'g'ri")

    if not os.path.isfile(source_path):
        raise Http404("Rasm topilmadi")

    source_mtime = os.path.getmtime(source_path)
    version_key = "".join(ch for ch in request.GET.get("v", "") if ch.isdigit())[:20]
    if not version_key:
        version_key = str(int(source_mtime))

    thumb_rel_path = os.path.join("_thumbs", preset, version_key, f"{path}.jpg")
    try:
        thumb_path = safe_join(settings.MEDIA_ROOT, thumb_rel_path)
    except ValueError:
        raise Http404("Thumbnail path noto'g'ri")

    if not os.path.exists(thumb_path) or os.path.getmtime(thumb_path) < source_mtime:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        try:
            with Image.open(source_path) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode not in ("RGB", "L"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    if "A" in img.mode:
                        background.paste(img, mask=img.getchannel("A"))
                    else:
                        background.paste(img)
                    img = background
                else:
                    img = img.convert("RGB")

                if config["crop"]:
                    img = ImageOps.fit(img, config["size"], method=Image.Resampling.LANCZOS)
                else:
                    img.thumbnail(config["size"], Image.Resampling.LANCZOS)

                img.save(thumb_path, "JPEG", quality=config["quality"], optimize=True, progressive=True)
        except (OSError, UnidentifiedImageError):
            raise Http404("Thumbnail yaratib bo'lmadi")

    response = FileResponse(open(thumb_path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "private, max-age=86400"
    return response

class AttendanceView(LoginRequiredMixin, View):
    login_url = 'login'
    template_name = "attendance/attendance.html"
    paginate_by = 100

    def get(self, request, *args, **kwargs):
        # ✅ date filter: dropdowns or fallback date string, default today
        year_str = request.GET.get("year")
        month_str = request.GET.get("month")
        day_str = request.GET.get("day")
        
        if year_str and month_str and day_str:
            try:
                today = datetime.strptime(f"{year_str}-{month_str}-{day_str}", "%Y-%m-%d").date()
            except ValueError:
                # If invalid date (e.g. Feb 31), fallback to date parameter
                date_str = request.GET.get("date")
                if date_str:
                    try:
                        today = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        today = timezone.localdate()
                else:
                    today = timezone.localdate()
        else:
            date_str = request.GET.get("date")
            if date_str:
                try:
                    today = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    today = timezone.localdate()
            else:
                today = timezone.localdate()

        # ✅ role filter
        role = request.GET.get("role")  # employee / student

        # ✅ search query (name, username, IDs)
        q = request.GET.get("q")

        # ✅ time reference filters (±30 min range)
        entry_time_ref = request.GET.get("entry_time_ref")  # HH:MM
        exit_time_ref = request.GET.get("exit_time_ref")    # HH:MM

        qs = (
            Attendance.objects
            .filter(date=today)
            .select_related('user')
            .prefetch_related('photos')
            .order_by('-last_seen')
        )

        # ✅ role filter
        if role in ["employee", "student"]:
            qs = qs.filter(user__role=role)

        # ✅ search
        if q:
            qs = qs.filter(
                Q(user__full_name__icontains=q) |
                Q(user__username__icontains=q) |
                Q(user__employee_id_number__icontains=q) |
                Q(user__student_id_number__icontains=q)
            )

        # ✅ entry time reference (±30 min)
        if entry_time_ref:
            try:
                ref_time = datetime.strptime(entry_time_ref, "%H:%M").time()
                ref_datetime = datetime.combine(today, ref_time)
                if timezone.is_aware(timezone.now()):
                    ref_datetime = timezone.make_aware(ref_datetime)
                from datetime import timedelta
                start_dt = ref_datetime - timedelta(minutes=30)
                end_dt = ref_datetime + timedelta(minutes=30)
                qs = qs.filter(entry_time__range=(start_dt, end_dt))
            except ValueError:
                pass

        # ✅ exit time reference (±30 min)
        if exit_time_ref:
            try:
                ref_time = datetime.strptime(exit_time_ref, "%H:%M").time()
                ref_datetime = datetime.combine(today, ref_time)
                if timezone.is_aware(timezone.now()):
                    ref_datetime = timezone.make_aware(ref_datetime)
                from datetime import timedelta
                start_dt = ref_datetime - timedelta(minutes=30)
                end_dt = ref_datetime + timedelta(minutes=30)
                qs = qs.filter(exit_time__range=(start_dt, end_dt))
            except ValueError:
                pass

        # ✅ Pagination
        paginator = Paginator(qs, self.paginate_by)
        page_number = request.GET.get("page", 1)

        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Davomat', 'url': '/attendance/'},
            {'name': 'Bugungi davomat', 'url': None},
        ]

        current_year = timezone.localdate().year
        years_list = list(range(2024, current_year + 3))
        months_list = [
            (1, "Yanvar"),
            (2, "Fevral"),
            (3, "Mart"),
            (4, "Aprel"),
            (5, "May"),
            (6, "Iyun"),
            (7, "Iyul"),
            (8, "Avgust"),
            (9, "Sentabr"),
            (10, "Oktabr"),
            (11, "Noyabr"),
            (12, "Dekabr"),
        ]
        days_list = list(range(1, 32))

        from camera.models import UnknownFaceCluster
        unknown_count = UnknownFaceCluster.objects.filter(associated_user__isnull=True).count()

        context = {
            "attendances": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,
            "breadcrumbs": breadcrumbs,
            "today": today,
            "unknown_count": unknown_count,

            # ✅ dropdown lists
            "years_list": years_list,
            "months_list": months_list,
            "days_list": days_list,

            # ✅ selected date dropdown values
            "selected_year": today.year,
            "selected_month": today.month,
            "selected_day": today.day,

            # ✅ return filter values to template
            "role": role or "",
            "q": q or "",
            "date": today.strftime("%Y-%m-%d"),
            "entry_time_ref": entry_time_ref or "",
            "exit_time_ref": exit_time_ref or "",
        }
        return render(request, self.template_name, context)

# ===============================================================
# YANGI PSIXOLOGIK PORTRET SAHIFASI
# ===============================================================
EMOTIONS = ['neutral', 'happiness', 'surprise', 'sadness', 'anger', 'disgust', 'fear', 'contempt']


def _merge_probs(profiles):
    """
    Bir userda bir nechta profile bo'lsa, emotion_probs ni birlashtirib o'rtacha qiladi.
    """
    sums = {e: 0.0 for e in EMOTIONS}
    cnt = 0
    for p in profiles:
        probs = getattr(p, "emotion_probs", None) or {}
        if probs:
            for e in EMOTIONS:
                sums[e] += float(probs.get(e, 0.0))
            cnt += 1
    if cnt == 0:
        return {}
    return {e: sums[e] / cnt for e in EMOTIONS}


def _top3_text(probs: dict) -> str:
    if not probs:
        return ""
    top3 = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:3]
    return ", ".join([f"{k}:{v:.2f}" for k, v in top3])


class PsychologicalProfileView(LoginRequiredMixin, View):
    login_url = 'login'
    template_name = "attendance/psychological_profile.html"
    paginate_by = 100
    def get(self, request, *args, **kwargs):
        # ✅ date filter: dropdowns or fallback date string, default today
        year_str = request.GET.get("year")
        month_str = request.GET.get("month")
        day_str = request.GET.get("day")
        
        if year_str and month_str and day_str:
            try:
                today = datetime.strptime(f"{year_str}-{month_str}-{day_str}", "%Y-%m-%d").date()
            except ValueError:
                # If invalid date (e.g. Feb 31), fallback to date parameter
                date_str = request.GET.get("date")
                if date_str:
                    try:
                        today = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        today = timezone.localdate()
                else:
                    today = timezone.localdate()
        else:
            date_str = request.GET.get("date")
            if date_str:
                try:
                    today = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    today = timezone.localdate()
            else:
                today = timezone.localdate()

        # ✅ filterlar
        role = request.GET.get("role")  # employee yoki student
        q = request.GET.get("q")        # full_name search

        profiles_qs = (
            PsychologicalProfile.objects
            .filter(attendance__date=today)
            .select_related('attendance__user')
            .order_by('attendance__user__full_name')
        )

        # ✅ role filter
        if role in ["employee", "student"]:
            profiles_qs = profiles_qs.filter(attendance__user__role=role)

        # ✅ name/username/id search
        if q:
            profiles_qs = profiles_qs.filter(
                Q(attendance__user__full_name__icontains=q) |
                Q(attendance__user__username__icontains=q) |
                Q(attendance__user__employee_id_number__icontains=q) |
                Q(attendance__user__student_id_number__icontains=q)
            )

        # ✅ User bo'yicha guruhlash
        user_map = {}
        for profile in profiles_qs:
            user = profile.attendance.user
            user_id = user.id
            user_map.setdefault(user_id, {"user": user, "profiles": []})
            user_map[user_id]["profiles"].append(profile)

        final_profiles = []
        for data in user_map.values():
            user = data["user"]
            plist = data["profiles"]
            count = len(plist)
            created_at = plist[0].created_at if plist else None

            # ====== AVERAGE CALC ======
            avg_stress = sum(float(p.stress_level or 0) for p in plist) / max(count, 1)
            avg_energy = sum(float(p.energy_level or 0) for p in plist) / max(count, 1)
            avg_mood = int(round(sum(int(p.mood_score or 0) for p in plist) / max(count, 1)))

            emotions = [(p.dominant_emotion or "neutral").lower().strip()
                        for p in plist if p.dominant_emotion]
            most_common_emotion = Counter(emotions).most_common(1)[0][0] if emotions else "neutral"

            avg_conf = sum(float(getattr(p, "confidence", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_stab = sum(float(getattr(p, "stability", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_neg  = sum(float(getattr(p, "negative_ratio", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_pos  = sum(float(getattr(p, "positive_ratio", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_neu  = sum(float(getattr(p, "neutral_ratio", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_val  = sum(float(getattr(p, "valence", 0.0) or 0.0) for p in plist) / max(count, 1)
            avg_ar   = sum(float(getattr(p, "arousal", 0.0) or 0.0) for p in plist) / max(count, 1)

            photo_count = sum(int(getattr(p, "photo_count", 0) or 0) for p in plist)
            avg_quality = sum(float(getattr(p, "face_quality", 0.0) or 0.0) for p in plist) / max(count, 1)

            probs = _merge_probs(plist)
            if probs:
                most_common_emotion = max(probs, key=probs.get)

            # ====== PSYCHOLOGY COMMENT ======
            psychology_text = generate_psychology_comment(
                stress=float(avg_stress),
                mood=int(avg_mood),
                energy=float(avg_energy),
                dominant_emotion=most_common_emotion,
                previous_profiles=PsychologicalProfile.objects.filter(
                    attendance__user=user,
                    attendance__date__lt=today,
                    attendance__date__gte=today - timezone.timedelta(days=30)
                ).select_related("attendance"),
                confidence=float(avg_conf),
                stability=float(avg_stab),
                negative_ratio=float(avg_neg),
            )

            # ====== STATE ======
            if avg_stress < 0.30 and avg_mood > 75 and avg_energy > 0.70:
                state = "excellent"
                state_display = "A'lo"
            elif avg_stress < 0.45 and avg_mood > 65:
                state = "good"
                state_display = "Yaxshi"
            elif avg_stress < 0.65:
                state = "normal"
                state_display = "O‘rtacha"
            elif avg_stress < 0.80:
                state = "warning"
                state_display = "Ehtiyot"
            else:
                state = "critical"
                state_display = "Jiddiy"

            final_profiles.append({
                "user": user,
                "stress": int(round(avg_stress * 100)),
                "mood": int(avg_mood),
                "energy": int(round(avg_energy * 100)),
                "psychology": psychology_text,
                "state": state,
                "state_display": state_display,
                "dominant_emotion": most_common_emotion.title(),
                "confidence": round(avg_conf, 2),
                "stability": round(avg_stab, 2),
                "negative_ratio": round(avg_neg, 2),
                "positive_ratio": round(avg_pos, 2),
                "neutral_ratio": round(avg_neu, 2),
                "valence": round(avg_val, 2),
                "arousal": round(avg_ar, 2),
                "photo_count": photo_count,
                "face_quality": round(avg_quality, 2),
                "emotion_probs": probs,
                "top_emotions": _top3_text(probs),
                "count": count,
                "created_at": created_at,
            })

        # ✅ Tartiblash
        state_order = ["critical", "warning", "normal", "good", "excellent"]
        final_profiles.sort(key=lambda x: (state_order.index(x["state"]), -x["photo_count"]))

        # ✅ Pagination
        paginator = Paginator(final_profiles, self.paginate_by)
        page_number = request.GET.get("page", 1)

        try:
            page_obj = paginator.page(page_number)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Davomat', 'url': '/attendance/'},
            {'name': 'Psixologik portretlar', 'url': None},
        ]

        critical_count = sum(1 for p in final_profiles if p["state"] == "critical")
        warning_count = sum(1 for p in final_profiles if p["state"] == "warning")

        current_year = timezone.localdate().year
        years_list = list(range(2024, current_year + 3))
        months_list = [
            (1, "Yanvar"),
            (2, "Fevral"),
            (3, "Mart"),
            (4, "Aprel"),
            (5, "May"),
            (6, "Iyun"),
            (7, "Iyul"),
            (8, "Avgust"),
            (9, "Sentabr"),
            (10, "Oktabr"),
            (11, "Noyabr"),
            (12, "Dekabr"),
        ]
        days_list = list(range(1, 32))

        return render(request, self.template_name, {
            "profiles": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,

            "breadcrumbs": breadcrumbs,
            "today": today,

            # ✅ dropdown lists
            "years_list": years_list,
            "months_list": months_list,
            "days_list": days_list,

            # ✅ selected date dropdown values
            "selected_year": today.year,
            "selected_month": today.month,
            "selected_day": today.day,

            "role": role or "",
            "q": q or "",
            "date": today.strftime("%Y-%m-%d"),

            "total_employees": len(final_profiles),
            "critical_count": critical_count,
            "warning_count": warning_count,
        })


@login_required(login_url="login")
def service_logs_view(request):
    """
    Service list + log viewer page
    """

    cmd = ["systemctl", "list-units", "--type=service", "--no-pager", "--no-legend"]
    out = subprocess.check_output(cmd, text=True)

    services = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 1:
            services.append(parts[0])

    # sort
    services = sorted(services)

    breadcrumbs = [
        {"name": "Bosh sahifa", "url": "/"},
        {"name": "Kameralar", "url": "/cameras/"},
        {"name": "Servis Loglari", "url": None},
    ]

    key_services = get_all_services_status()

    return render(request, "pages/service_logs.html", {
        "breadcrumbs": breadcrumbs,
        "services": services,
        "key_services": key_services,
    })

@login_required(login_url="login")
def service_status_view(request):
    """
    API endpoint that returns current status of all 3 allowed services.
    Used by the frontend status poller (every 5 seconds).
    """
    services = get_all_services_status()
    return JsonResponse({"services": services})


ALLOWED_ACTIONS = {"start", "stop", "restart", "enable", "disable"}


@login_required(login_url="login")
@csrf_exempt
def service_action_view(request):
    """
    POST JSON:
    {
        "service": "camera-daemon.service",
        "action": "restart"
    }
    """
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST kerak"}, status=405)

    try:
        body = json.loads(request.body.decode())
        service = (body.get("service") or "").strip()
        action = (body.get("action") or "").strip().lower()
    except Exception:
        return JsonResponse({"success": False, "message": "JSON xato"}, status=400)

    if not service.endswith(".service"):
        return JsonResponse({"success": False, "message": "Service nomi xato"}, status=400)

    if action not in ALLOWED_ACTIONS:
        return JsonResponse({"success": False, "message": "Action ruxsat yo‘q"}, status=400)

    cmd = ["sudo", "/bin/systemctl", action, service]

    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        return JsonResponse({"success": True, "message": f"{service} -> {action} bajarildi ✅"})
    except subprocess.CalledProcessError as exc:
        return JsonResponse({"success": False, "message": exc.output[:500]})

@login_required
def service_unit_load(request):
    service = request.GET.get("service")
    if not service:
        return JsonResponse({"success": False, "message": "Service berilmadi"})

    try:
        path = subprocess.check_output(["/bin/systemctl", "show", service, "--property=FragmentPath"], text=True).strip()
        path = path.replace("FragmentPath=", "").strip()

        if not path or not path.endswith(".service"):
            return JsonResponse({"success": False, "message": "Unit fayl topilmadi"})

        content = subprocess.check_output(["sudo", "/bin/cat", path], text=True)
        return JsonResponse({"success": True, "content": content})

    except subprocess.CalledProcessError as e:
        return JsonResponse({"success": False, "message": str(e)})

@login_required
@csrf_exempt
def service_unit_save(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST kerak"})

    data = json.loads(request.body.decode("utf-8"))
    service = data.get("service")
    content = data.get("content")

    if not service or not content:
        return JsonResponse({"success": False, "message": "Service yoki content yo‘q"})

    try:
        path = subprocess.check_output(["/bin/systemctl", "show", service, "--property=FragmentPath"], text=True).strip()
        path = path.replace("FragmentPath=", "").strip()

        if not path or not path.endswith(".service"):
            return JsonResponse({"success": False, "message": "Unit fayl topilmadi"})

        # temp file yozib keyin sudo mv
        tmp_path = f"/tmp/{service}.service"
        with open(tmp_path, "w") as f:
            f.write(content)

        subprocess.check_call(["sudo", "/bin/mv", tmp_path, path])
        subprocess.check_call(["sudo", "/bin/systemctl", "daemon-reload"])

        return JsonResponse({"success": True, "message": "Unit saqlandi va daemon-reload qilindi"})

    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)})


def get_chatbot_context(message_text):
    import django.utils.timezone as tz
    from datetime import date, timedelta
    from django.db.models import Q
    from attendance.models import Attendance, PsychologicalProfile
    from users.models import CustomUser, AcademicGroup
    from camera.models import LessonSchedule, Subject, Auditorium, LessonPair
    import re

    context_lines = []
    text_lower = message_text.lower()
    today = tz.localdate()

    # Determine target date
    target_date = today
    date_label = "Bugungi"
    
    if "kecha" in text_lower or "kechagi" in text_lower:
        target_date = today - timedelta(days=1)
        date_label = "Kechagi"
    elif "oldingi kuni" in text_lower or "o'tgan kuni" in text_lower or "avvalgi kuni" in text_lower:
        target_date = today - timedelta(days=2)
        date_label = "O'tgan kungi (2 kun oldingi)"

    # Tanlangan guruhni aniqlash
    mentioned_group = None
    groups = AcademicGroup.objects.all()
    for g in groups:
        g_name = g.name.lower()
        if g_name in text_lower or g_name.replace("-", "") in text_lower:
            mentioned_group = g
            break

    # 1. Extract potential name words to filter mentioned users quickly and avoid full table scan
    words = re.findall(r'[a-zA-Z\'\`‘’ʻo\'O\'g\'G\'а-яА-ЯёЁўЎқҚғҒҳҲ]+', text_lower)
    stop_words = {
        'bugun', 'kecha', 'ertaga', 'keldi', 'kelmadi', 'kelgan', 'kelmagan', 
        'xodim', 'talaba', 'ruhiy', 'holati', 'stress', 'oyda', 'oyida', 
        'davomida', 'necha', 'marta', 'sana', 'kimlar', 'kimning', 'rttm', 
        'moliya', 'kadr', 'oldingi', 'kuni', 'o\'tgan', 'avvalgi', 'yordam', 
        'tizim', 'loyiha', 'haqida', 'ma\'lumot', 'mening', 'ishga', 'darsga', 
        'darsda', 'ishda', 'yo\'q', 'psixologik', 'holatlar', 'jiddiy',
        'salom', 'rahmat', 'hali', 'sani', 'shuningdek', 'faqat', 'uning', 
        'men', 'sen', 'u', 'biz', 'siz', 'ular', 'bo\'limi', 'guruh', 'guruhidagi',
        'nechta', 'qachon', 'qanday', 'qaysi', 'kim', 'nima', 'qayerda',
        'bolimi', 'psixolog', 'holat', 'yordamchi', 'tahlil', 'kameralar'
    }
    candidates = [w for w in words if len(w) >= 4 and w not in stop_words]

    mentioned_user = None
    if candidates:
        q_filter = Q()
        for cand in candidates:
            q_filter |= Q(full_name__icontains=cand) | Q(username__icontains=cand)
            
        users_qs = CustomUser.objects.filter(q_filter, active=True)
        matched_users_data = list(users_qs.values_list('id', 'full_name', 'username'))
        
        best_match = None
        best_overlap = 0
        for uid, full_name, username in matched_users_data:
            fn_lower = full_name.lower() if full_name else ""
            un_lower = username.lower() if username else ""
            
            overlap = 0
            if fn_lower:
                if fn_lower in text_lower or text_lower in fn_lower:
                    overlap += 5
                else:
                    fn_words = fn_lower.split()
                    for cand in candidates:
                        if cand in fn_words:
                            overlap += 2
                        elif cand in fn_lower:
                            overlap += 1
            if un_lower:
                if un_lower in text_lower:
                    overlap += 5
                elif un_lower in candidates:
                    overlap += 3
            
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = uid
                
        if best_match:
            mentioned_user = CustomUser.objects.filter(id=best_match).first()

    if mentioned_user:
        context_lines.append(f"Foydalanuvchi ma'lumoti: {mentioned_user.full_name} ({mentioned_user.get_role_display()}, Bo'lim/Guruh: {mentioned_user.department_name or mentioned_user.group_name or 'Noma`lum'}, Lavozim: {mentioned_user.position or mentioned_user.specialty or 'Noma`lum'})")
        
        # Target date's attendance
        target_att = Attendance.objects.filter(user=mentioned_user, date=target_date).first()
        if target_att:
            status_str = "Binoda" if target_att.is_present else "Chiqib ketgan"
            context_lines.append(
                f"{date_label} davomat: Kelgan. Kirish vaqti: {target_att.entry_time.strftime('%H:%M:%S') if target_att.entry_time else 'Noma`lum'}, "
                f"Chiqish vaqti: {target_att.exit_time.strftime('%H:%M:%S') if target_att.exit_time else 'Noma`lum'}, "
                f"Holati: {status_str}, Binoda bo'lgan vaqt: {target_att.duration_minutes} daqiqa."
            )
            # Target date's psychology
            try:
                target_psych = target_att.psychology
                context_lines.append(
                    f"{date_label} ruhiy holati: Emotsiya: {target_psych.dominant_emotion}, Stress darajasi: {target_psych.stress_level:.2f}, "
                    f"Kayfiyat bali: {target_psych.mood_score}/100, AI tavsifi: {target_psych.summary_text or 'Izoh yo`q'}."
                )
            except PsychologicalProfile.DoesNotExist:
                context_lines.append(f"{date_label} ruhiy holati tahlil qilinmagan.")
        else:
            context_lines.append(f"{date_label} davomat: Kelmagan / darsda yoki ishda yo'q.")

        # Current month attendance statistics
        start_of_month = today.replace(day=1)
        month_atts = Attendance.objects.filter(
            user=mentioned_user,
            date__gte=start_of_month,
            date__lte=today
        ).order_by('-date')
        
        attended_days = month_atts.count()
        attended_dates_str = ", ".join([str(att.date) for att in month_atts[:15]])
        if month_atts.count() > 15:
            attended_dates_str += " (oxirgi 15 kun ko'rsatilmoqda)"
            
        context_lines.append(
            f"{mentioned_user.full_name} ning ushbu oydagi ({today.strftime('%B %Y')}) davomat statistikasi:\n"
            f"- Kelgan kunlari soni: jami {attended_days} marta.\n"
            f"- Kelgan kunlari sanalari: {attended_dates_str or 'Ushbu oyda hali darsga/ishga kelmagan.'}"
        )
            
        # Past serious profiles (excluding target date)
        past_profiles = PsychologicalProfile.objects.filter(
            attendance__user=mentioned_user
        ).filter(
            Q(stress_level__gt=0.6) | Q(mood_score__lt=40) | Q(dominant_emotion__in=['angry', 'sad', 'fear'])
        ).exclude(attendance__date=target_date).select_related('attendance').order_by('-attendance__date')[:5]
        
        if past_profiles.exists():
            context_lines.append(f"{mentioned_user.full_name} ning avvalgi ruhiy holati jiddiy bo'lgan kunlari (tarix):")
            for p in past_profiles:
                context_lines.append(
                    f"- Sana: {p.attendance.date}, Emotsiya: {p.dominant_emotion}, Stress: {p.stress_level:.2f}, "
                    f"Kayfiyat: {p.mood_score}/100, Izoh: {p.summary_text or ''}"
                )
            context_lines.append(
                f"Tavsiya: {mentioned_user.full_name} ga stressni kamaytirish uchun dam olish vaqti berish, "
                f"yuklamasini yengillashtirish yoki suhbatlashish tavsiya etiladi."
            )
        else:
            context_lines.append(f"{mentioned_user.full_name} ning avvalgi jiddiy ruhiy stress holatlari bazada topilmadi.")

    # 2. Target date's serious psychological profiles list (general or if specifically asked)
    if any(k in text_lower for k in ["jiddiy", "ruhiy", "psixolog", "stress", "kayfiyat", "charchagan", "tushkun"]):
        profiles = PsychologicalProfile.objects.filter(
            attendance__date=target_date
        ).filter(
            Q(stress_level__gt=0.6) | Q(mood_score__lt=40) | Q(dominant_emotion__in=['angry', 'sad', 'fear'])
        ).select_related('attendance__user').order_by('-stress_level')

        total_profiles_count = profiles.count()
        if total_profiles_count > 0:
            context_lines.append(f"{date_label} psixologik holati jiddiy (yuqori stress yoki past kayfiyat) bo'lgan foydalanuvchilar (jami {total_profiles_count} ta, eng jiddiy 15 tasi ko'rsatilmoqda):")
            for p in profiles[:15]:
                u = p.attendance.user
                context_lines.append(
                    f"- Foydalanuvchi: {u.full_name} ({u.get_role_display()}, ID: {u.employee_id_number or u.student_id_number or u.id}, Bo'lim: {u.department_name or u.group_name or 'Noma`lum'}), "
                    f"Emotsiya: {p.dominant_emotion}, Stress darajasi: {p.stress_level:.2f}, Kayfiyat bali: {p.mood_score}/100, "
                    f"Tavsif: {p.summary_text or 'Izoh yoq'}"
                )
            if total_profiles_count > 15:
                context_lines.append(f"- ... va yana {total_profiles_count - 15} ta foydalanuvchida jiddiy holat aniqlandi.")
        else:
            context_lines.append(f"{date_label} tizimda psixologik holati jiddiy bo'lgan foydalanuvchilar aniqlanmadi.")

    # 3. Attendance - who didn't come on target date
    if any(k in text_lower for k in ["kelmadi", "kelmagan", "yo'q", "kelish", "darsda yo'q", "ishda yo'q"]):
        # 3a. O'qituvchilar va Xodimlardan kelmaganlar
        dept_filter = None
        if "rttm" in text_lower:
            dept_filter = "rttm"
        elif "moliya" in text_lower:
            dept_filter = "moliya"
        elif "kadr" in text_lower:
            dept_filter = "kadr"
            
        employees = CustomUser.objects.filter(role=CustomUser.Role.EMPLOYEE, active=True)
        if dept_filter:
            employees = employees.filter(department_name__icontains=dept_filter)
            
        absent_employees = employees.exclude(attendances__date=target_date)
        absent_data = list(absent_employees.values_list('full_name', 'employee_id_number', 'id', 'department_name'))
        
        dept_str = f"'{dept_filter.upper()}' bo'limidan " if dept_filter else ""
        if absent_data:
            context_lines.append(f"{date_label} {dept_str}ishga kelmagan o'qituvchi va xodimlar ro'yxati (jami {len(absent_data)} ta, 15 tasi ko'rsatilmoqda):")
            for full_name, emp_id, uid, dept in absent_data[:15]:
                context_lines.append(
                    f"- Xodim: {full_name} (ID: {emp_id or uid}, Bo'lim: {dept or 'Noma`lum'})"
                )
            if len(absent_data) > 15:
                context_lines.append(f"- ... va yana {len(absent_data) - 15} ta xodim kelmagan.")
        else:
            context_lines.append(f"{date_label} {dept_str}barcha xodimlar ishga kelgan.")

        # 3b. Talabalardan kelmaganlar (Darsga kelmaganlar)
        if mentioned_group:
            students = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group=mentioned_group, active=True)
            absent_students = students.exclude(attendances__date=target_date)
            absent_list = list(absent_students.values_list('full_name', 'student_id_number', 'id'))
            if absent_list:
                context_lines.append(f"{date_label} {mentioned_group.name} guruhidan darsga kelmagan talabalar ro'yxati (jami {len(absent_list)} ta):")
                for name, std_id, uid in absent_list:
                    context_lines.append(f"- Talaba: {name} (ID: {std_id or uid})")
            else:
                context_lines.append(f"{date_label} {mentioned_group.name} guruhidan barcha talabalar darsga kelgan (100% davomat).")
        elif "dars" in text_lower or "talaba" in text_lower:
            weekday = target_date.isoweekday()
            if weekday != 7:
                active_groups = AcademicGroup.objects.filter(schedules__weekday=weekday).distinct()
                if active_groups.exists():
                    for grp in active_groups:
                        students = CustomUser.objects.filter(role=CustomUser.Role.STUDENT, academic_group=grp, active=True)
                        absent_students = students.exclude(attendances__date=target_date)
                        absent_list = list(absent_students.values_list('full_name', 'student_id_number', 'id'))
                        if absent_list:
                            context_lines.append(f"{date_label} {grp.name} guruhidan darsga kelmagan talabalar (jami {len(absent_list)} ta, top 10 ko'rsatilmoqda):")
                            for name, std_id, uid in absent_list[:10]:
                                context_lines.append(f"- Talaba: {name} (Guruh: {grp.name}, ID: {std_id or uid})")
                            if len(absent_list) > 10:
                                context_lines.append(f"  ... va yana {len(absent_list) - 10} ta talaba.")

    # 4. Target date's schedules, subjects, topics, and lectures
    if any(k in text_lower for k in ["mavzu", "dars", "ma'ruza", "maruza", "jadval", "reja", "tushuntir"]):
        weekday = target_date.isoweekday()
        if weekday != 7:
            schedules = LessonSchedule.objects.filter(weekday=weekday).select_related('academic_group', 'subject', 'auditorium', 'lesson_pair')
            if mentioned_group:
                schedules = schedules.filter(academic_group=mentioned_group)
            
            schedules_list = list(schedules)
            if schedules_list:
                context_lines.append(f"{date_label} dars jadvali, mavzulari va rejasi:")
                for s in schedules_list:
                    pair_str = f"{s.lesson_pair.pair_number}-para ({s.lesson_pair.start_time.strftime('%H:%M')} - {s.lesson_pair.end_time.strftime('%H:%M')})" if s.lesson_pair else "Noma'lum para"
                    context_lines.append(
                        f"- Guruh: {s.academic_group.name}\n"
                        f"  Fan: {s.subject.name}\n"
                        f"  O'qituvchi: {s.teacher_name or 'Noma`lum'}\n"
                        f"  Auditoriya: {s.auditorium.name} (Kamera IP: {s.auditorium.camera.ip if s.auditorium.camera else 'Yoq'})\n"
                        f"  Vaqti: {pair_str}\n"
                        f"  Dars mavzusi va rejasi: {s.subject.description or s.subject.name}"
                    )
            else:
                context_lines.append(f"{date_label} tizimda dars jadvallari topilmadi.")

    return "\n".join(context_lines)


@login_required
def chatbot_view(request):
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Gemini Chatbot', 'url': None},
    ]
    return render(request, "pages/chatbot.html", {'breadcrumbs': breadcrumbs})


@login_required
@csrf_exempt
def chatbot_api(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST so'rovi talab qilinadi"}, status=405)

    try:
        data = json.loads(request.body.decode("utf-8") if request.body else "{}")
        message = data.get("message", "").strip()
        history = data.get("history", [])

        if not message:
            return JsonResponse({"success": False, "message": "Xabar bo'sh bo'lishi mumkin emas"}, status=400)

        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # OpenRouter API konfiguratsiyasi (Google Gemini 2.5 Flash / 2.0 Flash)
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        models = [
            "google/gemini-2.5-flash",
            "google/gemini-2.0-flash-exp",
            "google/gemini-2.5-pro",
        ]
        
        # OpenRouter uchun standart chat xabarlar ro'yxati
        messages_payload = []
        
        # Tizim yo'riqnomasi (System Prompt)
        system_instruction_text = (
            "Siz Namangan Davlat Pedagogika Instituti (NamSPI) uchun maxsus yaratilgan aqlli AI yordamchisiz. "
            "Foydalanuvchi savollariga o'zbek tilida aniq, chiroyli va juda batafsil javob bering.\n\n"
            "MUHIM QOIDALAR:\n"
            "1. Javoblarda emojilardan (smayliklar, rasmli emojilar, belgi-emojilar kabi barcha emojilardan) umuman foydalanmang! Bu mutlaqo taqiqlangan.\n"
            "2. Emojilar o'rniga har doim gap va paragraflar boshlanishiga mos keluvchi Feather ikonkalari (<i data-feather='...'></i>) ni qo'shing. Barcha gaplar yoki bandlar kamida bitta Feather ikonka bilan boshlanishi kerak (masalan, kirish yoki salomlashish uchun <i data-feather='message-square' class='text-info me-2'></i>, ma'lumot berishda <i data-feather='info' class='text-primary me-2'></i>, xavfli yoki jiddiy holatlar uchun <i data-feather='alert-triangle' class='text-danger me-2'></i>, ijobiy/yaxshi holat uchun <i data-feather='check-circle' class='text-success me-2'></i>, psixologik yoki ruhiy holatlar uchun <i data-feather='heart' class='text-danger me-2'></i> yoki <i data-feather='activity' class='text-warning me-2'></i>, tavsiyalar uchun <i data-feather='help-circle' class='text-success me-2'></i>, xodim/talabalar uchun <i data-feather='user' class='text-secondary me-2'></i>).\n"
            "3. Ro'yxatlar, jadvallar yoki ma'lumotlarni chiroyli formatlash uchun faqat HTML va Bootstrap 5 klasslaridan foydalaning. Matn toza, chiroyli va premium ko'rinishga ega bo'lishi lozim (inline CSS yozmang, faqat Bootstrap 5 classlari):\n"
            "   - Jiddiy psixologik holatdagi foydalanuvchilar ro'yxati so'ralganda: Bootstrap 5 ning chiroyli jadvali (<table class='table table-hover align-middle border mb-0'>...</table>) yoki chiroyli ro'yxat guruhi (<div class='list-group'>...</div>) va har bir foydalanuvchi uchun <span class='badge bg-danger'>Stress yuqori</span> kabi badge'lardan va ogohlantirish ikonkalaridan foydalanib chiqarib bering.\n"
            "   - Agar hech kimda jiddiy holat aniqlanmagan bo'lsa: Buni oddiy matn sifatida emas, balki chiroyli alert/card (<div class='alert alert-success d-flex align-items-center mb-0' role='alert'><i data-feather='check-circle' class='text-success me-2 fs-5'></i> <div>Bugun/kecha tizimda jiddiy ruhiy holatdagi foydalanuvchilar aniqlanmadi.</div></div>) ko'rinishida yozing.\n"
            "   - Agar ma'lumot topilmasa o'rniga: Chiroyli Bootstrap card/alert (<div class='alert alert-warning d-flex align-items-center mb-0' role='alert'><i data-feather='alert-triangle' class='text-warning me-2 fs-5'></i> <div>Kerakli ma'lumot topilmadi...</div></div>) ishlating.\n"
            "   - Xodim yoki talabaning psixologik holati va unga yordam berish haqida so'ralganda: Avvalgi holatlarini jadval ko'rinishida ko'rsating. Tavsiyalarni esa chiroyli card (<div class='card bg-light border-start border-primary border-3 shadow-sm'>...</div>) ichida vizual jihatdan ajratib ko'rsating. Tavsiyalar oldida mos ikonkalardan foydalaning.\n"
            "   - Ishga yoki darsga kelmaganlar ro'yxati so'ralganda: Kelmagan xodim va talabalarning ro'yxatini HTML jadval (Ismi, ID raqami, Bo'limi/Guruhi) yoki list-group ko'rinishida chiqarib bering. Har bir kelmagan odam yoniga <i data-feather='user-x' class='text-danger me-2'></i> belgisini qo'ying. Foydalanuvchiga kelmaganlar ro'yxatini to'liq chiqarib bering, chala qoldirmang.\n"
            "   - Dars mavzusi va ma'ruzalar so'ralganda: Mavzu va uning tavsifiga asoslanib, juda batafsil, to'liq, mazmunli ma'ruza yozib bering. Mavzuni, uning asosiy tushunchalarini va qo'llanishini to'laqonli yoriting, tushunchalar bering.\n"
            "4. HTML teglarni to'g'ridan-to'g'ri o'z matningiz ichida qaytaring, ularni ```html kodi ko'rinishida o'ramang, chunki chat oynasi HTMLni render qiladi. Faqat haqiqiy dasturlash kodi yozgandagina ``` ishlatishingiz mumkin.\n"
        )
        
        # Build context from DB queries
        db_context = get_chatbot_context(message)
        if db_context:
            system_instruction_text += f"\nTizim ma'lumotlar bazasidan olingan bugungi davomat va dars jadvali ma'lumotlari (shu ma'lumotlar asosida foydalanuvchiga batafsil javob bering):\n{db_context}"
            
        messages_payload.append({
            "role": "system",
            "content": system_instruction_text
        })
        
        for msg in history:
            role = "user" if msg.get('role') == 'user' else "assistant"
            messages_payload.append({
                "role": role,
                "content": msg.get('text', '')
            })
        
        messages_payload.append({
            "role": "user",
            "content": message
        })
        
        text_response = None
        model_used = None
        last_err = None
        
        # OpenRouter API ga murojaat qilamiz
        for model in models:
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://bioface.uz",
                "X-Title": "NOVA AI",
                "Content-Type": "application/json"
            }
            payload = {
                "model": model,
                "messages": messages_payload,
                "temperature": 0.2,
                "max_tokens": 1200
            }
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=20)
                if r.status_code == 200:
                    res_data = r.json()
                    text_response = res_data['choices'][0]['message']['content'].strip()
                    model_used = model
                    break
                else:
                    last_err = f"OpenRouter model {model} returned {r.status_code}: {r.text}"
            except Exception as e:
                last_err = str(e)
 
        if text_response is None:
            return JsonResponse({"success": False, "message": f"OpenRouter API xatoligi: {last_err}"}, status=500)
 
        return JsonResponse({
            "success": True,
            "response": text_response,
            "model": model_used
        })
 
    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)}, status=500)
