from django.urls import path
from . import views

urlpatterns = [
    path("", views.youtube_dashboard, name="youtube_dashboard"),
    path("start/", views.youtube_start, name="youtube_start"),
    path("stop/<int:stream_id>/", views.youtube_stop, name="youtube_stop"),

    path("stream/delete/<int:stream_id>/", views.youtube_stream_delete, name="youtube_stream_delete"),
    path("stream/info/<int:stream_id>/", views.youtube_stream_info, name="youtube_stream_info"),

    path("profile/create/", views.youtube_profile_create, name="youtube_profile_create"),
    path("profile/delete/<int:profile_id>/", views.youtube_profile_delete, name="youtube_profile_delete"),

]
