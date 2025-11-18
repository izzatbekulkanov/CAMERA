from django.urls import path
from .view.dashboard_views import DashboardView
from .view.site_settings import site_settings_view
from .views import AttendanceView, PsychologicalProfileView
from .view.pages import AboutPageView, ContactPageView, FeedbackPageView  # <-- yangi import

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
]
