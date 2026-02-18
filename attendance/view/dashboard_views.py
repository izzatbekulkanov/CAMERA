from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib import messages

from attendance.models import Attendance
from camera.models import Camera

User = get_user_model()


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pages/home.html"

    def get(self, request, *args, **kwargs):
        # 1-marta login bo'lganda toast chiqadi
        if not request.session.get("login_message_shown", False):
            messages.success(
                request,
                "Tizimga muvaffaqiyatli kirdingiz.",
                extra_tags="login"
            )
            request.session["login_message_shown"] = True

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        now = timezone.localtime()

        # Users stats
        total_users = User.objects.count()
        total_students = User.objects.filter(role="student", is_superuser=False).count()
        total_employees = User.objects.filter(role="employee", is_superuser=False).count()
        total_admins = User.objects.filter(is_superuser=True).count()

        # Attendance stats (bugun)
        todays_attendance = Attendance.objects.filter(date=today)
        present_now = todays_attendance.filter(is_present=True).count()
        exit_today = todays_attendance.filter(is_present=False).count()

        last_attendance = (
            todays_attendance
            .select_related("user")
            .order_by("-last_seen")[:8]
        )

        # Cameras stats
        total_cameras = Camera.objects.count()
        active_cameras = Camera.objects.filter(is_active=True).count()

        context.update({
            "page_title": "Dashboard",
            "today": today,
            "now": now,

            # users
            "total_users": total_users,
            "total_students": total_students,
            "total_employees": total_employees,
            "total_admins": total_admins,

            # attendance
            "present_now": present_now,
            "exit_today": exit_today,
            "last_attendance": last_attendance,

            # cameras
            "total_cameras": total_cameras,
            "active_cameras": active_cameras,
        })
        return context
        return context