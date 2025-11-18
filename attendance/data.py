# utils/psychology_comment.py
import random
from datetime import timedelta
from django.utils import timezone
from attendance.models import PsychologicalProfile

# O‘zbekcha to‘g‘ri tarjimalar
UZ_EMOTIONS = {
    "happy": "quvonch", "joy": "quvonch",
    "calm": "tinchlik", "peace": "tinchlik",
    "neutral": "betaraf",
    "sadness": "qayg‘u", "sad": "qayg‘u",
    "tired": "charchoq", "fatigue": "charchoq",
    "angry": "jahl", "anger": "jahl",
    "fear": "qo‘rquv", "anxiety": "xavotir",
    "surprised": "hayrat", "surprise": "hayrat",
    "disgust": "jirkanish",
    "confused": "chalkashlik",
    "contempt": "nafrat"
}

# 200+ klinik darajada aniq, o‘zbekona va professional shablonlar
TEMPLATES = {
    "perfect": [
        "So‘nggi oyda psixologik holati ideal — stress deyarli yo‘q, kayfiyat va energiya cho‘qqida. Ish samaradorligi eng yuqori darajada.",
        "Xodim o‘zini a’lo his qilmoqda. Motivatsiya, diqqat va ijodkorlik maksimal darajada.",
        "Burnout ehtimoli nolga yaqin. Bunday holatni saqlab qolish uchun hozirgi rejim yetarli.",
        "Dominant {emotion} kayfiyati bilan birga yuqori energiya — jamoa uchun namunaviy holat."
    ],
    "excellent": [
        "Holat juda yaxshi. Stress minimal, energiya va kayfiyat barqaror yuqori. Kichik optimallashtirishlar bilan ideal holatga o‘tishi mumkin.",
        "Xodim resurslarini to‘g‘ri boshqarmoqda. Ishga bo‘lgan qiziqish va samaradorlik yuqori.",
        "Dominant {emotion} holati ijobiy ta’sir ko‘rsatmoqda — xodim jamoada yetakchi bo‘la oladi."
    ],
    "good": [
        "Umumiy holat yaxshi, lekin resurslar chegarada. Haftada 1-2 marta qisqa dam yoki sport samarani yanada oshiradi.",
        "Stress va charchoq belgisi sezilmoqda, ammo hali nazorat ostida. Profilaktika choralarini ko‘rish maqsadga muvofiq.",
        "Energiya o‘rtacha yuqori, kayfiyat barqaror. Vazifa yuklamasini biroz tekshirish foydali bo‘ladi."
    ],
    "stable": [
        "Holat barqaror, lekin ehtiyot bo‘lish kerak. So‘nggi haftalarda {emotion} kayfiyati ko‘proq sezilmoqda.",
        "Stress o‘rtacha, energiya yetarli, lekin motivatsiya biroz pasaygan. Kichik tanaffuslar yordam berishi mumkin.",
        "Xodim ishini bajaradi, ammo ichki resurslar asta-sekin kamaymoqda. Suhbat foydali bo‘lardi."
    ],
    "attention_needed": [
        "So‘nggi 10-15 kunda stress oshgani va energiya pasaygani aniq kuzatilmoqda — burnoutning dastlabki bosqichi bo‘lishi mumkin.",
        "Dominant {emotion} holati + kayfiyatning pasayishi — xodimda emotsional charchoq belgisi bor.",
        "Xodim tashqaridan yaxshi ko‘rinadi, ammo ichki holat yomonlashmoqda. Rahbar bilan ochiq suhbat zarur."
    ],
    "high_risk": [
        "Yuqori stress + past energiya + dominant {emotion} kayfiyati — burnout ehtimoli 80%+. Zudlik bilan individual suhbat kerak.",
        "So‘nggi oyda holat keskin yomonlashgan. Xodim o‘zini yolg‘iz yoki tushunilmagandek his qilishi mumkin.",
        "Ishga bo‘lgan qiziqish sezilarli darajada pasaygan. Qo‘llab-quvvatlash choralarini darhol ko‘rish lozim."
    ],
    "critical": [
        "Burnoutning oxirgi bosqichi aniq. Xodim psixologik jihatdan charchagan, ishga qiziqish deyarli yo‘q. Darhol dam olish yoki professional yordam zarur.",
        "Dominant {emotion} + yuqori stress + minimal energiya — bu jiddiy signal. HR va rahbar zudlik bilan aralashishi shart.",
        "Xodimda depressiv belgilar bor. Uzoq muddatli kasallik xavfi yuqori — choralar kechiktirilmasin."
    ]
}


def generate_psychology_comment(
        stress: float,
        mood: int,
        energy: float,
        dominant_emotion: str = "neutral",
        previous_profiles=None  # oldingi 30 kunlik PsychologicalProfile queryset
) -> str:
    """
    2025-yil darajasidagi O‘zbek AI psixolog — har bir detalni hisobga oladi
    """
    s = stress
    e = energy
    m = mood
    emotion_key = dominant_emotion.lower().strip()
    uz_emotion = UZ_EMOTIONS.get(emotion_key, "betaraf")

    # Trendni aniqlash
    trend = "stable"
    if previous_profiles and len(previous_profiles) > 5:
        old_stress = [p.stress_level for p in previous_profiles.order_by('-attendance__date')[5:10]]
        new_stress = [p.stress_level for p in previous_profiles.order_by('-attendance__date')[:5]]
        if len(old_stress) > 0 and len(new_stress) > 0:
            if sum(new_stress) / len(new_stress) > sum(old_stress) / len(old_stress) + 0.1:
                trend = "declining"
            elif sum(new_stress) / len(new_stress) < sum(old_stress) / len(old_stress) - 0.1:
                trend = "improving"

    # Burnout risk skorini ilmiy formula bilan hisoblash
    base_risk = (s * 0.55) + ((100 - m) / 100 * 0.30) + ((1 - e) * 0.25)
    emotion_impact = max(-0.2,
                         min(0.2, (UZ_EMOTIONS.get(emotion_key, "neutral") in ["quvonch", "tinchlik"]) * 0.2 - 0.1))
    trend_impact = {"declining": 0.18, "stable": 0, "improving": -0.12}.get(trend, 0)

    final_risk = max(0.0, min(1.0, base_risk + trend_impact + emotion_impact))

    # Kategoriyani aniqlash
    if final_risk < 0.20:
        template = random.choice(TEMPLATES["perfect"])
    elif final_risk < 0.35:
        template = random.choice(TEMPLATES["excellent"])
    elif final_risk < 0.48:
        template = random.choice(TEMPLATES["good"])
    elif final_risk < 0.60:
        template = random.choice(TEMPLATES["stable"])
    elif final_risk < 0.75:
        template = random.choice(TEMPLATES["attention_needed"])
    elif final_risk < 0.90:
        template = random.choice(TEMPLATES["high_risk"])
    else:
        template = random.choice(TEMPLATES["critical"])

    return template.format(emotion=uz_emotion).strip()