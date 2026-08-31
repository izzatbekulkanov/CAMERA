#camera/models.py
from django.db import models
from django.utils import timezone

# Create your models here.

class Camera(models.Model):
    ip = models.CharField(max_length=15, unique=True, verbose_name="IP manzil")
    port = models.IntegerField(default=80, verbose_name="Port")
    username = models.CharField(max_length=50, default="admin", verbose_name="Foydalanuvchi")
    password = models.CharField(max_length=100, verbose_name="Parol")
    rtsp_url = models.URLField(blank=True, null=True, verbose_name="RTSP URL")
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Kamera nomi")
    is_active = models.BooleanField(default=False, verbose_name="Faol")

    # 🔥 Yangi qo‘shilgan fieldlar
    enable_face_detection = models.BooleanField(
        default=False,
        verbose_name="Yuzni aniqlash yoqilsinmi?"
    )

    enable_infraction_detection = models.BooleanField(
        default=False,
        verbose_name="Huquqbuzarliklarni aniqlash yoqilsinmi?"
    )

    # 📌 Kamera vazifalari (Bir vaqtning o'zida bir nechta rejim bo'lishi mumkin)
    is_entry_camera = models.BooleanField(
        default=False,
        verbose_name="Kirish kamerasimi?"
    )
    is_exit_camera = models.BooleanField(
        default=False,
        verbose_name="Chiqish kamerasimi?"
    )
    is_infraction_camera = models.BooleanField(
        default=False,
        verbose_name="Huquqbuzarlikni aniqlash kamerasimi?"
    )
    is_lesson_camera = models.BooleanField(
        default=False,
        verbose_name="Dars jarayonini tahlil qilish kamerasimi?"
    )

    # 📌 Qurilma identifikatorlari (Avtomatik aniqlanadi)
    mac_address = models.CharField(max_length=50, blank=True, null=True, verbose_name="MAC manzil")
    serial_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="Seriya raqami")
    device_model = models.CharField(max_length=100, blank=True, null=True, verbose_name="Qurilma modeli")

    added_at = models.DateTimeField(auto_now_add=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name or 'Kamera'} ({self.ip})"

    class Meta:
        verbose_name = "Kamera"
        verbose_name_plural = "Kameralar"
        ordering = ['-is_active', 'ip']


    @property
    def current_pair(self):
        return self.entry_pairs.filter(is_active=True).first() or self.exit_pairs.filter(is_active=True).first()


class InfractionLog(models.Model):
    INFRACTION_TYPES = [
        ('fight', 'Urush/Janjal'),
        ('smoking', 'Sigaret chekish'),
        ('sleeping', 'Darsda uxlash'),
        ('other', 'Boshqa huquqbuzarlik'),
    ]
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name='infractions', verbose_name="Kamera")
    infraction_type = models.CharField(max_length=50, choices=INFRACTION_TYPES, verbose_name="Huquqbuzarlik turi")
    image = models.ImageField(upload_to='infraction_photos/%Y/%m/%d/', null=True, blank=True, verbose_name="Rasm")
    video = models.FileField(upload_to='infraction_videos/%Y/%m/%d/', null=True, blank=True, verbose_name="Qisqa video")
    offender_name = models.CharField(max_length=100, default="Shaxsi aniqlanmadi", verbose_name="Qoidabuzar shaxsi")
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Sodir bo'lgan vaqt")
    confidence = models.FloatField(default=0.0, verbose_name="Ishonchlilik")
    is_resolved = models.BooleanField(default=False, verbose_name="Hal etildi")

    class Meta:
        verbose_name = "Huquqbuzarlik"
        verbose_name_plural = "Huquqbuzarliklar"
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.get_infraction_type_display()} - {self.camera.name or self.camera.ip} ({self.timestamp.strftime('%H:%M')})"


