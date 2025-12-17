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
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
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
    INSIGHT_APP = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    INSIGHT_APP.prepare(ctx_id=0, det_size=(640, 640))
    print("[INSIGHTFACE] FaceAnalysis yuklandi! (Celery worker)")
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

def preprocess_gray64(pil_img: Image.Image, mode: int) -> np.ndarray:
    """
    FER model turlicha export bo‘lishi mumkin.
    mode 1/2/3 ni sinab eng yaxshi max_prob ni tanlaymiz.
    """
    img = pil_img.convert("L").resize((64, 64))
    x = np.asarray(img).astype(np.float32)

    if mode == 1:
        # 0..255
        return x
    elif mode == 2:
        # -1..1 (ko'p FER+ modellarda shu)
        return (x - 127.5) / 128.0
    else:
        # 0..1
        return x / 255.0

def face_quality_score(gray64: np.ndarray) -> float:
    """
    Soddalashtirilgan sifat: std (kontrast) yuqori bo‘lsa, yuz aniqroq.
    """
    s = float(np.std(gray64))
    return float(max(0.0, min(1.0, s * 2.5)))

def load_image_rgb(path: str) -> np.ndarray | None:
    try:
        img = Image.open(path).convert("RGB")
        return np.asarray(img)
    except Exception:
        return None

def get_best_face_crop(path: str) -> Image.Image | None:
    """
    InsightFace yordamida eng katta / eng ishonchli yuzni topib crop qiladi.
    Yuz topilmasa None qaytaradi.
    """
    if INSIGHT_APP is None:
        return None

    rgb = load_image_rgb(path)
    if rgb is None:
        return None

    faces = INSIGHT_APP.get(rgb)
    if not faces:
        return None

    # eng katta bbox'li yuzni tanlaymiz
    def area(f):
        x1, y1, x2, y2 = f.bbox
        return float(max(0, x2 - x1) * max(0, y2 - y1))

    best = max(faces, key=area)
    x1, y1, x2, y2 = best.bbox
    x1, y1, x2, y2 = int(max(0, x1)), int(max(0, y1)), int(x2), int(y2)

    # Crop + biroz padding
    h, w, _ = rgb.shape
    pad = int(0.15 * max(x2 - x1, y2 - y1))
    x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad); y2 = min(h, y2 + pad)

    if x2 <= x1 or y2 <= y1:
        return None

    face_rgb = rgb[y1:y2, x1:x2]
    return Image.fromarray(face_rgb)

def infer_one_from_pil(face_pil: Image.Image) -> dict | None:
    """
    Model batch=1 bo‘lgani uchun doim (1,1,64,64) yuboramiz.
    Auto-preprocess: mode 1/2/3 ichidan max_prob eng katta variant tanlanadi.
    """
    best = None

    for mode in (1, 2, 3):
        try:
            g = preprocess_gray64(face_pil, mode)
            x = g.reshape(1, 1, 64, 64).astype(np.float32)

            logits = SESSION.run(None, {INPUT_NAME: x})[0][0]  # (8,)
            probs = softmax_1d(logits)
            maxp = float(np.max(probs))
            idx = int(np.argmax(probs))

            cand = {
                "mode": mode,
                "dominant": EMOTIONS[idx],
                "probs": {EMOTIONS[i]: float(probs[i]) for i in range(len(EMOTIONS))},
                "max_prob": maxp,
                "quality": face_quality_score(g),
            }

            if (best is None) or (cand["max_prob"] > best["max_prob"]):
                best = cand
        except Exception:
            continue

    return best

def aggregate(results: list[dict]) -> dict:
    n = len(results)
    avg_probs = {e: 0.0 for e in EMOTIONS}
    counts = {e: 0 for e in EMOTIONS}
    conf_sum = 0.0
    qual_sum = 0.0

    for r in results:
        counts[r["dominant"]] += 1
        conf_sum += r["max_prob"]
        qual_sum += float(r.get("quality", 0.0))
        for e in EMOTIONS:
            avg_probs[e] += float(r["probs"].get(e, 0.0))

    for e in EMOTIONS:
        avg_probs[e] /= n

    dominant = max(counts, key=counts.get)
    confidence = conf_sum / n
    stability = max(counts.values()) / n

    negative_ratio = sum(avg_probs[e] for e in NEGATIVE_SET)
    positive_ratio = sum(avg_probs[e] for e in POSITIVE_SET)
    neutral_ratio = avg_probs.get("neutral", 0.0)

    valence = sum(avg_probs[e] * VALENCE_W.get(e, 0.0) for e in EMOTIONS)
    arousal = sum(avg_probs[e] * AROUSAL_W.get(e, 0.4) for e in EMOTIONS)

    return {
        "photo_count": n,
        "dominant_emotion": dominant,
        "emotion_probs": avg_probs,
        "confidence": float(confidence),
        "stability": float(stability),
        "negative_ratio": float(negative_ratio),
        "positive_ratio": float(positive_ratio),
        "neutral_ratio": float(neutral_ratio),
        "valence": float(max(-1.0, min(1.0, valence))),
        "arousal": float(max(0.0, min(1.0, arousal))),
        "face_quality": float(qual_sum / n),
    }

def map_metrics(agg: dict) -> tuple[float, float, int]:
    # Stress/energy/mood endi dominantga emas, distributionga asoslanadi
    stress = 0.70 * agg["negative_ratio"] + 0.30 * (1.0 - agg["confidence"])
    stress = float(max(0.0, min(1.0, stress)))

    energy = 0.75 * agg["arousal"] + 0.25 * agg["positive_ratio"]
    energy = float(max(0.0, min(1.0, energy)))

    mood = int(round((agg["valence"] + 1.0) * 50.0))
    mood = max(0, min(100, mood))
    return stress, energy, mood


