from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib import messages


User = get_user_model()


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pages/home.html"

    def get(self, request, *args, **kwargs):
        """
        Foydalanuvchi tizimga kirgandan keyingi birinchi kirishda
        'Tizimga muvaffaqiyatli kirdingiz' degan xabar chiqadi.
        Keyingi refreshlarda chiqmaydi.
        """
        if not request.session.get("login_message_shown", False):
            messages.success(
                request,
                "Tizimga muvaffaqiyatli kirdingiz.",
                extra_tags="login"  # login xabarini ajratib olish uchun
            )
            request.session["login_message_shown"] = True

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        total_users = User.objects.count()
        total_students = User.objects.filter(role="student").count()
        total_employees = User.objects.filter(role="employee").count()

        context.update({
            "page_title": "Dashboard",
            "total_users": total_users,
            "total_students": total_students,
            "total_employees": total_employees,
            "today": today,
        })
        return context