class CameraPair(models.Model):
    name = models.CharField(max_length=150, verbose_name="Bino / Nazorat Punkti Nomi")
    building = models.ForeignKey(
        'Building',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='camera_pairs',
        verbose_name="Bino"
    )
    entry_camera = models.ForeignKey(
        'Camera',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='entry_pairs',
        verbose_name="Kirish kamerasi"
    )
    exit_camera = models.ForeignKey(
        'Camera',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='exit_pairs',
        verbose_name="Chiqish kamerasi"
    )
    description = models.CharField(max_length=255, blank=True, null=True, verbose_name="Tavsif / Joylashuv")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Yaratilgan vaqt")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Tahrirlangan vaqt")

    class Meta:
        verbose_name = "Kamera Juftligi (Nazorat Punkti)"
        verbose_name_plural = "Kamera Juftliklari (Nazorat Punktlari)"
        ordering = ['name']

    def __str__(self):
        entry_name = self.entry_camera.name if self.entry_camera else '--'
        exit_name = self.exit_camera.name if self.exit_camera else '--'
        return f"{self.name} (Kirish: {entry_name} | Chiqish: {exit_name})"


class Building(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="Bino nomi")
    description = models.TextField(blank=True, null=True, verbose_name="Tavsif")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Bino"
        verbose_name_plural = "Binolar"
        ordering = ['name']

    def __str__(self):
        return self.name


class Auditorium(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="Auditoriya nomi")
    building = models.ForeignKey(
        'Building',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='auditoriums',
        verbose_name="Bino"
    )
    camera = models.ForeignKey(
        'Camera',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='auditoriums',
        verbose_name="Kamera"
    )
    description = models.TextField(blank=True, null=True, verbose_name="Izoh/Tavsif")
    capacity = models.IntegerField(default=30, verbose_name="Sig'imi (talaba)")
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Auditoriya"
        verbose_name_plural = "Auditoriyalar"
        ordering = ['name']

    def __str__(self):
        return self.name



class Subject(models.Model):
    DEGREE_LEVEL_CHOICES = [
        ('bachelor', 'Bakalavriat'),
        ('master', 'Magistratura'),
        ('doctoral', 'Doktorantura'),
        ('other', 'Boshqa'),
    ]
    name = models.CharField(max_length=200, unique=True, verbose_name="Fan nomi")
    code = models.CharField(max_length=50, blank=True, null=True, verbose_name="Fan kodi")
    degree_level = models.CharField(
        max_length=50,
        choices=DEGREE_LEVEL_CHOICES,
        default='bachelor',
        verbose_name="Ta'lim bosqichi"
    )
    description = models.TextField(blank=True, null=True, verbose_name="Izoh/Tavsif")
    syllabus = models.FileField(
        upload_to='subjects/syllabuses/',
        blank=True,
        null=True,
        verbose_name="Fan dasturi (Sillabus)"
    )
    is_active = models.BooleanField(default=True, verbose_name="Faol")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fan"
        verbose_name_plural = "Fanlar"
        ordering = ['name']

    def __str__(self):
        return self.name


class LessonPair(models.Model):
    SHIFT_CHOICES = [
        (1, "1-smena"),
        (2, "2-smena"),
        (3, "3-smena"),
    ]
    shift = models.IntegerField(choices=SHIFT_CHOICES, default=1, verbose_name="Smena")
    pair_number = models.IntegerField(verbose_name="Para raqami")
    start_time = models.TimeField(verbose_name="Boshlanish vaqti")
    end_time = models.TimeField(verbose_name="Tugash vaqti")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dars vaqti (Para)"
        verbose_name_plural = "Dars vaqtlari (Paralar)"
        unique_together = ('shift', 'pair_number')
        ordering = ['shift', 'pair_number']

    def __str__(self):
        return f"{self.get_shift_display()} - {self.pair_number}-para ({self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')})"


