# 🚪 SmartGate — Face Recognition & Attendance Monitoring System

**SmartGate** — Universitet kirish-chiqish turniketlari, IP kameralari orqali real-vaqtda yuzni aniqlash (Face Recognition), talabalar va xodimlar davomati, psixologik tahlil va xavfsizlik (huquqbuzarliklar) monitoringi tizimi.

---

## 📁 Loyiha Tuzilmasi

```
SmartGate/
├── core/                     # Django & Daphne ASGI sozlamalari
├── camera/                   # RTSP video oqimlari, Camera Daemon, AI tahlillar (InsightFace, YOLO)
├── attendance/               # Davomat jurnali, psixologik portret, xizmatlar statusi
├── users/                    # Foydalanuvchilar, rollar, yuz embeddinglari, HEMIS
├── youtube/                  # Video oqimlari integratsiyasi
├── models/                   # YOLOv8 va Liveness modellari
└── manage.py
```

---

## ⚙️ Asosiy Xizmatlar (Systemd)

* `daphne.service` — Asosiy ASGI serveri (Port 8000)
* `camera-daemon.service` — RTSP oqimlarini tahlil qiluvchi AI xizmati
* `celery.service` — Fon vazifalar va xabarnomalar
