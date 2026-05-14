#camera/models.py
from django.db import models

# Create your models here.

class Camera(models.Model):
    ip = models.CharField(max_length=15, unique=True, verbose_name="IP manzil")
    port = models.IntegerField(default=80, verbose_name="Port")
    username = models.CharField(max_length=50, default="admin", verbose_name="Foydalanuvchi")
    password = models.CharField(max_length=100, verbose_name="Parol")
    rtsp_url = models.URLField(blank=True, null=True, verbose_name="RTSP URL")
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Kamera nomi")
    is_active = models.BooleanField(default=False, verbose_name="Faol")

    # 🔥 Yangi qo‘shilgan field
    enable_face_detection = models.BooleanField(
        default=False,
        verbose_name="Yuzni aniqlash yoqilsinmi?"
    )

    added_at = models.DateTimeField(auto_now_add=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name or 'Kamera'} ({self.ip})"

    class Meta:
        verbose_name = "Kamera"
        verbose_name_plural = "Kameralar"
        ordering = ['-is_active', 'ip']