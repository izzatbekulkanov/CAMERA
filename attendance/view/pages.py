# attendance/view/pages_views.py
from django.shortcuts import render
from django.views import View

class AboutPageView(View):
    template_name = "pages/about.html"

    def get(self, request, *args, **kwargs):
        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'About', 'url': None},
        ]
        return render(request, self.template_name, {'breadcrumbs': breadcrumbs})


class ContactPageView(View):
    template_name = "pages/contact.html"

    def get(self, request, *args, **kwargs):
        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Kontakt', 'url': None},
        ]
        return render(request, self.template_name, {'breadcrumbs': breadcrumbs})


class FeedbackPageView(View):
    template_name = "pages/feedback.html"

    def get(self, request, *args, **kwargs):
        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Fikr-takliflar', 'url': None},
        ]
        return render(request, self.template_name, {'breadcrumbs': breadcrumbs})

    def post(self, request, *args, **kwargs):
        from django.http import JsonResponse
        from attendance.models import Feedback
        
        full_name = request.POST.get('full_name', '').strip()
        position = request.POST.get('position', '').strip()
        feedback_type = request.POST.get('type', 'taklif').strip()
        rating_raw = request.POST.get('rating')
        message = request.POST.get('message', '').strip()
        
        if not position or not message or not rating_raw:
            return JsonResponse({"success": False, "message": "Iltimos, barcha majburiy maydonlarni to'ldiring va baho bering."}, status=400)
            
        try:
            rating = int(rating_raw)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "message": "Baholash formati xato."}, status=400)
            
        feedback = Feedback.objects.create(
            full_name=full_name or None,
            position=position,
            feedback_type=feedback_type,
            rating=rating,
            message=message
        )
        return JsonResponse({"success": True, "message": "Fikringiz va taklifingiz muvaffaqiyatli qabul qilindi. Rahmat!"})


class FeedbackListView(View):
    template_name = "pages/feedback_list.html"

    def get(self, request, *args, **kwargs):
        from django.contrib.auth.decorators import login_required
        from django.utils.decorators import method_decorator
        from attendance.models import Feedback

        # Faqat login qilgan xodimlar ko'rsin
        if not request.user.is_authenticated:
            from django.shortcuts import redirect
            return redirect('login')

        feedbacks = Feedback.objects.all().order_by('-created_at')
        
        breadcrumbs = [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Xabarlar (Fikr-takliflar)', 'url': None},
        ]
        context = {
            'breadcrumbs': breadcrumbs,
            'feedbacks': feedbacks,
        }
        return render(request, self.template_name, context)


class FeedbackDeleteView(View):
    def post(self, request, pk, *args, **kwargs):
        from django.http import JsonResponse
        from attendance.models import Feedback

        if not request.user.is_authenticated:
            return JsonResponse({"success": False, "message": "Ruxsat berilmagan."}, status=403)

        try:
            feedback = Feedback.objects.get(pk=pk)
            feedback.delete()
            return JsonResponse({"success": True, "message": "Xabar muvaffaqiyatli o'chirildi!"})
        except Feedback.DoesNotExist:
            return JsonResponse({"success": False, "message": "Xabar topilmadi."}, status=404)