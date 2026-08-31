from django.urls import path
from . import views, api_hikvision
from .views import ip_camera_view_auto

urlpatterns = [
    # 🔥 IP Kameralar uchun sahifa va oqimlar
    path('cameras/ip/view/', ip_camera_view_auto, name='ip_camera_view'),
    path('cameras/ip/stream/<int:camera_id>/', views.ip_camera_mjpeg_stream, name='ip_camera_mjpeg_stream'),
    path('cameras/ip/audio/<int:camera_id>/', views.ip_camera_audio_stream, name='ip_camera_audio_stream'),
    path('api/camera/update/<str:ip>/', views.api_update_camera, name='api_update_camera'),

    # Xodimlar qidiruvi
    path('api/employees/search/', views.api_search_employees, name='api_search_employees'),

    # 📷 Hikvision IP Kamera Face Capture (DS-2CD2686G2-IZS) HTTP Listening API
    path('api/v1/face-capture/', api_hikvision.hikvision_face_capture_view, name='hikvision_face_capture_v1'),
    path('api/v1/face-capture', api_hikvision.hikvision_face_capture_view),
    path('api/camera/face-capture/', api_hikvision.hikvision_face_capture_view, name='hikvision_face_capture'),
    path('api/camera/face-capture', api_hikvision.hikvision_face_capture_view),
    path('api/face-capture/', api_hikvision.hikvision_face_capture_view),
    path('api/face-capture', api_hikvision.hikvision_face_capture_view),
    path('api/v1/httppost/', api_hikvision.hikvision_face_capture_view, name='hikvision_httppost'),
    path('api/v1/httppost', api_hikvision.hikvision_face_capture_view),
    path('attendance/api/camera/event/', api_hikvision.hikvision_face_capture_view, name='camera_event_endpoint'),
    path('attendance/api/camera/event', api_hikvision.hikvision_face_capture_view),
]

