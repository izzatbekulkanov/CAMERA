# youtube/models.py
from django.db import models
from django.utils import timezone

# Sizdagi kamera modeli nomi boshqacha bo‘lsa moslang:
from camera.models import Camera


class YouTubeProfile(models.Model):
    name = models.CharField(max_length=120)
    rtmp_url = models.URLField(default="rtmp://a.rtmp.youtube.com/live2")
    stream_key = models.CharField(max_length=255)  # YouTube stream key
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    def __str__(self):
        return self.name


class YouTubeStream(models.Model):
    STATUS_CHOICES = (
        ("stopped", "Stopped"),
        ("starting", "Starting"),
        ("running", "Running"),
        ("failed", "Failed"),
    )

    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="youtube_streams")
    profile = models.ForeignKey(YouTubeProfile, on_delete=models.CASCADE, related_name="streams")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="stopped")
    last_error = models.TextField(blank=True, null=True)
    last_cmd = models.TextField(null=True, blank=True)
    # serverda ishga tushgan ffmpeg PID
    ffmpeg_pid = models.IntegerField(blank=True, null=True)

    started_at = models.DateTimeField(blank=True, null=True)
    stopped_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("camera", "profile")

    def __str__(self):
        return f"{self.camera} -> {self.profile} ({self.status})"
