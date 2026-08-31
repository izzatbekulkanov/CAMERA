# AI NAMPSI Monitoring & Attendance System — Tizim Arxitekturasi va Tuzilmasi

Ushbu hujjat **ai.namspi.uz** intellektual monitoring va real-vaqt rejimida davomatni hisobga olish tizimining to'liq tarmoq, server va dasturiy ta'minot arxitekturasini batafsil tavsiflaydi.

---

## 1. Tarmoq va Virtualizatsiya Arxitekturasi

Tizim jismoniy server resurslarini optimallashtirish va xavfsizlikni ta'minlash maqsadida virtualizatsiya qilingan infratuzilmada ishlaydi.

```mermaid
graph TD
    User([Foydalanuvchi Brauzeri]) <-->|HTTPS / WSS| Nginx[nginx.namspi.uz<br>Tashqi Nginx Reverse Proxy]
    Nginx <-->|Proxy Port 8000| Proxmox[Proxmox VE Infratuzilmasi]
    Proxmox <-->|Virtual Machine| VM[10.10.0.40:8000<br>CAMERA Core VM]
    
    subgraph VM_Services [10.10.0.40 Ichki Servislari]
        Daphne[Daphne ASGI Server<br>Port 8000]
        Celery[Celery Worker<br>Background Tasks]
        Redis[(Redis DB)<br>Broker / Channels]
        Daemon[Camera Daemon<br>Continuous AI Analysis]
        Go2RTC[Go2RTC Server<br>MJPEG/RTSP Proxy]
    end
    
    VM <--> Daphne
    VM <--> Celery
    VM <--> Daemon
    VM <--> Go2RTC
```

### 1.1. Virtualizatsiya (Proxmox VE)
* **Xost Platforma**: Tizim **Proxmox VE** virtualizatsiya muhitida ishlovchi alohida Virtual Mashinada (VM) joylashtirilgan.
* **Core VM IP-Manzili**: `10.10.0.40`
* **Port bindings**: Tizim asosiy xizmatlari ichki port `8000` da ishlaydi.

### 1.2. Tashqi Reverse Proxy (Nginx)
* **Proxy Server**: `nginx.namspi.uz` domenida ishlovchi alohida xizmat (Reverse Proxy).
* **Kirish Domeni**: `ai.namspi.uz`
* **Yo'naltirish (Proxying)**: `nginx.namspi.uz` serveriga kelgan `ai.namspi.uz` so'rovlari va WebSocket oqimlari (WSS) tarmoq orqali ichki `10.10.0.40:8000` portiga xavfsiz va to'g'ridan-to'g'ri yo'naltiriladi.
* **WebSocket Qo'llab-quvvatlash**: Nginx konfiguratsiyasida WebSocket oqimlarini uzluksiz ishlashi uchun `Upgrade` va `Connection` sarlavhalari sozlangan.

---

## 2. Server Uskunalari va Resurslar (Hardware)

Tizim og'ir neyron tarmoq modellarini real-vaqt rejimida parallel qayta ishlashi uchun yuqori samaradorlikka ega uskunalar bilan jihozlangan:

* **GPU (Grafik Protsessor)**: **NVIDIA L40S** (48 GB VRAM, Ada Lovelace arxitekturasi). Barcha yuz aniqlash (RetinaFace), tanish (ArcFace) va sigaret chekishni aniqlash (YOLOv8) modellarini CUDA platformasi yordamida tezkor hisoblaydi.
* **RAM (Tezkor Xotira)**: **64 GB Total**.
* **CPU (Markaziy Protsessor)**: Ko'p yadroli virtualizatsiya qilingan protsessor resurslari.

---

## 3. Dasturiy Ta'minot Komponentlari (Software Stack)

Tizim Django ekotizimiga asoslangan va real-vaqt rejimida voqealarni uzatish (Event-driven) tamoyili bo'yicha ishlaydi.

