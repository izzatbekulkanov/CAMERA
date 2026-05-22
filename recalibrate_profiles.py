import os
import django
import sys

sys.path.append('/home/smartgate/web/CAMERA')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import transaction
from attendance.models import PsychologicalProfile

STRESS_MAP = {"angry": 0.9, "anger": 0.9, "fear": 0.8, "sadness": 0.7, "neutral": 0.3, "happiness": 0.1, "surprise": 0.4, "disgust": 0.8, "contempt": 0.7}
ENERGY_MAP = {"angry": 0.6, "anger": 0.6, "fear": 0.5, "sadness": 0.4, "neutral": 0.5, "happiness": 0.9, "surprise": 0.8, "disgust": 0.3, "contempt": 0.2}
MOOD_MAP = {"angry": 30, "anger": 30, "fear": 40, "sadness": 35, "neutral": 60, "happiness": 90, "surprise": 70, "disgust": 20, "contempt": 25}

profiles = PsychologicalProfile.objects.all()
count = profiles.count()
print(f"Total profiles to recalibrate back to original ONNX system: {count}")

updated = 0
with transaction.atomic():
    for idx, p in enumerate(profiles, 1):
        dom = (p.dominant_emotion or "neutral").lower().strip()
        
        # Original stress mapping
        p.stress_level = STRESS_MAP.get(dom, 0.5)
        p.energy_level = ENERGY_MAP.get(dom, 0.5)
        p.mood_score = MOOD_MAP.get(dom, 50)
        p.save()
        
        updated += 1
        if idx % 5000 == 0 or idx == count:
            print(f"[{idx}/{count}] Processed stress level...")

print(f"\nRecalibration completed! Total profiles restored to original stress level: {updated}")
