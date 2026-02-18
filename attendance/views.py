# attendance/views.py
import random
from collections import Counter
import json
from datetime import datetime

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.db.models import Q
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.shortcuts import render
from django.views import View
from django.utils import timezone
import subprocess
from attendance.data import generate_psychology_comment
from attendance.models import Attendance, PsychologicalProfile
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.decorators import login_required

class AttendanceView(LoginRequiredMixin, View):
    login_url = 'login'
    template_name = "attendance/attendance.html"
    paginate_by = 100

    def get(self, request, *args, **kwargs):
        # ✅ date filter: default today
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

        # ✅ time interval filter
        entry_from = request.GET.get("entry_from")  # HH:MM
        entry_to = request.GET.get("entry_to")
        exit_from = request.GET.get("exit_from")
        exit_to = request.GET.get("exit_to")

        qs = (
            Attendance.objects
            .filter(date=today)
            .select_related('user')
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

        # ✅ entry time interval
        if entry_from:
            try:
                qs = qs.filter(entry_time__gte=entry_from)
            except ValueError:
                pass

        if entry_to:
            try:
                qs = qs.filter(entry_time__lte=entry_to)
            except ValueError:
                pass

        # ✅ exit time interval
        if exit_from:
            try:
                qs = qs.filter(exit_time__gte=exit_from)
            except ValueError:
                pass

        if exit_to:
            try:
                qs = qs.filter(exit_time__lte=exit_to)
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

        context = {
            "attendances": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,
            "breadcrumbs": breadcrumbs,
            "today": today,

            # ✅ return filter values to template
            "role": role or "",
            "q": q or "",
            "date": today.strftime("%Y-%m-%d"),
            "entry_from": entry_from or "",
            "entry_to": entry_to or "",
            "exit_from": exit_from or "",
            "exit_to": exit_to or "",
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
        # ✅ date param bo'lsa shu sanani oladi, bo'lmasa today
        date_str = request.GET.get("date")
        if date_str:
            try:
                today = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
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
            if avg_conf < 0.35:
                state = "normal"
                state_display = "Taxminiy"
            else:
                if avg_stress >= 0.80 or avg_neg >= 0.60:
                    state = "critical"
                    state_display = "Jiddiy"
                elif avg_stress >= 0.65 or avg_neg >= 0.45:
                    state = "warning"
                    state_display = "Ehtiyot"
                elif avg_stress < 0.30 and avg_mood > 75 and avg_energy > 0.70 and avg_neg < 0.25:
                    state = "excellent"
                    state_display = "A'lo"
                elif avg_stress < 0.45 and avg_mood > 65 and avg_neg < 0.35:
                    state = "good"
                    state_display = "Yaxshi"
                else:
                    state = "normal"
                    state_display = "O‘rtacha"

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

        return render(request, self.template_name, {
            "profiles": page_obj,
            "page_obj": page_obj,
            "paginator": paginator,
            "is_paginated": paginator.num_pages > 1,

            "breadcrumbs": breadcrumbs,
            "today": today,

            "role": role,
            "q": q,
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

    return render(request, "pages/service_logs.html", {
        "breadcrumbs": breadcrumbs,
        "services": services,
    })

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

    cmd = ["sudo", "systemctl", action, service]

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
        path = subprocess.check_output(["systemctl", "show", service, "--property=FragmentPath"], text=True).strip()
        path = path.replace("FragmentPath=", "").strip()

        if not path or not path.endswith(".service"):
            return JsonResponse({"success": False, "message": "Unit fayl topilmadi"})

        content = subprocess.check_output(["sudo", "cat", path], text=True)
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
        path = subprocess.check_output(["systemctl", "show", service, "--property=FragmentPath"], text=True).strip()
        path = path.replace("FragmentPath=", "").strip()

        if not path or not path.endswith(".service"):
            return JsonResponse({"success": False, "message": "Unit fayl topilmadi"})

        # temp file yozib keyin sudo mv
        tmp_path = f"/tmp/{service}.service"
        with open(tmp_path, "w") as f:
            f.write(content)

        subprocess.check_call(["sudo", "mv", tmp_path, path])
        subprocess.check_call(["sudo", "systemctl", "daemon-reload"])

        return JsonResponse({"success": True, "message": "Unit saqlandi va daemon-reload qilindi"})

    except Exception as e:
        return JsonResponse({"success": False, "message": str(e)})