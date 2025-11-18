from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone
from django.contrib.auth import get_user_model


User = get_user_model()


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "pages/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()

        # --- Statistikalar ---
        total_users = User.objects.count()
        total_students = User.objects.filter(role="student").count()
        total_employees = User.objects.filter(role="employee").count()


        # Dashboard uchun kontekst
        context.update({
            "page_title": "Dashboard",
            "total_users": total_users,
            "total_students": total_students,
            "total_employees": total_employees,
            "today": today,
        })
        return context
