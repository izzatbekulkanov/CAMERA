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
class PsychologicalProfileView(LoginRequiredMixin, View):
    login_url = 'login'
    template_name = "attendance/psychological_profile.html"

    def get(self, request, *args, **kwargs):
        today = timezone.localdate()

        # Faqat bugungi attendance ga tegishli profile-larni olish
        profiles_qs = PsychologicalProfile.objects.filter(
            attendance__date=today
        ).select_related(
            'attendance__user'
        ).order_by('attendance__user__full_name')

        # Foydalanuvchi bo‘yicha guruhlash
        user_data = {}
        for profile in profiles_qs:
            user = profile.attendance.user
            user_id = user.id

            if user_id not in user_data:
                user_data[user_id] = {
                    "user": user,
                    "stress_sum": 0.0,
                    "mood_sum": 0,
                    "energy_sum": 0.0,
                    "emotions": [],
                    "all_profiles": [],
                    "count": 0
                }

            data = user_data[user_id]
            data["stress_sum"] += profile.stress_level
            data["mood_sum"] += profile.mood_score
            data["energy_sum"] += profile.energy_level
            data["count"] += 1

            if profile.dominant_emotion:
                data["emotions"].append(profile.dominant_emotion.lower().strip())

            data["all_profiles"].append(profile)

        # Yakuniy natijalarni tayyorlash
        final_profiles = []
        for data in user_data.values():
            count = max(data["count"], 1)
            avg_stress = data["stress_sum"] / count
            avg_mood = int(data["mood_sum"] / count)
            avg_energy = data["energy_sum"] / count

            most_common_emotion = "neutral"
            if data["emotions"]:
                emotion_counts = Counter(data["emotions"])
                most_common_emotion = emotion_counts.most_common(1)[0][0]

            # AI comment generatsiyasi
            psychology_text = generate_psychology_comment(
                stress=avg_stress,
                mood=avg_mood,
                energy=avg_energy,
                dominant_emotion=most_common_emotion,
                previous_profiles=data["all_profiles"]
            )

            # Holat aniqlash
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
                "user": data["user"],
                "stress": int(avg_stress * 100),
                "mood": avg_mood,
                "energy": int(avg_energy * 100),
                "psychology": psychology_text,
                "state": state,
                "state_display": state_display,
                "count": data["count"],
                "dominant_emotion": most_common_emotion.title()
            })

        # Tartiblash: eng muhimlari yuqorida
        state_order = ["critical", "warning", "normal", "good", "excellent"]
        final_profiles.sort(
            key=lambda x: (state_order.index(x["state"]), -x["count"])
        )

        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Davomat', 'url': '/attendance/'},
            {'name': 'Psixologik portretlar', 'url': None},
        ]

        # Bugungi statistikalar
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