# ---------------------------
# 4) Celery task
# ---------------------------
@shared_task(bind=True)
def analyze_attendance_psychology(self, attendance_id: int):
    channel_layer = get_channel_layer()
    group = "psychology_updates"

    try:
        attendance = Attendance.objects.select_related("user").get(id=attendance_id)
    except Attendance.DoesNotExist:
        return

    qs = AttendancePhoto.objects.filter(attendance=attendance).order_by("-captured_at")

    MAX_PHOTOS = 200
    photos = list(qs[:MAX_PHOTOS])

    paths = [p.image.path for p in photos if p.image and hasattr(p.image, "path") and os.path.exists(p.image.path)]
    total = len(paths)
    if total == 0:
        return

    results = []
    last_sent = -1
    last_send_time = 0.0

    # Agar websocket ko'p bo'lsa, haddan tashqari ko'p yubormaslik uchun throttle
    def send_progress(progress: int):
        nonlocal last_sent, last_send_time
        now = time.time()
        if progress >= last_sent + 5 or progress == 100:
            # vaqt bo'yicha ham cheklaymiz (har 0.25s dan tez yubormasin)
            if now - last_send_time < 0.25 and progress != 100:
                return
            last_sent = progress
            last_send_time = now
            async_to_sync(channel_layer.group_send)(group, {
                "type": "progress_update",
                "attendance_id": attendance.id,
                "user_id": attendance.user.id,
                "user_full_name": attendance.user.full_name,
                "progress": progress,
            })

    # 1) Har rasm -> face crop -> FER infer
    for i, path in enumerate(paths, start=1):
        face = get_best_face_crop(path)

        # yuz topilmasa: fallback sifatida originaldan ishlatamiz (lekin natija kuchsiz bo'lishi mumkin)
        if face is None:
            try:
                face = Image.open(path).convert("RGB")
            except Exception:
                face = None

        if face is not None:
            r = infer_one_from_pil(face)
            if r:
                results.append(r)

        send_progress(int(i * 100 / total))

    if not results:
        return

    agg = aggregate(results)
    stress, energy, mood = map_metrics(agg)

    # 30 kunlik tarix
    start_date = attendance.date - timedelta(days=30)
    prev_qs = PsychologicalProfile.objects.filter(
        attendance__user=attendance.user,
        attendance__date__lt=attendance.date,
        attendance__date__gte=start_date,
    ).select_related("attendance")

    # AI comment (confidence/stability/neg_ratio bilan)
    summary = generate_psychology_comment(
        stress=stress,
        mood=mood,
        energy=energy,
        dominant_emotion=agg["dominant_emotion"],
        previous_profiles=prev_qs,  # QuerySet bo'lib qoladi
        confidence=agg["confidence"],
        stability=agg["stability"],
        negative_ratio=agg["negative_ratio"],
    )

    top3 = sorted(agg["emotion_probs"].items(), key=lambda x: x[1], reverse=True)[:3]
    top3_text = ", ".join([f"{k}:{v:.2f}" for k, v in top3])

    summary_rich = (
        f"{summary}\n"
        f"Rasm: {agg['photo_count']} | Top: {top3_text} | "
        f"Conf:{agg['confidence']:.2f} | Stab:{agg['stability']:.2f} | "
        f"Neg:{agg['negative_ratio']:.2f} Pos:{agg['positive_ratio']:.2f} Neu:{agg['neutral_ratio']:.2f} | "
        f"Quality:{agg['face_quality']:.2f}"
    )

    with transaction.atomic():
        PsychologicalProfile.objects.update_or_create(
            attendance=attendance,
            defaults={
                "dominant_emotion": agg["dominant_emotion"],
                "stress_level": float(stress),
                "energy_level": float(energy),
                "mood_score": int(mood),
                "summary_text": summary_rich,

                # Agar modelga qo'shilgan bo'lsa:
                "emotion_probs": agg["emotion_probs"],
                "confidence": float(agg["confidence"]),
                "stability": float(agg["stability"]),
                "negative_ratio": float(agg["negative_ratio"]),
                "positive_ratio": float(agg["positive_ratio"]),
                "neutral_ratio": float(agg["neutral_ratio"]),
                "valence": float(agg["valence"]),
                "arousal": float(agg["arousal"]),
                "photo_count": int(agg["photo_count"]),
                "face_quality": float(agg["face_quality"]),
            }
        )

    # 100% yakuniy
    send_progress(100)

    async_to_sync(channel_layer.group_send)(group, {
        "type": "analysis_completed",
        "attendance_id": attendance.id,
        "user_id": attendance.user.id,
        "user_full_name": attendance.user.full_name,
        "dominant_emotion": agg["dominant_emotion"],
        "stress_level": float(stress),
        "energy_level": float(energy),
        "mood_score": int(mood),
        "summary_text": summary_rich,
        "completed": True,

        "emotion_probs": agg["emotion_probs"],
        "confidence": float(agg["confidence"]),
        "stability": float(agg["stability"]),
        "negative_ratio": float(agg["negative_ratio"]),
        "positive_ratio": float(agg["positive_ratio"]),
        "neutral_ratio": float(agg["neutral_ratio"]),
        "valence": float(agg["valence"]),
        "arousal": float(agg["arousal"]),
        "photo_count": int(agg["photo_count"]),
        "face_quality": float(agg["face_quality"]),
    })
