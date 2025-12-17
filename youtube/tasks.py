# youtube/tasks.py
import os
import time
import signal
import subprocess
from urllib.parse import quote

from celery import shared_task
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import YouTubeStream


# =========================================================
# CONFIG
# =========================================================
# Kamera audiosi yo'q bo'lsa ham YouTube ko'rsatsin desangiz True qiling.
# Siz aytgan talab bo'yicha default: False (audio bo'lmasa yubormaydi).
SILENT_AUDIO_FALLBACK = True

# YouTube uchun tavsiya: keyframe interval ~2 sec
# Sizda kamera 20fps bo'lsa: 2 sec => 40
DEFAULT_GOP = 40

# 1440p uchun kamida ~6000k tavsiya (siz sinab ishlatgansiz)
DEFAULT_VBITRATE = "6000k"
DEFAULT_MAXRATE = "6000k"
DEFAULT_BUFSIZE = "12000k"

THREAD_QUEUE_SIZE = "512"


# =========================================================
# Process helpers
# =========================================================
def kill_ffmpeg_pid(pid: int):
    """FFmpeg process/group ni xavfsiz o‘chirish"""
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        # fallback: oddiy kill
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


# =========================================================
# WebSocket helper
# =========================================================
def ws_send(payload: dict):
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        "youtube_stream_updates",
        {"type": "stream_update", "payload": payload},
    )


# =========================================================
# RTSP url builder (password/url-safe)
# =========================================================
def build_rtsp_url(camera) -> str:
    u = quote(camera.username or "", safe="")
    p = quote(camera.password or "", safe="")
    ip = camera.ip
    # Default Hikvision path
    return f"rtsp://{u}:{p}@{ip}:554/Streaming/Channels/101"


# =========================================================
# Probe helpers
# =========================================================
def rtsp_has_audio(rtsp_url: str, timeout_sec: int = 6) -> bool:
    """
    RTSP ichida audio stream bormi tekshiradi.
    ffprobe yo'q bo'lsa yoki xatolik bo'lsa => False qaytaradi.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-rtsp_transport", "tcp",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        rtsp_url,
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=timeout_sec)
        return bool((out or "").strip())
    except Exception:
        return False


# =========================================================
# FFmpeg command builders
# =========================================================
def build_ffmpeg_test_cmd(rtsp_url: str) -> list[str]:
    """
    RTSP ishlashini tekshirish: 3 soniya stream o‘qib ko‘radi.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", "3",
        "-f", "null",
        "-"
    ]


def build_ffmpeg_stream_cmd(
    rtsp_url: str,
    rtmp_url: str,
    stream_key: str,
    use_camera_audio: bool,
    silent_audio_fallback: bool = SILENT_AUDIO_FALLBACK,
) -> list[str]:
    """
    use_camera_audio=True bo'lsa: kameradan audio bo'lsa yuboradi (0:a?).
    use_camera_audio=False bo'lsa:
        - silent_audio_fallback=True bo'lsa: silent audio qo'shib yuboradi
        - aks holda: audio yubormaydi (-an)
    """
    out = f"{rtmp_url.rstrip('/')}/{stream_key}"

    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",

        # RTSP input
        "-thread_queue_size", THREAD_QUEUE_SIZE,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
    ]

    # ====== Audio mapping ======
    maps: list[str] = ["-map", "0:v:0"]

    if use_camera_audio:
        # 0:a:0? => audio bo'lmasa ham fail qilmasin
        maps += ["-map", "0:a:0?"]
        audio = [
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
        ]
    else:
        if silent_audio_fallback:
            # silent audio input qo'shamiz
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
            maps += ["-map", "1:a:0"]
            audio = [
                "-c:a", "aac",
                "-b:a", "128k",
                "-ar", "44100",
                "-ac", "2",
            ]
        else:
            # audio umuman yubormaymiz
            audio = ["-an"]

    # ====== Video encoding ======
    # Sizda terminalda libx264 ishlagan — shu variantni barqaror qilib qoldiramiz.
    video = [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",

        "-b:v", DEFAULT_VBITRATE,
        "-maxrate", DEFAULT_MAXRATE,
        "-bufsize", DEFAULT_BUFSIZE,

        "-g", str(DEFAULT_GOP),
        "-keyint_min", str(DEFAULT_GOP),
        "-sc_threshold", "0",

        "-pix_fmt", "yuv420p",
    ]

    cmd += maps + video + audio + ["-f", "flv", out]
    return cmd


def _run_ffmpeg_check(cmd: list[str], timeout_sec: int = 6) -> tuple[bool, str]:
    """
    cmd ni ishga tushirib, timeout ichida natijani oladi.
    True => ok
    False => err_text
    """
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, err = p.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            # timeout bo'lsa — bu ham ok bo'lishi mumkin (stream o'qiyapti)
            p.kill()
            return True, ""

        if p.returncode == 0:
            return True, ""
        return False, (err or "").strip()
    except Exception as e:
        return False, str(e)