```
+-----------------------------------------------------------------------+
|                       Foydalanuvchi Interfeysi                        |
|             (HTML5 HUD Dashboard, WebSockets, Light Theme)            |
+------------------------------------+----------------------------------+
                                     |
                                     v
+------------------------------------+----------------------------------+
|                   Daphne ASGI Server (Port 8000)                      |
|    - Handles HTTP Requests & Live WebSockets (stt, ipcamera, att)     |
+------------------------------------+----------------------------------+
                                     |
         +---------------------------+---------------------------+
         |                                                       |
         v                                                       v
+--------+-----------------------+              +----------------+-------+
|  Camera Daemon (RTSP Runner)   |              |        Celery Worker   |
|  - Continuous RTSP Capture    |              |  - Save Attendances    |
|  - InsightFace (GPU CUDA)     |              |  - Save Infractions    |
|  - YOLOv8 Smoke (GPU CUDA)    |              |  - Telegram Alerts     |
|  - Hikvision / Telegram Bot   |              |                        |
+--------+-----------------------+              +----------------+-------+
         |                                                       |
         +---------------------------+---------------------------+
                                     |
                                     v
                        +------------+------------+
                        |      Redis Channels     |
                        | (Message Broker & Cache)|
                        +-------------------------+
```

### 3.1. Daphne ASGI Server
* **Vazifasi**: Django ASGI serveri sifatida ishlaydi. HTTP so'rovlari bilan birga real-vaqtda audio va video oqimlarini qabul qiluvchi WebSockets ulanishlarini boshqaradi.
* **WebSocket Yo'nalishlari (`routing.py`)**:
  1. `ws/attendance/live/` — Dashboard uchun real-vaqtda darsdagi davomat statistikasini yangilaydi.
  2. `ws/ipcamera/<camera_id>/` — RTSP/Webcam video oqimini neyron tarmoqlar (InsightFace, YOLO) orqali kadrlar bo'yicha tahlil qilib, yuz va qoidabuzarlik ramkalari (bounding boxes) bilan birga brauzerga MJPEG kadrlar va JSON ma'lumotlar qaytaradi.
  3. `ws/stt/<schedule_id>/` — Brauzerdan kelayotgan jonli nutq audio oqimlarini (PCM/WAV) qabul qilib, o'zbek tili STT modeliga uzatadi va matn ko'rinishida qaytaradi.

### 3.2. Camera Daemon (`camera_daemon.py` & `rtsp_runner.py`)
* **Tavsifi**: systemd xizmati sifatida orqa fonda doimiy ishlovchi mustaqil Python jarayoni (`python manage.py camera_daemon`).
* **Vazifalari**:
  - Tizimdagi faol IP kameralarning RTSP oqimlarini muntazam kuzatadi.
  - Har bir kamera uchun mustaqil asinxron kadrlar o'qish va neyron tarmoq (AI) tahlili oqimlarini boshqaradi.
  - Talabalar va o'qituvchilar yuzlarini tahlil qilib, dars davomati ro'yxatini (`Attendance`) ma'lumotlar bazasida yangilaydi.
  - Dars jarayonida sigaret chekish yoki tutun kabi qoidabuzarliklarni aniqlaydi.
  - Kameralardan kelgan ma'lumotlarni Telegram Bot orqali ogohlantirish sifatida yuboradi.

### 3.3. Celery va Redis
* **Redis**: Xabarlar brokeri (Message Broker), Django Channels qatlami (Channel Layer) va kesh xizmati sifatida ishlaydi.
* **Celery**: Og'ir va uzoq vaqt oluvchi vazifalarni (masalan, aniqlangan yuz rasmlarini diskka yozish, bazaga davomat yozuvlarini qo'shish, Telegram xabarlarini jo'natish) asinxron navbatlar yordamida bajaradi. Bu Daphne va asosiy tizimning qotmasdan tez ishlashini ta'minlaydi.

### 3.4. Go2RTC Streaming Engine
* **Vazifasi**: Kameralardan kelayotgan xom RTSP oqimlarini yuqori tezlikda MJPEG va WebRTC formatlariga transkod va proxy qiluvchi ultra-past kechikishli video oqim serveri. Daphne video oqimini to'g'ridan-to'g'ri kameradan emas, balki Go2RTC keshi va transkoderi orqali oladi, bu esa kameralarning yuklamasini kamaytiradi.