class LessonSchedule(models.Model):
    WEEKDAY_CHOICES = [
        (1, "Dushanba"),
        (2, "Seshanba"),
        (3, "Chorshanba"),
        (4, "Payshanba"),
        (5, "Juma"),
        (6, "Shanba"),
    ]

    academic_group = models.ForeignKey(
        'users.AcademicGroup',
        on_delete=models.CASCADE,
        related_name='schedules',
        verbose_name="Akademik guruh"
    )
    subject = models.ForeignKey(
        'Subject',
        on_delete=models.CASCADE,
        related_name='schedules',
        verbose_name="Fan"
    )
    auditorium = models.ForeignKey(
        'Auditorium',
        on_delete=models.CASCADE,
        related_name='schedules',
        verbose_name="Auditoriya"
    )
    teacher_name = models.CharField(max_length=255, blank=True, null=True, verbose_name="O'qituvchi F.I.SH")
    weekday = models.IntegerField(choices=WEEKDAY_CHOICES, verbose_name="Hafta kuni")
    lesson_pair = models.ForeignKey(
        LessonPair,
        on_delete=models.CASCADE,
        related_name='schedules',
        verbose_name="Dars vaqti (Para)",
        null=True
    )
    lesson_type = models.CharField(
        max_length=50,
        choices=[('lecture', 'Ma\'ruza'), ('seminar', 'Seminar'), ('lab', 'Laboratoriya')],
        default='lecture',
        verbose_name="Dars turi"
    )
    topic = models.CharField(max_length=255, blank=True, null=True, verbose_name="Dars mavzusi")
    is_passed = models.BooleanField(default=False, verbose_name="Dars o'tildi")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dars jadvali"
        verbose_name_plural = "Dars jadvallari"
        unique_together = ('academic_group', 'weekday', 'lesson_pair')
        ordering = ['weekday', 'lesson_pair']

    def __str__(self):
        return f"{self.academic_group.name} - {self.subject.name} ({self.get_weekday_display()}, {self.lesson_pair})"

    @property
    def teacher_image_url(self):
        if not self.teacher_name:
            return None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        # Normalization
        def normalize_name(s):
            return s.replace("‘", "'").replace("’", "'").replace("`", "'").strip().upper()
            
        normalized_teacher = normalize_name(self.teacher_name)
        tokens = [t for t in normalized_teacher.split() if t]
        if not tokens:
            return None
            
        family_name = tokens[0]
        initials = [t.replace('.', '') for t in tokens[1:] if t.replace('.', '')]
        
        candidates = User.objects.filter(role=User.Role.EMPLOYEE)
        
        for cand in candidates:
            cand_fullname = normalize_name(cand.full_name or "")
            cand_shortname = normalize_name(cand.short_name or "")
            
            if cand_fullname == normalized_teacher or cand_shortname == normalized_teacher:
                if cand.image:
                    return cand.image.url
                return None
                
            cand_tokens = [t for t in cand_fullname.split() if t]
            if cand_tokens and cand_tokens[0] == family_name:
                matches_initials = True
                for i, init in enumerate(initials):
                    if i + 1 < len(cand_tokens):
                        if not cand_tokens[i + 1].startswith(init[0]):
                            matches_initials = False
                            break
                    else:
                        matches_initials = False
                        break
                if matches_initials:
                    if cand.image:
                        return cand.image.url
                    return None
        return None

    @property
    def matched_teacher_fullname(self):
        if not self.teacher_name:
            return ""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        # Normalization
        def normalize_name(s):
            return s.replace("‘", "'").replace("’", "'").replace("`", "'").strip().upper()
            
        normalized_teacher = normalize_name(self.teacher_name)
        tokens = [t for t in normalized_teacher.split() if t]
        if not tokens:
            return self.teacher_name
            
        family_name = tokens[0]
        initials = [t.replace('.', '') for t in tokens[1:] if t.replace('.', '')]
        
        candidates = User.objects.filter(role=User.Role.EMPLOYEE)
        
        for cand in candidates:
            cand_fullname = normalize_name(cand.full_name or "")
            cand_shortname = normalize_name(cand.short_name or "")
            
            if cand_fullname == normalized_teacher or cand_shortname == normalized_teacher:
                return cand.full_name
                
            cand_tokens = [t for t in cand_fullname.split() if t]
            if cand_tokens and cand_tokens[0] == family_name:
                matches_initials = True
                for i, init in enumerate(initials):
                    if i + 1 < len(cand_tokens):
                        if not cand_tokens[i + 1].startswith(init[0]):
                            matches_initials = False
                            break
                    else:
                        matches_initials = False
                        break
                if matches_initials:
                    return cand.full_name
        return self.teacher_name


