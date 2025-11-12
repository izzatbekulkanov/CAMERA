from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils.translation import gettext_lazy as _

class CustomUser(AbstractUser):
    """
    Foydalanuvchi modeli — ham xodim, ham talaba uchun.
    """
    class Role(models.TextChoices):
        STUDENT = "student", _("Talaba")
        EMPLOYEE = "employee", _("Xodim")

    # umumiy ma’lumotlar
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.STUDENT,
        verbose_name=_("Foydalanuvchi turi")
    )
    full_name = models.CharField(max_length=255, verbose_name=_("To‘liq ism"), blank=True, null=True)
    short_name = models.CharField(max_length=255, verbose_name=_("Qisqa ism"), blank=True, null=True)
    first_name = models.CharField(max_length=100, verbose_name=_("Ism"), blank=True, null=True)
    second_name = models.CharField(max_length=100, verbose_name=_("Familiya"), blank=True, null=True)
    third_name = models.CharField(max_length=100, verbose_name=_("Otasining ismi"), blank=True, null=True)
    gender = models.CharField(max_length=20, verbose_name=_("Jinsi"), blank=True, null=True)
    birth_date = models.DateField(verbose_name=_("Tug‘ilgan sana"), blank=True, null=True)

    # identifikatsiya
    employee_id_number = models.CharField(max_length=50, verbose_name=_("Xodim ID raqami"), blank=True, null=True)
    student_id_number = models.CharField(max_length=50, verbose_name=_("Talaba ID raqami"), blank=True, null=True)
    year_of_enter = models.CharField(max_length=10, verbose_name=_("O‘qishga kirgan yili"), blank=True, null=True)

    # rasm (asosiy)
    image = models.ImageField(upload_to='users/images/', verbose_name=_("Rasm"), blank=True, null=True)

    # lavozim va bo‘limlar
    department_name = models.CharField(max_length=255, verbose_name=_("Bo‘lim nomi"), blank=True, null=True)
    department_code = models.CharField(max_length=100, verbose_name=_("Bo‘lim kodi"), blank=True, null=True)
    specialty = models.CharField(max_length=255, verbose_name=_("Mutaxassislik / lavozim"), blank=True, null=True)
    position = models.CharField(max_length=255, verbose_name=_("Lavozim (xodimlar uchun)"), blank=True, null=True)

    # talabalarga xos ma’lumotlar
    group_name = models.CharField(max_length=100, verbose_name=_("Guruh nomi"), blank=True, null=True)
    education_year = models.CharField(max_length=50, verbose_name=_("O‘quv yili"), blank=True, null=True)
    gpa = models.DecimalField(max_digits=4, decimal_places=2, verbose_name=_("O‘rtacha GPA"), blank=True, null=True)

    # status va vaqt
    active = models.BooleanField(default=True, verbose_name=_("Faol"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Yaratilgan vaqt"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Yangilangan vaqt"))

    def __str__(self):
        return self.full_name or self.username

    class Meta:
        verbose_name = _("Foydalanuvchi")
        verbose_name_plural = _("Foydalanuvchilar")


class FaceEncoding(models.Model):
    """
    Foydalanuvchi yuz ma’lumotlarini saqlovchi model.
    Bir foydalanuvchiga bir yoki bir nechta encoding yozuvi tegishli bo‘lishi mumkin.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="face_encodings", verbose_name=_("Foydalanuvchi"))
    encoding_data = models.JSONField(verbose_name=_("Yuz encoding ma’lumoti"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Yaratilgan vaqt"))

    def __str__(self):
        return f"{self.user.full_name or self.user.username} — {self.created_at.strftime('%Y-%m-%d %H:%M')}"

    class Meta:
        verbose_name = _("Yuz ma’lumoti")
        verbose_name_plural = _("Yuz ma’lumotlari")