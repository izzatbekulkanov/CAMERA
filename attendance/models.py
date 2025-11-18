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

    # Sayt logolari
    logo_dark = models.ImageField(
        upload_to='site_logos/',
        null=True,
        blank=True,
        help_text='Saytning qora (dark) logosi'
    )
    logo_light = models.ImageField(
        upload_to='site_logos/',
        null=True,
        blank=True,
        help_text='Saytning oq (light) logosi'
    )

    # HEMIS ma’lumotlari
    hemis_url = models.URLField(
        max_length=255,
        blank=True,
        help_text='HEMIS tizimining URL manzili'
    )
    hemis_api_token = models.CharField(
        max_length=255,
        blank=True,
        help_text='HEMIS API tokeni'
    )

    # Qo‘shimcha sayt sozlamalari
    site_name = models.CharField(max_length=100, default='NamDPI', help_text='Sayt nomi')
    contact_email = models.EmailField(blank=True, help_text='Asosiy kontakt email')
    contact_phone = models.CharField(max_length=50, blank=True, help_text='Asosiy kontakt telefoni')

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Site Settings ({self.site_name})"

    class Meta:
        verbose_name = "Site Settings"
        verbose_name_plural = "Site Settings"

    def save(self, *args, **kwargs):
        """Ensure only one instance exists."""
        if not self.pk and SiteSettings.objects.exists():
            raise ValueError('Faqat bitta SiteSettings instance bo‘lishi mumkin')
        return super().save(*args, **kwargs)


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
    attendance = models.OneToOneField(
        Attendance,
        on_delete=models.CASCADE,
        related_name='psychology'
    )

    # AI orqali aniqlangan holatlar
    dominant_emotion = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="Masalan: happy, sad, angry, neutral, surprised..."
    )
    stress_level = models.FloatField(
        default=0,
        help_text="0.0 - 1.0 oralig‘ida stress darajasi"
    )
    energy_level = models.FloatField(
        default=0,
        help_text="0.0 - 1.0 oralig‘ida energiya darajasi"
    )
    mood_score = models.IntegerField(
        default=50,
        help_text="0 - 100 oralig‘ida psixologik holat"
    )

    summary_text = models.TextField(
        null=True,
        blank=True,
        help_text="AI tomonidan yaratilgan psixologik tavsif"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Psixologik Portret"
        verbose_name_plural = "Psixologik Portretlar"

    def __str__(self):
        return f"{self.attendance.user.full_name} — {self.attendance.date} psixologik tahlil"