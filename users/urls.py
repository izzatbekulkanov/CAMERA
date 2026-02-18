from django.urls import path
from .view.api import permanently_delete_user, get_groups, get_specialties, clear_employees, clear_students
from .view.hemis_sync import ( sync_employees_from_hemis, get_sync_progress,
    sync_students_from_hemis,

)
from .views import login_view, logout_view, reset_password_view, profile_view, users_list_view, face_encoding_list_view, \
    import_users_view

urlpatterns = [
    # Auth views
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('reset-password/', reset_password_view, name='reset_password'),

    # Profil va foydalanuvchilar
    path('profile/', profile_view, name='profile'),
    path('users/', users_list_view, name='users_list'),
    path('users/import/', import_users_view, name='import_users'),

    # HEMIS sync va tozalash
    path('users/get-groups/', get_groups, name='get_groups'),
    path('users/get-specialties/', get_specialties, name='get_specialties'),
    path('users/sync-employees/', sync_employees_from_hemis, name='sync_employees_from_hemis'),
    path('users/get-sync-progress/', get_sync_progress, name='sync_progress'),
    path('users/clear-employees/', clear_employees, name='clear_employees'),
    path('users/clear-students/', clear_students, name='clear_students'),
    path('users/sync-students/', sync_students_from_hemis, name='sync_students_from_hemis'),

    # Face Encodings ro‘yxati
    path('users/face-encodings/', face_encoding_list_view, name='face_encodings_list'),

    # XAVFSIZ: Faqat superuser uchun BUTUNLAY O‘CHIRISH
    path('users/delete-permanently/<int:user_id>/', permanently_delete_user, name='permanently_delete_user'),
]