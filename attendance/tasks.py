import os
import time
from datetime import timedelta

import numpy as np
from PIL import Image

import onnxruntime as ort
from celery import shared_task
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from camera.device import get_face_runtime
from .models import Attendance, AttendancePhoto, PsychologicalProfile
from attendance.data import generate_psychology_comment

# ---------------------------
# 1) FER+ ONNX emotion model
# ---------------------------
MODEL_PATH = os.path.join("static", "models", "emotion-ferplus-8.onnx")

EMOTIONS = [
    "neutral", "happiness", "surprise", "sadness",
    "anger", "disgust", "fear", "contempt"
]

NEGATIVE_SET = {"anger", "fear", "sadness", "disgust", "contempt"}
POSITIVE_SET = {"happiness"}

# Valence / Arousal heuristics (0..1, -1..1)
VALENCE_W = {
    "happiness": 0.9, "neutral": 0.0, "surprise": 0.1, "sadness": -0.7,
    "anger": -0.8, "disgust": -0.85, "fear": -0.75, "contempt": -0.7
}
AROUSAL_W = {
    "happiness": 0.7, "neutral": 0.4, "surprise": 0.85, "sadness": 0.25,
    "anger": 0.8, "disgust": 0.55, "fear": 0.85, "contempt": 0.45
}

def _create_ort_session():
    face_runtime = get_face_runtime()
    providers = face_runtime["providers"]
    try:
        sess = ort.InferenceSession(MODEL_PATH, providers=providers)
        return sess
    except Exception:
        # fallback
        return ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])

SESSION = _create_ort_session()
INPUT_NAME = SESSION.get_inputs()[0].name


# -----------------------------------------
# 2) InsightFace face detector (GPU/CPU)
# -----------------------------------------
# Sizda allaqachon insightface ishlayotgani ko'rinib turibdi.
# Bu block task import vaqtida 1 marta modelni yuklaydi.
INSIGHT_APP = None
try:
    from insightface.app import FaceAnalysis
    # name="buffalo_l" odatda embedding + det beradi.
    face_runtime = get_face_runtime()
    INSIGHT_APP = FaceAnalysis(name="buffalo_l", providers=face_runtime["providers"])
    INSIGHT_APP.prepare(ctx_id=face_runtime["ctx_id"], det_size=(640, 640))
    print(f"[INSIGHTFACE] FaceAnalysis yuklandi! (Celery worker, {face_runtime['device_type'].upper()} mode)")
except Exception as e:
    INSIGHT_APP = None
    print("[INSIGHTFACE] Yuklanmadi:", str(e))


# ---------------------------
# 3) Helpers
# ---------------------------
def softmax_1d(x: np.ndarray) -> np.ndarray:
    x = x - np.max(x)
    e = np.exp(x)
    s = np.sum(e)
    return e / (s if s != 0 else 1.0)

STRESS_MAP = {"angry": 0.9, "anger": 0.9, "fear": 0.8, "sadness": 0.7, "neutral": 0.3, "happiness": 0.1, "surprise": 0.4, "disgust": 0.8, "contempt": 0.7}
ENERGY_MAP = {"angry": 0.6, "anger": 0.6, "fear": 0.5, "sadness": 0.4, "neutral": 0.5, "happiness": 0.9, "surprise": 0.8, "disgust": 0.3, "contempt": 0.2}
MOOD_MAP = {"angry": 30, "anger": 30, "fear": 40, "sadness": 35, "neutral": 60, "happiness": 90, "surprise": 70, "disgust": 20, "contempt": 25}

def analyze_psychology_from_image(image_path):
    img = Image.open(image_path).convert("L").resize((64, 64))
    arr = np.array(img).astype(np.float32).reshape(1, 1, 64, 64)
    inputs = {SESSION.get_inputs()[0].name: arr}
    logits = SESSION.run(None, inputs)[0][0]
    probs = np.exp(logits) / np.exp(logits).sum()
    idx = int(np.argmax(probs))
    dominant_emotion = EMOTIONS[idx]
    return {
        "dominant_emotion": dominant_emotion,
        "probs": {EMOTIONS[i]: float(probs[i]) for i in range(len(EMOTIONS))},
        "max_prob": float(probs[idx]),
        "stress_level": STRESS_MAP.get(dominant_emotion, 0.5),
        "energy_level": ENERGY_MAP.get(dominant_emotion, 0.5),
        "mood_score": MOOD_MAP.get(dominant_emotion, 50),
    }