# =========================================================
# Celery tasks
# =========================================================
@shared_task(bind=True)
def start_youtube_stream(self, stream_id: int):
    try:
        stream = YouTubeStream.objects.select_related("camera", "profile").get(id=stream_id)
    except YouTubeStream.DoesNotExist:
        return

    # agar allaqachon running bo'lsa qayta boshlamaymiz
    if stream.status in ("starting", "running") and stream.ffmpeg_pid:
        return

    # Shu stream_key + rtmp_url bilan boshqa running/starting streamlarni to'xtatamiz
    dup_qs = YouTubeStream.objects.select_related("profile").filter(
        profile__rtmp_url=stream.profile.rtmp_url,
        profile__stream_key=stream.profile.stream_key,
        status__in=["starting", "running"],
    ).exclude(id=stream.id)

    for dup in dup_qs:
        if dup.ffmpeg_pid:
            kill_ffmpeg_pid(dup.ffmpeg_pid)

        dup.status = "stopped"
        dup.ffmpeg_pid = None
        dup.stopped_at = timezone.now()
        dup.last_error = "Auto-stopped: shu stream key bilan boshqa stream ishga tushirilgandi."
        dup.save(update_fields=["status", "ffmpeg_pid", "stopped_at", "last_error"])

        ws_send({
            "event": "auto_stopped",
            "stream_id": dup.id,
            "message": "Auto-stop: bitta stream keyga bitta stream qoidasi.",
        })

    rtsp_url = build_rtsp_url(stream.camera)

    # DB update (starting)
    stream.status = "starting"
    stream.last_error = ""
    stream.ffmpeg_pid = None
    stream.started_at = None
    stream.stopped_at = None
    if hasattr(stream, "last_cmd"):
        stream.last_cmd = ""
    stream.save()

    ws_send({"event": "starting", "stream_id": stream.id, "message": "RTSP tekshirilmoqda..."})

    # 1) RTSP test
    ok, err = _run_ffmpeg_check(build_ffmpeg_test_cmd(rtsp_url), timeout_sec=6)
    if not ok:
        stream.status = "failed"
        stream.last_error = f"RTSP ulanish xatosi: {err[:1800]}"
        stream.save(update_fields=["status", "last_error"])
        ws_send({"event": "failed", "stream_id": stream.id, "error": stream.last_error})
        return

    # 2) Audio bor-yo'qligini aniqlaymiz
    has_audio = rtsp_has_audio(rtsp_url)

    if has_audio:
        ws_send({"event": "starting", "stream_id": stream.id, "message": "Audio topildi. Stream boshlanmoqda..."})
    else:
        if SILENT_AUDIO_FALLBACK:
            ws_send({"event": "starting", "stream_id": stream.id, "message": "Audio yo'q. Silent audio bilan boshlanmoqda..."})
        else:
            ws_send({"event": "starting", "stream_id": stream.id, "message": "Audio yo'q. Audio yuborilmaydi (-an)..."})

    ws_send({"event": "starting", "stream_id": stream.id, "message": "FFmpeg ishga tushirilmoqda..."})

    cmd = build_ffmpeg_stream_cmd(
        rtsp_url,
        stream.profile.rtmp_url,
        stream.profile.stream_key,
        use_camera_audio=has_audio,
        silent_audio_fallback=SILENT_AUDIO_FALLBACK,
    )

    if hasattr(stream, "last_cmd"):
        stream.last_cmd = " ".join(cmd)
        stream.save(update_fields=["last_cmd"])

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

        # 3 soniya kutamiz: yiqilsa sababini olib failed qilamiz
        time.sleep(3.0)
        code = proc.poll()

        if code is not None:
            err_text = ""
            try:
                _, err_text = proc.communicate(timeout=1)
            except Exception:
                pass

            stream.status = "failed"
            stream.ffmpeg_pid = None
            stream.last_error = (err_text or "FFmpeg start bo‘lmadi. RTMP/StreamKey/YouTube tekshiring.").strip()[:1800]
            stream.save(update_fields=["status", "ffmpeg_pid", "last_error"])
            ws_send({"event": "failed", "stream_id": stream.id, "error": stream.last_error})
            return

        # running bo'ldi
        stream.status = "running"
        stream.ffmpeg_pid = proc.pid
        stream.started_at = timezone.now()
        stream.last_error = ""
        stream.save(update_fields=["status", "ffmpeg_pid", "started_at", "last_error"])

        ws_send({
            "event": "running",
            "stream_id": stream.id,
            "pid": proc.pid,
            "started_at": stream.started_at.isoformat(),
        })

    except Exception as e:
        stream.status = "failed"
        stream.ffmpeg_pid = None
        stream.last_error = str(e)[:1800]
        stream.save(update_fields=["status", "ffmpeg_pid", "last_error"])
        ws_send({"event": "failed", "stream_id": stream.id, "error": stream.last_error})


@shared_task(bind=True)
def stop_youtube_stream(self, stream_id: int):
    try:
        stream = YouTubeStream.objects.get(id=stream_id)
    except YouTubeStream.DoesNotExist:
        return

    if stream.ffmpeg_pid:
        kill_ffmpeg_pid(stream.ffmpeg_pid)

    stream.status = "stopped"
    stream.ffmpeg_pid = None
    stream.stopped_at = timezone.now()
    stream.save(update_fields=["status", "ffmpeg_pid", "stopped_at"])

    ws_send({"event": "stopped", "stream_id": stream.id})
