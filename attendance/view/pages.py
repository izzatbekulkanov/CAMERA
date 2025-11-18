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
            {'name': 'Feedback', 'url': None},
        ]
        return render(request, self.template_name, {'breadcrumbs': breadcrumbs})