@shared_task
def analyze_attendance_psychology(attendance_id: int):
    channel_layer = get_channel_layer()
    group = "psychology_updates"

    try:
        attendance = Attendance.objects.select_related("user").get(id=attendance_id)
    except Attendance.DoesNotExist:
        return

    photos = list(AttendancePhoto.objects.filter(attendance=attendance).order_by("-captured_at"))
    if not photos:
        return

    stress_vals, energy_vals, mood_vals, emotions = [], [], [], {}
    results = []
    
    total = len(photos)
    
    def send_progress(progress: int):
        async_to_sync(channel_layer.group_send)(group, {
            "type": "progress_update",
            "attendance_id": attendance.id,
            "user_id": attendance.user.id,
            "user_full_name": attendance.user.full_name,
            "progress": progress,
        })

    for idx, photo in enumerate(photos, start=1):
        if not photo.image or not hasattr(photo.image, "path") or not os.path.exists(photo.image.path):
            continue
        try:
            res = analyze_psychology_from_image(photo.image.path)
            stress_vals.append(res["stress_level"])
            energy_vals.append(res["energy_level"])
            mood_vals.append(res["mood_score"])
            emo = res["dominant_emotion"]
            emotions[emo] = emotions.get(emo, 0) + 1
            results.append(res)
        except Exception:
            continue

        send_progress(int(idx * 100 / total))

    if not stress_vals:
        return

    avg_stress = sum(stress_vals) / len(stress_vals)
    avg_energy = sum(energy_vals) / len(energy_vals)
    avg_mood = int(round(sum(mood_vals) / len(mood_vals)))
    dominant_emotion = max(emotions, key=emotions.get)

    n = len(results)
    avg_probs = {e: 0.0 for e in EMOTIONS}
    conf_sum = 0.0
    for r in results:
        conf_sum += r["max_prob"]
        for e in EMOTIONS:
            avg_probs[e] += r["probs"][e]
    for e in EMOTIONS:
        avg_probs[e] /= n

    confidence = conf_sum / n
    stability = max(emotions.values()) / n
    negative_ratio = sum(avg_probs[e] for e in NEGATIVE_SET)
    positive_ratio = sum(avg_probs[e] for e in POSITIVE_SET)
    neutral_ratio = avg_probs.get("neutral", 0.0)
    valence = sum(avg_probs[e] * VALENCE_W.get(e, 0.0) for e in EMOTIONS)
    arousal = sum(avg_probs[e] * AROUSAL_W.get(e, 0.4) for e in EMOTIONS)

    summary = generate_psychology_comment(
        stress=avg_stress,
        mood=avg_mood,
        energy=avg_energy,
        dominant_emotion=dominant_emotion,
        previous_profiles=PsychologicalProfile.objects.filter(
            attendance__user=attendance.user,
            attendance__date__lt=attendance.date,
            attendance__date__gte=attendance.date - timedelta(days=30),
        ).select_related("attendance"),
        confidence=confidence,
        stability=stability,
        negative_ratio=negative_ratio,
    )

    top3 = sorted(avg_probs.items(), key=lambda x: x[1], reverse=True)[:3]
    top3_text = ", ".join([f"{k}:{v:.2f}" for k, v in top3])

    summary_rich = (
        f"{summary}\n"
        f"Rasm: {n} | Top: {top3_text} | "
        f"Conf:{confidence:.2f} | Stab:{stability:.2f} | "
        f"Neg:{negative_ratio:.2f} Pos:{positive_ratio:.2f} Neu:{neutral_ratio:.2f} | "
        f"Quality:1.00"
    )

    with transaction.atomic():
        PsychologicalProfile.objects.update_or_create(
            attendance=attendance,
            defaults={
                "dominant_emotion": dominant_emotion,
                "stress_level": float(avg_stress),
                "energy_level": float(avg_energy),
                "mood_score": int(avg_mood),
                "summary_text": summary_rich,

                "emotion_probs": avg_probs,
                "confidence": float(confidence),
                "stability": float(stability),
                "negative_ratio": float(negative_ratio),
                "positive_ratio": float(positive_ratio),
                "neutral_ratio": float(neutral_ratio),
                "valence": float(max(-1.0, min(1.0, valence))),
                "arousal": float(max(0.0, min(1.0, arousal))),
                "photo_count": int(n),
                "face_quality": 1.0,
            }
        )

    # Calculate State and POST if critical/warning using original state thresholds
    state = "normal"
    state_display = "O'rtacha"
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

    if state in ["critical", "warning"]:
        try:
            import requests
            from django.utils import timezone
            
            id_num = attendance.user.student_id_number if attendance.user.role == "student" else attendance.user.employee_id_number
            if not id_num:
                id_num = attendance.user.username

            payload = {
                "system_id": "CAMERA_AI_01",
                "timestamp": timezone.now().isoformat(),
                "user": {
                    "role": attendance.user.role,
                    "id_number": id_num,
                    "full_name": attendance.user.full_name
                },
                "psychological_state": {
                    "status_code": state,
                    "status_display": state_display,
                    "stress_level": int(round(avg_stress * 100)),
                    "mood_score": int(avg_mood),
                    "energy_level": int(round(avg_energy * 100)),
                    "dominant_emotion": dominant_emotion
                },
                "metrics": {
                    "negative_ratio": round(negative_ratio, 2),
                    "confidence_score": round(confidence, 2),
                    "face_quality": 1.0
                },
                "ai_summary_text": summary.strip()
            }
            
            requests.post("https://dc.namspi.uz/rest/api/psdate", json=payload, timeout=5)
            print(f"[PSYCHOLOGY API] Ma'lumot {attendance.user.full_name} uchun yuborildi. Holat: {state}")
        except Exception as e:
            print(f"[PSYCHOLOGY API XATO] API ga jo'natishda xatolik: {str(e)}")

    send_progress(100)

    async_to_sync(channel_layer.group_send)(group, {
        "type": "analysis_completed",
        "attendance_id": attendance.id,
        "user_id": attendance.user.id,
        "user_full_name": attendance.user.full_name,
        "dominant_emotion": dominant_emotion,
        "stress_level": float(avg_stress),
        "energy_level": float(avg_energy),
        "mood_score": int(avg_mood),
        "summary_text": summary_rich,
        "completed": True,

        "emotion_probs": avg_probs,
        "confidence": float(confidence),
        "stability": float(stability),
        "negative_ratio": float(negative_ratio),
        "positive_ratio": float(positive_ratio),
        "neutral_ratio": float(neutral_ratio),
        "valence": float(valence),
        "arousal": float(arousal),
        "photo_count": int(n),
        "face_quality": 1.0,
    })
