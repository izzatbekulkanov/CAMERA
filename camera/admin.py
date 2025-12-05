from django.contrib import admin
from .models import Camera

@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip', 'port', 'username', 'is_active', 'added_at', 'last_checked')
    list_filter = ('is_active', 'added_at')
    search_fields = ('ip', 'name', 'username')
    ordering = ['-is_active', 'ip']

    # Faqat o‘qish uchun maydonlar
    readonly_fields = ('added_at', 'last_checked')

    # Tahrirlash sahifasida maydonlar tartibi
    fieldsets = (
        ("Asosiy ma'lumotlar", {
            "fields": ("name", "ip", "port", "username", "password", "is_active")
        }),
        ("Vaqtlar", {
            "fields": ("added_at", "last_checked"),
        }),
    )

    # List sahifasi uchun ustunlarni kliklab o'tish
    list_display_links = ('name', 'ip')
