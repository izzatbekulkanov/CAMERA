# core/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render


# ===============================
# 🔹 Custom error pages
# ===============================
def custom_404(request, exception):
    return render(request, "pages/404.html", status=404)


def custom_500(request):
    return render(request, "pages/500.html", status=500)


# ===============================
# 🔹 URL Patterns
# ===============================
urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),

    # Apps
    path('', include('attendance.urls')),
    path('', include('users.urls')),
    path('', include('camera.urls')),

    # i18n URL (tilni o‘zgartirish uchun)
    path('i18n/', include('django.conf.urls.i18n')),

    # Rosetta (translation editor)
    path('rosetta/', include('rosetta.urls')),
]

# ===============================
# 🔹 Dev holatda static & media fayllarni servis qilish
# ===============================
if settings.DEBUG:
    # Agar STATICFILES_DIRS ishlatilsa, document_root sifatida birinchi papkani belgilang
    static_root = settings.STATIC_ROOT if settings.STATIC_ROOT else (settings.STATICFILES_DIRS[0] if hasattr(settings, "STATICFILES_DIRS") else None)
    if static_root:
        urlpatterns += static(settings.STATIC_URL, document_root=static_root)
    # Media fayllar
    if hasattr(settings, "MEDIA_URL") and hasattr(settings, "MEDIA_ROOT"):
        urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# ===============================
# 🔹 Custom error handlers
# ===============================
handler404 = custom_404
handler500 = custom_500
