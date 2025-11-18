#camera/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('usb-camera/', views.usb_camera_view, name='usb_camera_view'),
]
