# attendance/data.py
import random
from datetime import timedelta
from django.utils import timezone

# FER+ 8-class mos tarjimalar
UZ_EMOTIONS = {
    "neutral": "betaraf",
    "happiness": "quvonch",
    "surprise": "hayrat",
    "sadness": "qayg‘u",
    "anger": "jahl",
    "disgust": "jirkanish",
    "fear": "qo‘rquv",
    "contempt": "nafrat",
}

TEMPLATES = {
    "perfect": [
        "So‘nggi davrda holat ideal: stress deyarli yo‘q, kayfiyat va energiya yuqori. Ish samaradorligi maksimal.",
        "Kayfiyat barqaror, resurslar yetarli. Burnout ehtimoli juda past.",
        "Dominant {emotion} va yuqori energiya — jamoada ijobiy dinamika beradi."
    ],
    "excellent": [
        "Holat juda yaxshi: stress past, energiya va kayfiyat barqaror yuqori. Shu rejimni ushlab turish tavsiya etiladi.",
        "Dominant {emotion} holati ijobiy; ishga bo‘lgan qiziqish va diqqat yaxshi."
    ],
    "good": [
        "Umumiy holat yaxshi, ammo resurslar chegarada bo‘lishi mumkin. Kichik tanaffuslar foydali.",
        "Stress o‘rtacha; profilaktik dam va uyqu rejimi holatni yaxshilaydi."
    ],
    "stable": [
        "Holat barqaror, lekin ehtiyot: so‘nggi kuzatuvlarda {emotion} holati ko‘proq sezilmoqda.",
        "Stress o‘rtacha; energiya yetarli, ammo motivatsiya pasayishi ehtimoli bor."
    ],
    "attention_needed": [
        "So‘nggi kunlarda stress oshgani va energiya pasaygani seziladi — burnoutning boshlanish belgilari bo‘lishi mumkin.",
        "Dominant {emotion} + kayfiyat pasayishi — rahbar/HR bilan yumshoq suhbat foydali bo‘ladi."
    ],
    "high_risk": [
        "Yuqori stress va past energiya — burnout xavfi yuqori. Zudlik bilan yuklamani yengillatish va qo‘llab-quvvatlash kerak.",
        "Dominant {emotion} holati uzoq davom etsa, emotsional charchoq kuchayishi mumkin."
    ],
    "critical": [
        "Jiddiy signal: stress yuqori, energiya past. Dam olish va professional yordam masalasini ko‘rib chiqish kerak.",
        "Dominant {emotion} + yuqori stress — HR va rahbar darhol aralashishi zarur."
    ],
    "low_confidence": [
        "Bugungi kadrlar sifati past (yuz noaniq/yorug‘lik yetarli emas). Natija taxminiy — qo‘shimcha kuzatuv kerak."
    ]
}

def generate_psychology_comment(
    stress: float,
    mood: int,
    energy: float,
    dominant_emotion: str = "neutral",
    previous_profiles=None,
    confidence: float | None = None,
    stability: float | None = None,
    negative_ratio: float | None = None,
) -> str:
    """
    FER+ (8 emotion) natijalariga mos, barqaror va izohli xulosa.
    """
    emotion_key = (dominant_emotion or "neutral").lower().strip()
    uz_emotion = UZ_EMOTIONS.get(emotion_key, "betaraf")

    # Agar ishonchlilik past bo'lsa - alohida shablon
    if confidence is not None and confidence < 0.35:
        return random.choice(TEMPLATES["low_confidence"]).strip()

    # Trend (oldingi profile-lar bo'lsa)
    trend_impact = 0.0

    if previous_profiles:
        # previous_profiles list yoki QuerySet bo‘lishi mumkin
        prev_list = list(previous_profiles)

        # Sana bo‘yicha saralash (eng yangisi oldinda)
        prev_list.sort(
            key=lambda p: p.attendance.date if p.attendance else None,
            reverse=True
        )

        if len(prev_list) >= 10:
            new_stress = [p.stress_level for p in prev_list[:5]]
            old_stress = [p.stress_level for p in prev_list[5:10]]

            avg_new = sum(new_stress) / len(new_stress)
            avg_old = sum(old_stress) / len(old_stress)

            if avg_new > avg_old + 0.10:
                trend_impact = 0.18
            elif avg_new < avg_old - 0.10:
                trend_impact = -0.12

    # Emotion impact (faqat FER+ bo'yicha)
    if emotion_key == "happiness":
        emotion_impact = -0.10
    elif emotion_key in ["sadness", "anger", "fear", "disgust", "contempt"]:
        emotion_impact = 0.10
    else:
        emotion_impact = 0.00

    # Risk score (barqaror formula)
    base_risk = (stress * 0.55) + ((100 - mood) / 100 * 0.30) + ((1 - energy) * 0.25)
    final_risk = max(0.0, min(1.0, base_risk + trend_impact + emotion_impact))

    # Kategoriya
    if final_risk < 0.20:
        key = "perfect"
    elif final_risk < 0.35:
        key = "excellent"
    elif final_risk < 0.48:
        key = "good"
    elif final_risk < 0.60:
        key = "stable"
    elif final_risk < 0.75:
        key = "attention_needed"
    elif final_risk < 0.90:
        key = "high_risk"
    else:
        key = "critical"

    text = random.choice(TEMPLATES[key]).format(emotion=uz_emotion).strip()

    # Qo'shimcha "individual" izoh (ixtiyoriy)
    extra = []
    if stability is not None:
        extra.append("Holat barqaror" if stability >= 0.70 else "Holat o‘zgaruvchan")
    if negative_ratio is not None:
        if negative_ratio >= 0.55:
            extra.append("salbiy affekt ulushi yuqori")
        elif negative_ratio >= 0.35:
            extra.append("salbiy affekt o‘rtacha")
        else:
            extra.append("salbiy affekt past")

    if extra:
        text += " (" + ", ".join(extra) + ")."
    return text
