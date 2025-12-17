import platform
import shutil
import subprocess

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
import os
from attendance.models import SiteSettings


def get_device_info():
    """
    Serverdagi CPU/GPU va umumiy tizim ma'lumotlari.
    """
    info = {
        "os": platform.platform(),
        "architecture": platform.machine(),
        "cpu_model": None,
        "cpu_cores": None,
        "cpu_logical_cores": None,
        "cpu_percent": None,
        "memory_total_gb": None,
        "memory_used_gb": None,
        "memory_available_gb": None,
        "memory_percent": None,
        "gpus": [],
        "gpu_backend": None,
        "psutil_ok": False,
        "torch_ok": False,
        "nvidia_smi_ok": False,
    }

    # CPU modeli (/proc/cpuinfo orqali ham tekshiramiz)
    cpu_model = platform.processor() or getattr(platform.uname(), "processor", "") or ""
    if not cpu_model and os.path.exists("/proc/cpuinfo"):
        try:
            with open("/proc/cpuinfo", "r") as f:
                for line in f:
                    if "model name" in line:
                        cpu_model = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    info["cpu_model"] = cpu_model or "Noma'lum"

    # CPU yadrolar, load va RAM (psutil bo'lsa)
    try:
        import psutil

        info["psutil_ok"] = True

        info["cpu_cores"] = psutil.cpu_count(logical=False)
        info["cpu_logical_cores"] = psutil.cpu_count(logical=True)
        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)

        mem = psutil.virtual_memory()
        info["memory_total_gb"] = round(mem.total / (1024**3), 2)
        info["memory_used_gb"] = round(mem.used / (1024**3), 2)
        info["memory_available_gb"] = round(mem.available / (1024**3), 2)
        info["memory_percent"] = mem.percent
    except Exception:
        pass

    # GPU: torch.cuda orqali
    try:
        import torch

        info["torch_ok"] = True

        if torch.cuda.is_available():
            count = torch.cuda.device_count()
            for i in range(count):
                name = torch.cuda.get_device_name(i)
                info["gpus"].append({
                    "name": name,
                    "memory_total_mb": None,
                })
            info["gpu_backend"] = "torch.cuda"
    except Exception:
        pass

    # GPU: nvidia-smi orqali
    try:
        if shutil.which("nvidia-smi"):
            info["nvidia_smi_ok"] = True
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                stderr=subprocess.STDOUT,
                encoding="utf-8"
            )
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            if lines:
                info["gpus"] = []
                for line in lines:
                    parts = [p.strip() for p in line.split(",")]
                    name = parts[0]
                    mem_mb = None
                    if len(parts) > 1:
                        mem_str = parts[1].replace("MiB", "").strip()
                        try:
                            mem_mb = int(mem_str)
                        except ValueError:
                            mem_mb = None
                    info["gpus"].append({
                        "name": name,
                        "memory_total_mb": mem_mb,
                    })
                info["gpu_backend"] = "nvidia-smi"
    except Exception:
        pass

    return info


@login_required(login_url="login")
def site_settings_view(request):
    site_settings, created = SiteSettings.objects.get_or_create(id=1)

    if request.method == "POST":
        # --- oddiy text fieldlar ---
        site_settings.site_name = request.POST.get("site_name", site_settings.site_name)
        site_settings.site_status = request.POST.get("site_status", site_settings.site_status)
        site_settings.contact_email = request.POST.get("contact_email", site_settings.contact_email)
        site_settings.contact_phone = request.POST.get("contact_phone", site_settings.contact_phone)
        site_settings.hemis_url = request.POST.get("hemis_url", site_settings.hemis_url)
        site_settings.hemis_api_token = request.POST.get("hemis_api_token", site_settings.hemis_api_token)

        site_settings.face_processing_device = request.POST.get(
            "face_processing_device",
            site_settings.face_processing_device
        )

        # ✅ AUTO FACE ENCODING toggle (checkbox)
        # HTML checkbox checked bo'lsa "on" keladi, bo'lmasa umuman kelmaydi
        site_settings.enable_auto_face_encoding = (request.POST.get("enable_auto_face_encoding") == "on")

        # --- logo uploadlar ---
        if request.FILES.get("logo_large"):
            site_settings.logo_large = request.FILES["logo_large"]

        if request.FILES.get("logo_small"):
            site_settings.logo_small = request.FILES["logo_small"]

        site_settings.save()

        messages.success(
            request,
            "Sayt sozlamalari muvaffaqiyatli saqlandi.",
            extra_tags="settings",
        )
        return redirect("site_settings")

    device_info = get_device_info()

    # psutil bo'lmasa ogohlantirish
    if not device_info.get("psutil_ok"):
        messages.warning(
            request,
            "RAM va CPU yuklanishi haqida batafsil ma’lumot uchun <code>psutil</code> paketini o‘rnatish tavsiya etiladi "
            "(pip install psutil)."
        )

    # GPU tanlangan, lekin GPU yo'q bo'lsa xabar
    if site_settings.face_processing_device == "gpu" and not device_info.get("gpus"):
        messages.error(
            request,
            "GPU rejimi tanlangan, lekin serverda GPU topilmadi yoki drayverlar o‘rnatilmagan. Iltimos, tekshiring "
            "yoki CPU rejimiga o‘ting."
        )

    breadcrumbs = [
        {"name": "Asosiy sahifa", "url": "/"},
        {"name": "Sayt sozlamalari", "url": None},
    ]

    context = {
        "breadcrumbs": breadcrumbs,
        "site_settings": site_settings,
        "page_title": "Sayt sozlamalari",
        "device_info": device_info,
    }

    return render(request, "pages/settings.html", context)