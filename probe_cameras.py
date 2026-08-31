import os
import sys
import django
import subprocess
from urllib.parse import quote

# Setup Django environment
sys.path.append("/home/smartgate/web/CAMERA")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from camera.models import Camera
from camera.views import build_rtsp_candidates

def test_rtsp_url_with_ffmpeg(rtsp_url: str) -> bool:
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-rtsp_transport", "tcp",
        "-timeout", "2000000",  # 2 seconds timeout
        "-i", rtsp_url,
        "-frames:v", "1",
        "-f", "null",
        "-"
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4.0)
        return res.returncode == 0
    except Exception:
        return False

def main():
    print("=== KAMERALARNI RTSP PROBE QILISH BOSHLANDI ===")
    cameras = Camera.objects.filter(is_active=True)
    for cam in cameras:
        print(f"\nKamera: {cam.name} ({cam.ip})")
        print(f"Hozirgi RTSP URL: {cam.rtsp_url}")
        
        # Test existing first if set
        if cam.rtsp_url and test_rtsp_url_with_ffmpeg(cam.rtsp_url):
            print(f"  [OK] Hozirgi RTSP URL ishlamoqda!")
            continue
            
        # Try all candidates
        print("  Hozirgi URL ishlamayapti yoki bo'sh. Nomzodlarni tekshiramiz...")
        candidates = build_rtsp_candidates(cam)
        found = False
        for url in candidates:
            print(f"    Tekshirilmoqda: {url}")
            if test_rtsp_url_with_ffmpeg(url):
                print(f"    [MUVAFFARIYAT] Ishlaydigan URL topildi: {url}")
                cam.rtsp_url = url
                cam.save(update_fields=['rtsp_url'])
                print("    Bazaga saqlandi!")
                found = True
                break
        if not found:
            print("    [XATO] Hech qaysi RTSP nomzodi ishlamadi!")

if __name__ == "__main__":
    main()
