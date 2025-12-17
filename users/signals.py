# users/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from users.models import CustomUser
from users.tasks import create_insightface_encoding
from attendance.models import SiteSettings

@receiver(post_save, sender=CustomUser)
def enqueue_face_encoding(sender, instance, created, **kwargs):
    if not instance.image or not instance.image.name:
        return
    s = SiteSettings.get_settings()
    if not getattr(s, "enable_auto_face_encoding", False):
        return

    # ❌ batch progressni buzadi — olib tashlang:
    # face_progress_reset(total=1, message="Face encoding navbatda (1 user)")

    create_insightface_encoding.delay(instance.id)  # run_id yo'q -> progressga tegmaydi