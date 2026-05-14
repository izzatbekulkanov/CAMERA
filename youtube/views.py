from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from camera.models import Camera
from .models import YouTubeProfile, YouTubeStream

try:
    from .tasks import start_youtube_stream, stop_youtube_stream
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False


def normalize_stream_key(key: str) -> str:
    return (key or "").strip().replace(" ", "")


@login_required
def youtube_dashboard(request):
    cameras = Camera.objects.all().order_by("-is_active", "ip")
    profiles = YouTubeProfile.objects.filter(is_active=True).order_by("name")
    streams = YouTubeStream.objects.select_related("camera", "profile").order_by("-updated_at", "-created_at")
    return render(request, "youtube/dashboard.html", {
        "cameras": cameras,
        "profiles": profiles,
        "streams": streams,
    })


@require_POST
@login_required
def youtube_profile_create(request):
    name = (request.POST.get("name") or "").strip()
    stream_key = normalize_stream_key(request.POST.get("stream_key"))
    rtmp_url = (request.POST.get("rtmp_url") or "rtmp://a.rtmp.youtube.com/live2").strip()

    if not name:
        messages.error(request, "Profil nomi kiritilmadi.")
        return redirect("youtube_dashboard")

    if not stream_key or len(stream_key) < 10:
        messages.error(request, "Stream key noto‘g‘ri yoki juda qisqa.")
        return redirect("youtube_dashboard")

    if YouTubeProfile.objects.filter(stream_key=stream_key).exists():
        messages.warning(request, "Bu stream key allaqachon mavjud. Yangi profil saqlanmadi.")
        return redirect("youtube_dashboard")

    YouTubeProfile.objects.create(
        name=name,
        rtmp_url=rtmp_url,
        stream_key=stream_key,
        is_active=True
    )
    messages.success(request, "YouTube profil qo‘shildi.")
    return redirect("youtube_dashboard")


@login_required
def youtube_profile_delete(request, profile_id: int):
    p = get_object_or_404(YouTubeProfile, id=profile_id)
    # running stream bo‘lsa oldin stop qiling
    if YouTubeStream.objects.filter(profile=p, status__in=["starting", "running"]).exists():
        messages.error(request, "Avval shu profil bilan ishlayotgan stream(lar)ni to‘xtating.")
        return redirect("youtube_dashboard")
    p.delete()
    messages.success(request, "Profil o‘chirildi.")
    return redirect("youtube_dashboard")


@require_POST
@login_required
def youtube_start(request):
    camera_id = request.POST.get("camera_id")
    profile_id = request.POST.get("profile_id")

    camera = get_object_or_404(Camera, id=camera_id)
    profile = get_object_or_404(YouTubeProfile, id=profile_id, is_active=True)

    stream, _ = YouTubeStream.objects.get_or_create(camera=camera, profile=profile)

    if not CELERY_AVAILABLE:
        messages.error(request, "Celery yoqilmagan. YouTube streamni ishga tushirib bo‘lmaydi.")
        return redirect("youtube_dashboard")

    messages.info(request, "Stream ishga tushirilmoqda...")
    start_youtube_stream.delay(stream.id)
    return redirect("youtube_dashboard")


@login_required
def youtube_stop(request, stream_id: int):
    stream = get_object_or_404(YouTubeStream, id=stream_id)
    if not CELERY_AVAILABLE:
        messages.error(request, "Celery yoqilmagan. YouTube streamni to‘xtatib bo‘lmaydi.")
        return redirect("youtube_dashboard")

    messages.info(request, "Stream to‘xtatilmoqda...")
    stop_youtube_stream.delay(stream.id)
    return redirect("youtube_dashboard")


@login_required
def youtube_stream_delete(request, stream_id: int):
    s = get_object_or_404(YouTubeStream, id=stream_id)
    if s.status in ("starting", "running"):
        messages.error(request, "Avval streamni to‘xtating (running/starting).")
        return redirect("youtube_dashboard")
    s.delete()
    messages.success(request, "Stream yozuvi o‘chirildi.")
    return redirect("youtube_dashboard")


@login_required
def youtube_stream_info(request, stream_id: int):
    s = get_object_or_404(YouTubeStream.objects.select_related("camera", "profile"), id=stream_id)
    return JsonResponse({
        "id": s.id,
        "status": s.status,
        "pid": s.ffmpeg_pid,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "stopped_at": s.stopped_at.isoformat() if s.stopped_at else None,
        "last_error": s.last_error or "",
        "last_cmd": s.last_cmd or "",
        "camera": str(s.camera),
        "profile": s.profile.name,
        "rtmp_url": s.profile.rtmp_url,
    })
