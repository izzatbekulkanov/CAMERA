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

    # GPU: nvidia-smi orqali (to'liq)
    try:
        if shutil.which("nvidia-smi"):
            info["nvidia_smi_ok"] = True

            # 1) Umumiy GPU statistikasi
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit",
                    "--format=csv,noheader,nounits",
                ],
                stderr=subprocess.STDOUT,
                encoding="utf-8",
            )
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            gpus = []
            for line in lines:
                # csv -> 9 ta field
                parts = [p.strip() for p in line.split(",")]
                # name, total, used, free, util, temp, pdraw, plimit
                name = parts[0] if len(parts) > 0 else "Unknown GPU"

                def _to_int(x):
                    try:
                        return int(float(x))
                    except Exception:
                        return None

                def _to_float(x):
                    try:
                        return float(x)
                    except Exception:
                        return None

                total_mb = _to_int(parts[1]) if len(parts) > 1 else None
                used_mb = _to_int(parts[2]) if len(parts) > 2 else None
                free_mb = _to_int(parts[3]) if len(parts) > 3 else None
                util_pct = _to_int(parts[4]) if len(parts) > 4 else None
                temp_c = _to_int(parts[5]) if len(parts) > 5 else None
                p_draw_w = _to_float(parts[6]) if len(parts) > 6 else None
                p_lim_w = _to_float(parts[7]) if len(parts) > 7 else None

                gpus.append({
                    "name": name,
                    "memory_total_mb": total_mb,
                    "memory_used_mb": used_mb,
                    "memory_free_mb": free_mb,
                    "utilization_gpu_percent": util_pct,
                    "temperature_c": temp_c,
                    "power_draw_w": p_draw_w,
                    "power_limit_w": p_lim_w,
                })

            info["gpus"] = gpus
            info["gpu_backend"] = "nvidia-smi"

            # 2) GPU processlar (kim VRAM yeyapti)
            try:
                pout = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
                        "--format=csv,noheader,nounits",
                    ],
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                )
                plines = [line.strip() for line in pout.splitlines() if line.strip() and "No running" not in line]
                processes = []
                for line in plines:
                    p = [x.strip() for x in line.split(",")]
                    # gpu_uuid, pid, process_name, used_memory
                    processes.append({
                        "gpu_uuid": p[0] if len(p) > 0 else None,
                        "pid": int(p[1]) if len(p) > 1 and p[1].isdigit() else None,
                        "process_name": p[2] if len(p) > 2 else None,
                        "used_memory_mb": int(float(p[3])) if len(p) > 3 else None,
                    })
                info["gpu_processes"] = processes
            except Exception:
                info["gpu_processes"] = []

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
        site_settings.bot_token = request.POST.get("bot_token", site_settings.bot_token)
        site_settings.face_processing_device = request.POST.get("face_processing_device", site_settings.face_processing_device)

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


@login_required(login_url="login")
def site_settings_api_device_info(request):
    """
    JSON API that returns real-time hardware stats (CPU, RAM, GPU) for the site settings page.
    """
    from django.http import JsonResponse
    return JsonResponse(get_device_info())

