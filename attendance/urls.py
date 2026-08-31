from django.urls import path
from .view.dashboard_views import DashboardView
from .view.site_settings import site_settings_view, site_settings_api_device_info
from .views import AttendanceView, PsychologicalProfileView, media_thumbnail, service_logs_view, service_action_view, service_unit_load, service_unit_save, service_status_view, chatbot_view, chatbot_api
from .view.pages import AboutPageView, ContactPageView, FeedbackPageView, FeedbackListView, FeedbackDeleteView

from .view.isup_views import (
    isup_dashboard_view, isup_api_status, isup_api_connect_camera, isup_api_add_camera,
    isup_api_probe_camera, isup_api_discover_network_cameras, isup_camera_live_preview_stream,
    isup_api_camera_channels, isup_api_delete_camera,
    isup_api_disconnect_device, isup_live_stream_view, isup_snapshot_view,
    isup_stream_start_view, isup_stream_stop_view, isup_stream_status_view,
    isup_api_reboot_camera, isup_api_detect_face, isup_camera_detail_view,
    isup_api_get_isapi_config, isup_api_save_isapi_config, isup_api_sync_time,
    isup_api_send_raw_isapi, isup_api_get_pairs, isup_api_save_pair, isup_api_delete_pair
)

urlpatterns = [
    path('media-thumb/<str:preset>/<path:path>', media_thumbnail, name='media_thumbnail'),
    path('', DashboardView.as_view(), name='dashboard'),
    path('settings/site/', site_settings_view, name='site_settings'),
    path('settings/site/api/device-info/', site_settings_api_device_info, name='site_settings_api_device_info'),

    path('settings/isup/', isup_dashboard_view, name='isup_dashboard'),
    path('settings/isup/camera/<int:camera_id>/', isup_camera_detail_view, name='isup_camera_detail'),
    path('settings/isup/api/status/', isup_api_status, name='isup_api_status'),
    path('settings/isup/api/connect/', isup_api_connect_camera, name='isup_api_connect_camera'),
    path('settings/isup/api/camera/add/', isup_api_add_camera, name='isup_api_add_camera'),
    path('settings/isup/api/camera/probe/', isup_api_probe_camera, name='isup_api_probe_camera'),
    path('settings/isup/api/camera/discover/', isup_api_discover_network_cameras, name='isup_api_discover_network_cameras'),
    path('settings/isup/api/camera/stream-preview/', isup_camera_live_preview_stream, name='isup_camera_live_preview_stream'),
    path('settings/isup/api/camera/channels/', isup_api_camera_channels, name='isup_api_camera_channels'),
    path('settings/isup/api/camera/delete/<int:camera_id>/', isup_api_delete_camera, name='isup_api_delete_camera'),
    path('settings/isup/api/pairs/', isup_api_get_pairs, name='isup_api_get_pairs'),
    path('settings/isup/api/pairs/save/', isup_api_save_pair, name='isup_api_save_pair'),
    path('settings/isup/api/pairs/delete/<int:pair_id>/', isup_api_delete_pair, name='isup_api_delete_pair'),
    path('settings/isup/api/disconnect/', isup_api_disconnect_device, name='isup_api_disconnect_device'),
    path('settings/isup/api/reboot/', isup_api_reboot_camera, name='isup_api_reboot_camera'),
    path('settings/isup/api/detect-face/', isup_api_detect_face, name='isup_api_detect_face'),
    path('settings/isup/api/isapi/config/<int:camera_id>/', isup_api_get_isapi_config, name='isup_api_get_isapi_config'),
    path('settings/isup/api/isapi/save/', isup_api_save_isapi_config, name='isup_api_save_isapi_config'),
    path('settings/isup/api/isapi/sync-time/', isup_api_sync_time, name='isup_api_sync_time'),
    path('settings/isup/api/isapi/raw/', isup_api_send_raw_isapi, name='isup_api_send_raw_isapi'),
    path('settings/isup/stream/<str:device_id>/', isup_live_stream_view, name='isup_live_stream'),
    path('settings/isup/snapshot/<str:device_id>/', isup_snapshot_view, name='isup_snapshot'),
    path('settings/isup/stream/start/<str:device_id>/', isup_stream_start_view, name='isup_stream_start'),
    path('settings/isup/stream/stop/<str:device_id>/', isup_stream_stop_view, name='isup_stream_stop'),
    path('settings/isup/stream/status/<str:device_id>/', isup_stream_status_view, name='isup_stream_status'),
    path('attendance/', AttendanceView.as_view(), name='attendance'),
    path('chatbot/', chatbot_view, name='chatbot_view'),
    path('chatbot/api/', chatbot_api, name='chatbot_api'),

    # 🔥 Psixologik portretlar
    path('attendance/psychology/', PsychologicalProfileView.as_view(), name='attendance_psychology'),

    # 🔥 About sahifa
    path('about/', AboutPageView.as_view(), name='about'),
    path('contact/', ContactPageView.as_view(), name='contact'),
    path('feedback/', FeedbackPageView.as_view(), name='feedback'),
    path('feedback/list/', FeedbackListView.as_view(), name='feedback_list'),
    path('feedback/delete/<int:pk>/', FeedbackDeleteView.as_view(), name='feedback_delete'),
    path("services/logs/", service_logs_view, name="service_logs"),
    path("services/status/", service_status_view, name="service_status"),
    path("services/action/", service_action_view, name="service_action"),
    path("services/unit/load/", service_unit_load, name="service_unit_load"),
    path("services/unit/save/", service_unit_save, name="service_unit_save"),
]
