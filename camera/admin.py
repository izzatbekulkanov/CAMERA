from django.contrib import admin
from .models import Camera

@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip', 'port', 'username', 'is_active', 'enable_face_detection', 'added_at')
    list_filter = ('is_active', 'enable_face_detection', 'added_at')
    search_fields = ('ip', 'name', 'username')
    ordering = ['-is_active', 'ip']

    # Faqat o‘qish uchun maydonlar
    readonly_fields = ('added_at', 'last_checked')

    # Tahrirlash sahifasida maydonlar tartibi
    fieldsets = (
        ("Asosiy ma'lumotlar", {
            "fields": ("name", "ip", "port", "username", "password", "rtsp_url", "is_active")
        }),
        ("AI Funksiyalari", {
            "fields": ("enable_face_detection",)
        }),
        ("Vaqtlar", {
            "fields": ("added_at", "last_checked"),
        }),
    )

    # List sahifasi uchun ustunlarni kliklab o'tish
    list_display_links = ('name', 'ip')


from .models import Auditorium, Subject, Building, LessonMaterial

@admin.register(Building)
class BuildingAdmin(admin.ModelAdmin):
    list_display = ('name', 'description', 'created_at')
    search_fields = ('name', 'description')
    ordering = ['name']


@admin.register(Auditorium)
class AuditoriumAdmin(admin.ModelAdmin):
    list_display = ('name', 'building', 'camera', 'capacity', 'is_active', 'created_at')
    list_filter = ('is_active', 'building', 'camera')
    search_fields = ('name', 'description')
    ordering = ['name']


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'degree_level', 'is_active', 'created_at')
    list_filter = ('degree_level', 'is_active')
    search_fields = ('name', 'code', 'description')
    ordering = ['name']


@admin.register(LessonMaterial)
class LessonMaterialAdmin(admin.ModelAdmin):
    list_display = ('title', 'schedule', 'material_type', 'file_size', 'uploaded_by', 'created_at')
    list_filter = ('material_type', 'created_at')
    search_fields = ('title', 'schedule__subject__name', 'schedule__academic_group__name')
    readonly_fields = ('file_size', 'created_at')
    ordering = ['-created_at']
from .models import UnknownFaceCluster, UnknownFaceLog

@admin.register(UnknownFaceCluster)
class UnknownFaceClusterAdmin(admin.ModelAdmin):
    list_display = ('name', 'associated_user', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('name', 'associated_user__username', 'associated_user__first_name', 'associated_user__last_name')
    ordering = ['-created_at']


@admin.register(UnknownFaceLog)
class UnknownFaceLogAdmin(admin.ModelAdmin):
    list_display = ('camera', 'cluster', 'timestamp')
    list_filter = ('timestamp', 'camera', 'cluster')
    readonly_fields = ('timestamp', 'encoding_data')
    ordering = ['-timestamp']


from .models import FaceLog

@admin.register(FaceLog)
class FaceLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'matched_user', 'is_recognized', 'similarity', 'camera', 'camera_ip', 'age', 'gender', 'captured_at')
    list_filter = ('is_recognized', 'gender', 'captured_at', 'camera')
    search_fields = ('matched_user__username', 'matched_user__first_name', 'matched_user__last_name', 'camera__name', 'camera_ip', 'device_id')
    readonly_fields = ('captured_at', 'created_at', 'raw_metadata')
    ordering = ['-captured_at']

