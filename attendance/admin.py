# attendance/admin.py
from django.contrib import admin
from django.utils.html import format_html

from .models import Attendance, SiteSettings, AttendancePhoto, PsychologicalProfile


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'entry_time', 'exit_time', 'is_present', 'last_seen')
    list_filter = ('date', 'is_present', 'user__role')
    search_fields = ('user__full_name', 'user__username', 'user__student_id_number')
    date_hierarchy = 'date'
    ordering = ('-date', '-entry_time')

    # first_seen callable sifatida aniqlaymiz
    readonly_fields = ('get_first_seen', 'last_seen', 'entry_time', 'exit_time')

    def get_first_seen(self, obj):
        # Agar Attendance modelida first_seen yo‘q bo‘lsa, exit_time bilan qaytarsin yoki None
        return getattr(obj, 'first_seen', obj.entry_time)
    get_first_seen.short_description = "Birinchi ko‘rish"

    def has_add_permission(self, request):
        # Qo‘lda qo‘shishni cheklaymiz — faqat kamera orqali
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        # Faqat superuser tahrir qila oladi
        return request.user.is_superuser

@admin.register(AttendancePhoto)
class AttendancePhotoAdmin(admin.ModelAdmin):
    list_display = ('attendance', 'captured_at', 'image_tag')
    readonly_fields = ('image_tag',)

    def image_tag(self, obj):
        if obj.image:
            return format_html(f'<img src="{obj.image.url}" width="150"/>')
        return "No image"
    image_tag.short_description = "Surat"

@admin.register(PsychologicalProfile)
class PsychologicalProfileAdmin(admin.ModelAdmin):
    list_display = ('attendance', 'dominant_emotion', 'mood_score', 'stress_level', 'updated_at')
    readonly_fields = (
        'attendance',
        'dominant_emotion',
        'stress_level',
        'energy_level',
        'mood_score',
        'summary_text',
        'created_at',
        'updated_at'
    )

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ('site_name', 'site_status', 'contact_email', 'contact_phone')
    list_editable = ('site_status',)

    def has_add_permission(self, request):
        # Faqat bitta instance bo‘lishi kerak
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    fieldsets = (
        ('Sayt holati', {
            'fields': ('site_status', 'site_name')
        }),
        ('Logolar', {
            'fields': ('logo_dark', 'logo_light')
        }),
        ('HEMIS integratsiyasi', {
            'fields': ('hemis_url', 'hemis_api_token'),
            'description': 'HEMIS bilan avtomatik sinxronizatsiya uchun'
        }),
        ('Aloqa', {
            'fields': ('contact_email', 'contact_phone')
        }),
    )