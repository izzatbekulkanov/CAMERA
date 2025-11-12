from django.db import models

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
