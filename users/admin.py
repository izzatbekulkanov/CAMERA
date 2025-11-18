from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from .models import CustomUser, FaceEncoding


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = (
        "id", "image_preview", "full_name", "role",
        "employee_id_number", "student_id_number",
        "department_name", "active", "created_at"
    )
    list_display_links = ("id", "full_name")
    list_filter = ("role", "department_name", "active", "gender")
    search_fields = (
        "full_name", "first_name", "second_name",
        "student_id_number", "employee_id_number", "department_name"
    )
    readonly_fields = ("created_at", "updated_at", "image_preview")
    ordering = ("-created_at",)
    list_per_page = 20

    fieldsets = (
        (_("Asosiy ma’lumotlar"), {
            "fields": (
                "username", "password", "role", "active",
                "full_name", "short_name", "first_name", "second_name", "third_name",
                "gender", "birth_date", "image", "image_preview",
            )
        }),
        (_("Identifikatsiya"), {
            "fields": (
                "employee_id_number", "student_id_number", "year_of_enter",
            )
        }),
        (_("Bo‘lim va lavozim"), {
            "fields": (
                "department_name", "department_code", "specialty", "position",
            )
        }),
        (_("Ta’lim ma’lumotlari (talabalar uchun)"), {
            "fields": (
                "group_name", "education_year", "gpa",
            )
        }),
        (_("Tizim ma’lumotlari"), {
            "fields": ("created_at", "updated_at")
        }),
        (_("Ruxsatlar"), {
            "classes": ("collapse",),
            "fields": (
                "is_active", "is_staff", "is_superuser",
                "groups", "user_permissions",
            ),
        }),
    )

    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="width:50px; height:50px; border-radius:50%; object-fit:cover;" />',
                obj.image.url
            )
        return "–"
    image_preview.short_description = _("Rasm ko‘rinishi")


@admin.register(FaceEncoding)
class FaceEncodingAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "user_full_name", "face_image_preview", "created_at")
    list_filter = ("created_at", "user__role")
    search_fields = ("user__full_name", "user__username", "user__email")
    readonly_fields = ("created_at", "face_image_preview", "encoding_data")
    list_per_page = 20

    fieldsets = (
        (_("Foydalanuvchi"), {"fields": ("user",)}),
        (_("Yuz ma’lumoti"), {
            "fields": ("face_image_preview", "encoding_data")
        }),
        (_("Tizim"), {"fields": ("created_at",)}),
    )

    def user_full_name(self, obj):
        return obj.user.full_name or obj.user.username
    user_full_name.short_description = _("Ism")

    def face_image_preview(self, obj):
        if obj.user.image:
            return format_html(
                '<img src="{}" style="width:60px; height:60px; border-radius:8px; object-fit:cover; border:2px solid #0ff;" />',
                obj.user.image.url
            )
        return "–"
    face_image_preview.short_description = _("Rasm")