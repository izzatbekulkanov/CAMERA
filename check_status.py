import os
import django
import sys
from datetime import date

sys.path.append('/home/smartgate/web/CAMERA')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from attendance.models import PsychologicalProfile

target_date = date(2026, 5, 20)
profiles = PsychologicalProfile.objects.filter(attendance__date=target_date)
total = profiles.count()
print(f"Total profiles on {target_date}: {total}")

states = {
    "critical": 0,
    "warning": 0,
    "excellent": 0,
    "good": 0,
    "normal": 0
}

NEGATIVE_SET = {"anger", "fear", "sadness", "disgust", "contempt"}

for p in profiles:
    stress = float(p.stress_level or 0.0)
    conf = float(p.confidence or 0.0)
    neg_ratio = float(p.negative_ratio or 0.0)
    mood = int(p.mood_score or 50)
    energy = float(p.energy_level or 0.0)
    dom = (p.dominant_emotion or "").lower().strip()
    
    is_negative_dominant = dom in NEGATIVE_SET
    
    state = "normal"
    if conf < 0.35:
        state = "normal"
    else:
        if is_negative_dominant and conf >= 0.45 and stress >= 0.80 and neg_ratio >= 0.65:
            state = "critical"
        elif is_negative_dominant and conf >= 0.40 and stress >= 0.65 and neg_ratio >= 0.50:
            state = "warning"
        elif stress < 0.25 and mood > 75 and energy > 0.70 and neg_ratio < 0.20 and dom == "happiness":
            state = "excellent"
        elif stress < 0.40 and mood > 65 and neg_ratio < 0.30:
            state = "good"
            
    states[state] += 1

print("\nPsychological State Distribution on May 20th under NEW logic:")
for state, cnt in states.items():
    print(f"  - {state.upper()}: {cnt} ({cnt*100/max(total, 1):.1f}%)")
