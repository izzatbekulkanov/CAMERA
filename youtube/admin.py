from django.contrib import admin
from .models import YouTubeProfile, YouTubeStream

@admin.register(YouTubeProfile)
class YouTubeProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "rtmp_url", "is_active", "created_at")
    search_fields = ("name",)
    list_filter = ("is_active",)

@admin.register(YouTubeStream)
class YouTubeStreamAdmin(admin.ModelAdmin):
    list_display = ("camera", "profile", "status", "ffmpeg_pid", "started_at", "stopped_at")
    list_filter = ("status", "profile")
    search_fields = ("camera__name", "profile__name")