class LessonMaterial(models.Model):
    MATERIAL_TYPE_CHOICES = [
        ('image', 'Rasm'),
        ('video', 'Video'),
        ('presentation', 'Prezentatsiya'),
        ('document', 'Hujjat'),
        ('archive', 'Arxiv'),
        ('other', 'Boshqa'),
    ]

    schedule = models.ForeignKey(
        LessonSchedule,
        on_delete=models.CASCADE,
        related_name='materials',
        verbose_name="Dars jadvali"
    )
    title = models.CharField(max_length=255, verbose_name="Material nomi")
    file = models.FileField(upload_to='lesson_materials/%Y/%m/%d/', verbose_name="Fayl")
    material_type = models.CharField(
        max_length=30,
        choices=MATERIAL_TYPE_CHOICES,
        default='other',
        verbose_name="Material turi"
    )
    file_size = models.PositiveBigIntegerField(default=0, verbose_name="Fayl hajmi")
    uploaded_by = models.ForeignKey(
        'users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='lesson_materials',
        verbose_name="Yuklagan foydalanuvchi"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Yuklangan vaqt")

    class Meta:
        verbose_name = "Dars materiali"
        verbose_name_plural = "Dars materiallari"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.schedule_id} - {self.title}"


class LessonSummary(models.Model):
    schedule = models.ForeignKey(
        'LessonSchedule',
        on_delete=models.CASCADE,
        related_name='summaries',
        verbose_name="Dars jadvali"
    )
    date = models.DateField(default=timezone.now, verbose_name="Sana")
    summary_text = models.TextField(verbose_name="Dars xulosasi")
    
    # Yangi professional AI va RAG/LLM tahlil maydonlari
    relevance_score = models.IntegerField(default=100, verbose_name="Mavzuga moslik foizi")
    personal_life_distractions = models.IntegerField(default=0, verbose_name="Shaxsiy hayotga chalg'ishlar soni")
    professionalism_rating = models.CharField(max_length=100, default="Professional", verbose_name="Metodika professionalizmi")
    diarized_transcript = models.JSONField(default=list, blank=True, verbose_name="Diarizatsiya qilingan matn")
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Dars xulosasi"
        verbose_name_plural = "Dars xulosalari"
        unique_together = ('schedule', 'date')

    def __str__(self):
        return f"{self.schedule} - {self.date}"



