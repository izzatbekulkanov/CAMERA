from django.urls import path
from .view.dashboard_views import DashboardView
from .view.site_settings import site_settings_view
from .views import AttendanceView, PsychologicalProfileView, service_logs_view, service_action_view, service_unit_load, service_unit_save, service_status_view
from .view.pages import AboutPageView, ContactPageView, FeedbackPageView

urlpatterns = [
    path('', DashboardView.as_view(), name='dashboard'),
    path('settings/site/', site_settings_view, name='site_settings'),
    path('attendance/', AttendanceView.as_view(), name='attendance'),

    # 🔥 Psixologik portretlar
    path('attendance/psychology/', PsychologicalProfileView.as_view(), name='attendance_psychology'),

    # 🔥 About sahifa
    path('about/', AboutPageView.as_view(), name='about'),
    path('contact/', ContactPageView.as_view(), name='contact'),
    path('feedback/', FeedbackPageView.as_view(), name='feedback'),
    path("services/logs/", service_logs_view, name="service_logs"),
    path("services/status/", service_status_view, name="service_status"),
    path("services/action/", service_action_view, name="service_action"),
    path("services/unit/load/", service_unit_load, name="service_unit_load"),
    path("services/unit/save/", service_unit_save, name="service_unit_save"),
]
