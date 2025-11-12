from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import render


urlpatterns = [
    # --- Admin panel ---
    path('admin/', admin.site.urls),

    # --- App URL’lari ---
    path('', include('attendance.urls')),
    path('', include('users.urls')),
]

# --- Static va media fayllar (developmentda ishlaydi) ---
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# --- Custom 404 view ---
def custom_404(request, exception):
    return render(request, "pages/404.html", status=404)

# --- Custom 500 view ---
def custom_500(request):
    return render(request, "pages/500.html", status=500)


# Django’ga bildirish
handler404 = custom_404
handler500 = custom_500
