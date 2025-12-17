# attendance/models.py
from django.db import models
from django.utils import timezone
from users.models import CustomUser


class SiteSettings(models.Model):
    # Sayt holati
    SITE_STATUS_CHOICES = [
        ('online', 'Online'),
        ('maintenance', 'Maintenance'),
        ('offline', 'Offline'),
    ]
    site_status = models.CharField(
        max_length=20,
        choices=SITE_STATUS_CHOICES,
        default='online',
        help_text='Saytning hozirgi holati'
    )

    # Face recognition qurilmasi
    FACE_DEVICE_CHOICES = [
        ('cpu', 'CPU'),
        ('gpu', 'GPU'),
    ]
    face_processing_device = models.CharField(
        max_length=10,
        choices=FACE_DEVICE_CHOICES,
        default='cpu',
        help_text="Face recognition qaysi qurilmada ishlasin"
    )

    # Bitta logo — katta va kichik o‘lcham uchun
    logo_large = models.ImageField(
        upload_to='site_logos/',
        null=True,
        blank=True,
        help_text='Saytning asosiy logosi (katta o‘lcham, masalan: header uchun)'
    )
    logo_small = models.ImageField(
        upload_to='site_logos/',
        null=True,
        blank=True,
        help_text='Saytning kichik logosi (masalan: navbar, favicon yonida)'
    )

    enable_auto_face_encoding = models.BooleanField(
        default=False,
        help_text="Agar yoqilgan bo‘lsa, foydalanuvchi rasm yuklaganda avtomatik face encoding yaratiladi"
    )

    # HEMIS sozlamalari
    hemis_url = models.URLField(max_length=255, blank=True, help_text='HEMIS API URL')
    hemis_api_token = models.CharField(max_length=255, blank=True, help_text='HEMIS API token')

    # Sayt haqida
    site_name = models.CharField(max_length=100, default='NamDPI', help_text='Sayt nomi')
    contact_email = models.EmailField(blank=True, help_text='Kontakt email')
    contact_phone = models.CharField(max_length=50, blank=True, help_text='Kontakt telefon')

    # Vaqt belgilari
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.site_name} - Sozlamalar"

    class Meta:
        verbose_name = "Sayt sozlamalari"
        verbose_name_plural = "Sayt sozlamalari"

    def save(self, *args, **kwargs):
        """Faqat bitta obyekt bo‘lishi shart (Singleton pattern)"""
        self.pk = 1  # Har doim ID=1 bo‘ladi
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls):
        """Har qanday joydan oson chaqirish uchun"""
        return cls.objects.first() or cls.objects.create(site_name="NamDPI")


class Attendance(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField(default=timezone.now, db_index=True)

    entry_time = models.DateTimeField(null=True, blank=True, verbose_name="Kirish vaqti")
    exit_time = models.DateTimeField(null=True, blank=True, verbose_name="Chiqish vaqti")
    last_seen = models.DateTimeField(auto_now=True, verbose_name="Oxirgi marta ko‘rilgan")

    is_present = models.BooleanField(default=True, verbose_name="Hozirda binoda")
    duration_minutes = models.IntegerField(default=0, verbose_name="Binoda bo‘lgan vaqt (daqiqa)")

    class Meta:
        unique_together = ('user', 'date')
        indexes = [models.Index(fields=['date', 'is_present'])]

    def __str__(self):
        return f"{self.user} — {self.date} ({'Binoda' if self.is_present else 'Chiqdi'})"


class AttendancePhoto(models.Model):
    attendance = models.ForeignKey(Attendance, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='attendance_photos/%Y/%m/%d/')
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-captured_at']


class PsychologicalProfile(models.Model):
    attendance = models.OneToOneField(Attendance, on_delete=models.CASCADE, related_name='psychology')

    dominant_emotion = models.CharField(max_length=50, null=True, blank=True,
                                        help_text="Masalan: happy, sad, angry, neutral, surprised...")
    stress_level = models.FloatField(default=0, help_text="0.0 - 1.0 oralig‘ida stress darajasi")
    energy_level = models.FloatField(default=0, help_text="0.0 - 1.0 oralig‘ida energiya darajasi")
    mood_score = models.IntegerField(default=50, help_text="0 - 100 oralig‘ida psixologik holat")

    summary_text = models.TextField(null=True, blank=True, help_text="AI tomonidan yaratilgan psixologik tavsif")
    # Qo'shimcha insightlar
    emotion_probs = models.JSONField(default=dict, blank=True)  # {"neutral":0.6,"sadness":0.1,...}
    confidence = models.FloatField(default=0.0)  # 0..1 (o'rtacha max_prob)
    stability = models.FloatField(default=0.0)  # 0..1 (dominant share)
    negative_ratio = models.FloatField(default=0.0)  # 0..1
    positive_ratio = models.FloatField(default=0.0)  # 0..1
    neutral_ratio = models.FloatField(default=0.0)  # 0..1
    valence = models.FloatField(default=0.0)  # -1..1
    arousal = models.FloatField(default=0.0)  # 0..1
    photo_count = models.IntegerField(default=0)

    # Sifat metrikalari (rasm sifatiga qarab ishonchlilik)
    face_quality = models.FloatField(default=0.0)  # 0..1 (blur+brightness)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Psixologik Portret"
        verbose_name_plural = "Psixologik Portretlar"

    def __str__(self):
        return f"{self.attendance.user.full_name} — {self.attendance.date} psixologik tahlil"
