# core/urls.py
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render
from django.urls import re_path
from django.views.static import serve


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
    path("youtube/", include("youtube.urls")),

    # i18n URL (tilni o‘zgartirish uchun)
    path('i18n/', include('django.conf.urls.i18n')),

    # Rosetta (translation editor)
    path('rosetta/', include('rosetta.urls')),
]

# ===============================
# 🔹 Dev holatda static & media fayllarni servis qilish
# ===============================
if settings.DEBUG:
    # STATIC: devda Django static view orqali
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    # MEDIA: devda Django static view orqali
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # ⚠️ Production:
    # Static fayllarni WhiteNoise beradi (MIDDLEWARE orqali),
    # shu sababli bu yerda STATIC uchun hech narsa yozmaymiz.

    # Media fayllar uchun oddiy serve (nginx bo'lmaguncha vaqtinchalik yechim)
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {
            'document_root': settings.MEDIA_ROOT,
        }),
    ]
# ===============================
# 🔹 Custom error handlers
# ===============================
handler404 = custom_404
handler500 = custom_500
