from django.urls import path
from .view.dashboard_views import DashboardView
from .view.site_settings import site_settings_view

urlpatterns = [
    path('', DashboardView.as_view(), name='dashboard'),
    path('settings/site/', site_settings_view, name='site_settings'),
]