class UnknownFaceCluster(models.Model):
    name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Klaster nomi")
    associated_user = models.ForeignKey(
        'users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='unknown_clusters',
        verbose_name="Bog'langan foydalanuvchi"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Noma'lum shaxs guruhi"
        verbose_name_plural = "Noma'lum shaxslar guruhlari"

    def __str__(self):
        return self.name or f"Guruh #{self.id}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_user = None
        if not is_new:
            try:
                old_user = UnknownFaceCluster.objects.get(pk=self.pk).associated_user
            except UnknownFaceCluster.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        # Agar foydalanuvchi biriktirilgan bo'lsa va u yangi bo'lsa (yoki o'zgargan bo'lsa)
        if self.associated_user and (is_new or old_user != self.associated_user):
            from users.models import FaceEncoding
            import numpy as np
            import os
            
            face_logs = self.faces.all()
            embeddings = []
            
            for log in face_logs:
                if log.encoding_data and len(log.encoding_data) == 512:
                    embeddings.append(np.array(log.encoding_data, dtype=np.float32))
                    
            if embeddings:
                # O'rtacha yuz embeddingini hisoblaymiz
                mean_embedding = np.mean(embeddings, axis=0)
                norm = np.linalg.norm(mean_embedding)
                if norm > 0:
                    mean_embedding = mean_embedding / norm
                
                # FaceEncoding yaratamiz yoki yangilaymiz
                fe, created = FaceEncoding.objects.get_or_create(
                    user=self.associated_user,
                    model_version="insightface_buffalo_l",
                    defaults={"encoding_data": mean_embedding.tolist()}
                )
                if not created:
                    fe.encoding_data = mean_embedding.tolist()
                    fe.save(update_fields=["encoding_data"])
                
                # Profil rasmini yangilash (agar mavjud bo'lmasa)
                if not self.associated_user.image and face_logs[0].image:
                    try:
                        self.associated_user.image.save(
                            os.path.basename(face_logs[0].image.name),
                            face_logs[0].image.file,
                            save=True
                        )
                    except Exception:
                        pass
                
                # Integratsiya bo'lgach, yuz loglarini tozalaymiz
                face_logs.delete()


class UnknownFaceLog(models.Model):
    camera = models.ForeignKey(
        Camera,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='unknown_logs',
        verbose_name="Kamera"
    )
    image = models.ImageField(
        upload_to='unknown_faces/%Y/%m/%d/',
        verbose_name="Yuz surati"
    )
    encoding_data = models.JSONField(verbose_name="Vektor ma'lumotlari (embedding)")
    cluster = models.ForeignKey(
        UnknownFaceCluster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='faces',
        verbose_name="Klaster"
    )
    timestamp = models.DateTimeField(default=timezone.now, verbose_name="Aniqlangan vaqt")

    class Meta:
        verbose_name = "Noma'lum yuz"
        verbose_name_plural = "Noma'lum yuzlar"
        ordering = ['-timestamp']

    def __str__(self):
        return f"Noma'lum yuz - {self.camera.name or self.camera.ip if self.camera else 'Nomalum'} ({self.timestamp.strftime('%H:%M')})"


class LessonTranscript(models.Model):
    """Dars davomida o'qituvchi nutqining matn yozuvi."""
    schedule = models.ForeignKey(
        LessonSchedule,
        on_delete=models.CASCADE,
        related_name='transcripts',
        verbose_name="Dars jadvali"
    )
    date = models.DateField(verbose_name="Sana")
    text = models.TextField(verbose_name="Transkript matni")
    segment_index = models.IntegerField(default=0, verbose_name="Segment tartibi")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Dars transkripti"
        verbose_name_plural = "Dars transkriptlari"
        ordering = ['date', 'segment_index']

    def __str__(self):
        return f"Schedule {self.schedule_id} | {self.date} | Seg {self.segment_index}"


class LessonSession(models.Model):
    """
    O'qituvchi dars boshlash/yakunlash vaqtlarini aniq saqlaydi.
    Har bir dars uchun bir yozuv (schedule + date bo'yicha unique).
    """
    schedule = models.ForeignKey(
        LessonSchedule,
        on_delete=models.CASCADE,
        related_name='sessions',
        verbose_name="Dars jadvali"
    )
    date = models.DateField(verbose_name="Sana")

    # Rejalashtirilgan vaqtlar (LessonPair dan ko'chiriladi)
    planned_start = models.TimeField(verbose_name="Rejalashtirilgan boshlanish", null=True, blank=True)
    planned_end = models.TimeField(verbose_name="Rejalashtirilgan tugash", null=True, blank=True)

    # O'qituvchi haqiqiy boshlash va yakunlash vaqtlari
    teacher_started_at = models.DateTimeField(null=True, blank=True, verbose_name="O'qituvchi darsni boshlagan vaqt")
    teacher_ended_at = models.DateTimeField(null=True, blank=True, verbose_name="O'qituvchi darsni tugatgan vaqt")

    # Dars yakunidagi to'liq tahlil natijasi (JSON)
    analysis_json = models.JSONField(null=True, blank=True, verbose_name="Tahlil natijalari")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dars sessiyasi"
        verbose_name_plural = "Dars sessiyalari"
        unique_together = ('schedule', 'date')
        ordering = ['-date']

    def __str__(self):
        return f"Session: {self.schedule} | {self.date}"

    @property
    def teacher_late_minutes(self):
        """O'qituvchi kechikish daqiqalari (0 = kechikmasdan boshladi)."""
        if not self.teacher_started_at or not self.planned_start:
            return None
        import datetime
        planned_dt = datetime.datetime.combine(self.date, self.planned_start)
        from django.utils import timezone
        actual_dt = timezone.localtime(self.teacher_started_at).replace(tzinfo=None)
        diff = (actual_dt - planned_dt).total_seconds()
        return max(0, int(diff // 60))

    @property
    def lesson_duration_minutes(self):
        """Dars davomiyligi daqiqalarda."""
        if not self.teacher_started_at or not self.teacher_ended_at:
            return None
        diff = (self.teacher_ended_at - self.teacher_started_at).total_seconds()
        return int(diff // 60)


class FaceLog(models.Model):
    """
    Hikvision va boshqa aqlli IP kameralardan (DS-2CD2686G2-IZS) tutilgan yuzlar logi.
    """
    camera = models.ForeignKey(
        'Camera',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='face_logs',
        verbose_name="Kamera"
    )
    device_id = models.CharField(max_length=100, blank=True, null=True, verbose_name="Qurilma ID (Hikvision)")
    channel_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="Kanal ID")
    camera_ip = models.GenericIPAddressField(blank=True, null=True, verbose_name="Kamera IP")
    
    # Rasmlar
    face_image = models.ImageField(
        upload_to='faces/%Y/%m/%d/',
        verbose_name="Yuz surati (Crop)"
    )
    background_image = models.ImageField(
        upload_to='faces_bg/%Y/%m/%d/',
        null=True,
        blank=True,
        verbose_name="Umumiy kadr surati"
    )
    
    # Voqea ma'lumotlari
    event_type = models.CharField(max_length=100, default="faceCapture", verbose_name="Voqea turi")
    event_description = models.CharField(max_length=255, blank=True, null=True, verbose_name="Voqea tavsifi")
    confidence = models.FloatField(default=0.0, verbose_name="Yuz ishonchliligi (Score)")
    
    # Atributlar (Hikvision AcuSense AI)
    age = models.IntegerField(null=True, blank=True, verbose_name="Taxminiy yosh")
    gender = models.CharField(max_length=20, blank=True, null=True, verbose_name="Jinsi")
    has_glasses = models.BooleanField(null=True, blank=True, verbose_name="Ko'zoynak")
    has_mask = models.BooleanField(null=True, blank=True, verbose_name="Niqob")
    face_rect = models.JSONField(default=dict, blank=True, verbose_name="Yuz koordinatalari")
    
    # Tizim foydalanuvchisi bilan bog'lash (Tanish / Matching natijasi)
    matched_user = models.ForeignKey(
        'users.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='face_logs',
        verbose_name="Tizim foydalanuvchisi"
    )
    similarity = models.FloatField(default=0.0, verbose_name="Moslik darajasi (Similarity)")
    is_recognized = models.BooleanField(default=False, verbose_name="Shaxsi aniqlandimi?")
    
    raw_metadata = models.JSONField(default=dict, blank=True, verbose_name="Xom XML/JSON metadata")
    
    captured_at = models.DateTimeField(default=timezone.now, verbose_name="Tutilgan vaqt (Kameradan)")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Yozilgan vaqt")
    
    class Meta:
        db_table = 'face_logs'
        verbose_name = "Yuz logi"
        verbose_name_plural = "Yuz loglari"
        ordering = ['-captured_at']
        indexes = [
            models.Index(fields=['-captured_at']),
            models.Index(fields=['camera', '-captured_at']),
            models.Index(fields=['matched_user', '-captured_at']),
        ]

    def __str__(self):
        user_str = self.matched_user.get_full_name() if self.matched_user else "Noma'lum"
        cam_str = self.camera.name if self.camera else (self.camera_ip or "Noma'lum kamera")
        return f"{user_str} - {cam_str} ({self.captured_at.strftime('%Y-%m-%d %H:%M:%S')})"