---

## 4. Sun'iy Intellekt Modellar Tizimi (AI Pipelines)

Barcha neyron tarmoqlari maksimal tezlik uchun **NVIDIA CUDA (onnxruntime-gpu / pytorch)** yordamida GPU drayverlarida parallel hisoblanadi:

1. **Yuz Aniqlash va Tanish (InsightFace / buffalo_l)**:
   - **RetinaFace**: Kadrdagi barcha insonlar yuzlarini 99% aniqlik bilan deteksiya qiladi.
   - **ArcFace**: Aniqlangan yuzlardan 512 o'lchamli vektorlar (Embeddings) olib, ularni ma'lumotlar bazasidagi talabalar profil vektorlari bilan solishtiradi va shaxsni aniqlaydi.
2. **Qoidabuzarliklarni Aniqlash (YOLOv8)**:
   - Maxsus o'qitilgan **YOLOv8n** modeli orqali sinfxonadagi insonlar, sigaretlar va tutunni aniqlab, chekuvchi shaxslarni real-vaqtda ogohlantiradi.
3. **Nutqni Matnga O'girish (Kotib Uzbek STT - Whisper)**:
   - Mahalliy serverda joylashgan, o'zbek nutqiga moslashtirilgan fine-tune qilingan **Kotib Whisper CT2** modeli yordamida darsdagi nutq va diarizatsiyani (kim gapirayotgani) real-vaqt rejimida matnga o'giradi.

---

## 5. Tizimdagi Kamchiliklarni Bartaraf Etish va Optimallashtirish (Performance Fixes)

Tizimning qotib ishlashi ("qotib ishlamoqda") muammosini hal qilish uchun quyidagi muhim me'moriy va dasturiy optimallashtirishlar amalga oshirildi:

### 5.1. Thread-Leak (Oqimlar Sizib Chiqishi) Bartaraf Etildi
* **Muammo**: `IpCameraConsumer` da har safar kamera ulanib-uzilganda yoki boshqa candidate oqimga o'tishda, eski `subprocess.Popen` jarayonlari va block qiluvchi `q.put(None)` sababli reader threadlar abadiy xotirada qolib ketgan. Daphne jarayonida **411 tagacha dangling (osilib qolgan) threadlar** to'planib, GIL (Global Interpreter Lock) band bo'lishiga va server qotishiga olib kelgan.
* **Yechim**: 
  - `_reader_thread` dagi `q.put(None)` chaqiruvi `q.put(None, timeout=1.0)` ga o'zgartirildi. Agar queue to'lib qolsa, thread qulflanib qolmaydi va blokdan chiqib o'ladi.
  - Har bir kadr o'qish sikli (RTSP candidate) `try...finally` blokiga olindi. Loop keyingi manzilga o'tishidan oldin joriy jarayonni `proc.terminate()` qilishi va joriy oqimni `reader.join(timeout=1.0)` orqali kafolatli tozalashi ta'minlandi.

### 5.2. AI Kadrlar Skalasi Optimallashtirildi
* **Yechim**: Kadrlar bo'yicha neyron tarmoq tahlili chastotasi har 2-kadrda emas, har **3-kadrda** (`self._frame_counter % 3 == 0`) ishlaydigan qilindi. Bu yuz deteksiyasi va YOLO modellarining GPU hisoblash yuki va CPU/GPU o'rtasidagi ma'lumot uzatish shinasini **33% ga kamaytiradi**, davomat aniqligiga esa mutlaqo ta'sir qilmaydi.

### 5.3. Xom Kadrlarni Chetlab O'tish (AI-Bypass)
* **Yechim**: AI o'chirilgan holatlarda (`ai=0` yoki `ai_enabled = False`) yoki model yuklanmaganda, kadrlar to'g'ridan-to'g'ri `imdecode/imencode` amallarini bajarmasdan brauzerga yuboriladi. Bu server protsessorining ortiqcha yuklanishini to'liq bartaraf etadi.
