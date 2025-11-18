from django.db.models.signals import post_save
from django.dispatch import receiver
from django.conf import settings
from users.models import CustomUser, FaceEncoding
from users.tasks import create_face_encoding  # Celery task

@receiver(post_save, sender=CustomUser)
def enqueue_face_encoding(sender, instance, created, **kwargs):
    """
    Har safar foydalanuvchi rasm yuklasa,
    encoding yaratish Celery orqali background-da bajariladi.
    """
    if instance.image and instance.image.name:
        create_face_encoding.delay(instance.id)
