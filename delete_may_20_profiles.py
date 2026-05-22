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
count = profiles.count()
print(f"Total psychological profiles found for {target_date}: {count}")

if count > 0:
    deleted_count, _ = profiles.delete()
    print(f"Successfully deleted {deleted_count} psychological profiles for {target_date}.")
else:
    print("No psychological profiles found to delete for this date.")
