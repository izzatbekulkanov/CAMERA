from django.urls import path
from . import views
from .views import view_cameras

urlpatterns = [
    path('cameras/usb-camera/', views.usb_camera_view, name='usb_camera_view'),
    path('cameras/add/', views.add_camera_view, name='add_camera_view'),

    # Kameralar ro'yxati
    path('cameras/list/', views.camera_list_view, name='camera_list_view'),

    # API
    path('api/cameras/add/', views.api_add_camera, name='api_add_camera'),
    path('api/cameras/active/', views.api_active_cameras),
    path('api/cameras/remove/<str:ip>/', views.api_remove_camera),
    path('api/cameras/update/<str:ip>/', views.api_update_camera, name='api_update_camera'),

    # Jonli ko‘rish sahifasi
    path('cameras/view/', view_cameras, name='view_cameras'),
]