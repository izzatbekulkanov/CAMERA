from django.urls import path
from . import views
from .views import view_cameras, ip_camera_view_auto

urlpatterns = [
    # USB Kamera

    # Kamera qo‘shish
    path('cameras/add/', views.add_camera_view, name='add_camera_view'),

    # Kameralar ro‘yxati
    path('cameras/list/', views.camera_list_view, name='camera_list_view'),

    # API
    path('api/cameras/add/', views.api_add_camera, name='api_add_camera'),
    path('api/cameras/active/', views.api_active_cameras),
    path('api/cameras/remove/<str:ip>/', views.api_remove_camera),
    path('api/cameras/update/<str:ip>/', views.api_update_cameras, name='api_update_camera'),
    path('api/camera/update/<str:ip>/', views.api_update_camera, name='api_update_camera'),

    # Jonli ko‘rish (USB + IP aralash)
    path('cameras/view/', view_cameras, name='view_cameras'),

    # 🔥 Faqat IP Kameralar uchun sahifa (yangi qo‘shildi)
    path('cameras/ip/view/', ip_camera_view_auto, name='ip_camera_view'),
    path('cameras/ip/stream/<int:camera_id>/', views.ip_camera_mjpeg_stream, name='ip_camera_mjpeg_stream'),
]
