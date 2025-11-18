from celery import shared_task
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
import os
import numpy as np
from PIL import Image
import onnxruntime as ort
from .models import Attendance, PsychologicalProfile

MODEL_PATH = os.path.join("static", "models", "emotion-ferplus-8.onnx")
SESSION = ort.InferenceSession(MODEL_PATH)
EMOTIONS = ['neutral', 'happiness', 'surprise', 'sadness', 'anger', 'disgust', 'fear', 'contempt']

STRESS_MAP = {"angry":0.9,"fear":0.8,"sadness":0.7,"neutral":0.3,"happiness":0.1,"surprise":0.4,"disgust":0.8,"contempt":0.7}
ENERGY_MAP = {"angry":0.6,"fear":0.5,"sadness":0.4,"neutral":0.5,"happiness":0.9,"surprise":0.8,"disgust":0.3,"contempt":0.2}
MOOD_MAP = {"angry":30,"fear":40,"sadness":35,"neutral":60,"happiness":90,"surprise":70,"disgust":20,"contempt":25}

def analyze_psychology_from_image(image_path):
    img = Image.open(image_path).convert("L").resize((64,64))
    arr = np.array(img).astype(np.float32).reshape(1,1,64,64)
    inputs = {SESSION.get_inputs()[0].name: arr}
    outputs = SESSION.run(None, inputs)
    scores = outputs[0][0]
    probs = np.exp(scores) / np.exp(scores).sum()
    idx = int(np.argmax(probs))
    dominant_emotion = EMOTIONS[idx]
    return {
        "dominant_emotion": dominant_emotion,
        "stress_level": STRESS_MAP.get(dominant_emotion,0.5),
        "energy_level": ENERGY_MAP.get(dominant_emotion,0.5),
        "mood_score": MOOD_MAP.get(dominant_emotion,50)
    }

@shared_task
def analyze_attendance_psychology(attendance_id):
    try:
        attendance = Attendance.objects.select_related('user').get(id=attendance_id)
        photos = list(attendance.photos.all())
        if not photos: return

        stress_vals, energy_vals, mood_vals, emotions = [], [], [], {}
        channel_layer = get_channel_layer()

        for idx, photo in enumerate(photos, start=1):
            if not os.path.exists(photo.image.path): continue
            try:
                res = analyze_psychology_from_image(photo.image.path)
            except: continue

            stress_vals.append(res["stress_level"])
            energy_vals.append(res["energy_level"])
            mood_vals.append(res["mood_score"])
            emo = res["dominant_emotion"]
            emotions[emo] = emotions.get(emo,0)+1

            async_to_sync(channel_layer.group_send)(
                "psychology_updates",
                {
                    "type":"progress_update",
                    "attendance_id":attendance.id,
                    "user_id":attendance.user.id,
                    "user_full_name":attendance.user.full_name,
                    "progress":round((idx/len(photos))*100)
                }
            )

        if stress_vals:
            avg_stress = sum(stress_vals)/len(stress_vals)
            avg_energy = sum(energy_vals)/len(energy_vals)
            avg_mood = sum(mood_vals)/len(mood_vals)
            dominant_emotion = max(emotions,key=emotions.get)
            summary = f"Stress:{avg_stress:.2f}, Energy:{avg_energy:.2f}, Mood:{avg_mood:.0f}, Dominant:{dominant_emotion}"

            PsychologicalProfile.objects.update_or_create(
                attendance=attendance,
                defaults={
                    "dominant_emotion":dominant_emotion,
                    "stress_level":avg_stress,
                    "energy_level":avg_energy,
                    "mood_score":avg_mood,
                    "summary_text":summary
                }
            )

            async_to_sync(channel_layer.group_send)(
                "psychology_updates",
                {
                    "type":"analysis_completed",
                    "attendance_id":attendance.id,
                    "user_id":attendance.user.id,
                    "user_full_name":attendance.user.full_name,
                    "dominant_emotion":dominant_emotion,
                    "stress_level":avg_stress,
                    "energy_level":avg_energy,
                    "mood_score":avg_mood,
                    "summary_text":summary,
                    "completed":True
                }
            )
    except Attendance.DoesNotExist:
        print(f"Attendance {attendance_id} not found")
