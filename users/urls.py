from django.urls import path
from .view.api import permanently_delete_user, get_groups, get_specialties, clear_employees, clear_students
from .view.hemis_sync import ( sync_employees_from_hemis, get_sync_progress,
    sync_students_from_hemis,

)
from .view.academic import academic_groups_view, faculties_view, curriculums_view, sync_existing_academic_data, academic_group_students_view
from .views import login_view, logout_view, reset_password_view, profile_view, users_list_view, face_encoding_list_view, \
    import_users_view, dc_oauth_login, dc_oauth_callback, students_list_view, employees_list_view, \
    unknown_clusters_list_view, associate_cluster_view, run_clustering_trigger_view, api_search_users

urlpatterns = [
    # Auth views
    path('login/', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('reset-password/', reset_password_view, name='reset_password'),
    path('oauth/dc/', dc_oauth_login, name='dc_oauth_login'),
    path('login/dc/redirect', dc_oauth_callback, name='dc_oauth_callback'),
    path('login/dc/redirect/', dc_oauth_callback),

    # Profil va foydalanuvchilar
    path('profile/', profile_view, name='profile'),
    path('users/', users_list_view, name='users_list'),
    path('users/students/', students_list_view, name='students_list'),
    path('users/employees/', employees_list_view, name='employees_list'),
    path('users/import/', import_users_view, name='import_users'),

    # HEMIS sync va tozalash
    path('users/get-groups/', get_groups, name='get_groups'),
    path('users/get-specialties/', get_specialties, name='get_specialties'),
    path('users/sync-employees/', sync_employees_from_hemis, name='sync_employees_from_hemis'),
    path('users/get-sync-progress/', get_sync_progress, name='sync_progress'),
    path('users/clear-employees/', clear_employees, name='clear_employees'),
    path('users/clear-students/', clear_students, name='clear_students'),
    path('users/sync-students/', sync_students_from_hemis, name='sync_students_from_hemis'),

    # Akademik bo'lim
    path('users/academic/groups/', academic_groups_view, name='academic_groups'),
    path('users/academic/groups/<int:group_id>/students/', academic_group_students_view, name='academic_group_students'),
    path('users/academic/faculties/', faculties_view, name='faculties_list'),
    path('users/academic/curriculums/', curriculums_view, name='curriculums_list'),
    path('users/academic/sync/', sync_existing_academic_data, name='sync_existing_academic'),

    # Face Encodings ro‘yxati
    path('users/face-encodings/', face_encoding_list_view, name='face_encodings_list'),

    # Noma'lum yuzlar klasterlari
    path('users/unknown-clusters/', unknown_clusters_list_view, name='unknown_clusters_list'),
    path('users/unknown-clusters/<int:cluster_id>/associate/', associate_cluster_view, name='associate_cluster'),
    path('users/unknown-clusters/run-clustering/', run_clustering_trigger_view, name='run_clustering_trigger'),
    path('api/users/search/', api_search_users, name='api_search_users'),

    # XAVFSIZ: Faqat superuser uchun BUTUNLAY O‘CHIRISH
    path('users/delete-permanently/<int:user_id>/', permanently_delete_user, name='permanently_delete_user'),
]