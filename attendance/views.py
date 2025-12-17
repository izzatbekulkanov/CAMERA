# attendance/views.py
import random
from collections import Counter

from django.shortcuts import render
from django.views import View
from django.utils import timezone

from attendance.data import generate_psychology_comment
from attendance.models import Attendance, PsychologicalProfile
from django.contrib.auth.mixins import LoginRequiredMixin


class AttendanceView(LoginRequiredMixin, View):
    login_url = 'login'  # agar foydalanuvchi login qilmagan bo'lsa
    template_name = "attendance/attendance.html"

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()
        attendances = Attendance.objects.filter(date=today).select_related('user').order_by('-last_seen')

        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Davomat', 'url': '/attendance/'},
            {'name': 'Bugungi davomat', 'url': None},
        ]

        context = {
            "attendances": attendances,
            "breadcrumbs": breadcrumbs,
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

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()

        profiles_qs = PsychologicalProfile.objects.filter(
            attendance__date=today
        ).select_related('attendance__user').order_by('attendance__user__full_name')

        # User bo'yicha guruhlash (agar bir userda ko'p profile bo'lsa ham)
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

            # O'rtacha metrikalar
            avg_stress = sum(float(p.stress_level or 0) for p in plist) / max(count, 1)
            avg_energy = sum(float(p.energy_level or 0) for p in plist) / max(count, 1)
            avg_mood = int(round(sum(int(p.mood_score or 0) for p in plist) / max(count, 1)))

            # Emotion: (1) dominant_emotion bo'yicha (2) probs bo'lsa probsdan top
            emotions = [ (p.dominant_emotion or "neutral").lower().strip() for p in plist if p.dominant_emotion ]
            most_common_emotion = Counter(emotions).most_common(1)[0][0] if emotions else "neutral"

            # Qo'shimcha insightlar: avg confidence/stability/ratios/valence/arousal + emotion_probs merge
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
                # probs mavjud bo'lsa dominantni probsdan aniqroq olamiz
                most_common_emotion = max(probs, key=probs.get)

            # AI comment (modelga mos parametrlar bilan)
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

            # State aniqlash (endi ko'proq indikator bilan)
            # Past confidence bo'lsa: holatni "normal"ga tushiramiz (taxminiy bo'lgani uchun)
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

                # UI uchun foizga o'giramiz
                "stress": int(round(avg_stress * 100)),
                "mood": int(avg_mood),
                "energy": int(round(avg_energy * 100)),

                "psychology": psychology_text,
                "state": state,
                "state_display": state_display,

                # qo'shimcha insightlar
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

        # Tartiblash: muhimlari yuqorida
        state_order = ["critical", "warning", "normal", "good", "excellent"]
        final_profiles.sort(key=lambda x: (state_order.index(x["state"]), -x["photo_count"]))

        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Davomat', 'url': '/attendance/'},
            {'name': 'Psixologik portretlar', 'url': None},
        ]

        critical_count = sum(1 for p in final_profiles if p["state"] == "critical")
        warning_count = sum(1 for p in final_profiles if p["state"] == "warning")

        return render(request, self.template_name, {
            "profiles": final_profiles,
            "breadcrumbs": breadcrumbs,
            "today": today,
            "total_employees": len(final_profiles),
            "critical_count": critical_count,
            "warning_count": warning_count,
        })