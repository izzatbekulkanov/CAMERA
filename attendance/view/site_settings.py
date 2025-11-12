from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages

from attendance.models import SiteSettings


@login_required(login_url='login')
def site_settings_view(request):
    """
    Site Settings sahifasi uchun view.
    GET -> sahifani ko'rsatadi
    POST -> form ma'lumotlarini saqlaydi va Swal bilan xabar beradi
    """
    # Singleton bo'lgani uchun bitta obyekt oling yoki yaratib oling
    site_settings, created = SiteSettings.objects.get_or_create(id=1)

    if request.method == 'POST':
        # Text va URL maydonlar
        site_settings.site_name = request.POST.get('site_name', site_settings.site_name)
        site_settings.site_status = request.POST.get('site_status', site_settings.site_status)
        site_settings.contact_email = request.POST.get('contact_email', site_settings.contact_email)
        site_settings.contact_phone = request.POST.get('contact_phone', site_settings.contact_phone)
        site_settings.hemis_url = request.POST.get('hemis_url', site_settings.hemis_url)
        site_settings.hemis_api_token = request.POST.get('hemis_api_token', site_settings.hemis_api_token)

        # Fayl maydonlar (logo_dark, logo_light)
        if 'logo_dark' in request.FILES and request.FILES['logo_dark']:
            site_settings.logo_dark = request.FILES['logo_dark']
        if 'logo_light' in request.FILES and request.FILES['logo_light']:
            site_settings.logo_light = request.FILES['logo_light']

        # Saqlash
        site_settings.save()

        # SweetAlert bilan xabar
        messages.success(request, "Sayt sozlamalari muvaffaqiyatli saqlandi.")

        # Redirect same page
        return redirect('site_settings')

    # Breadcrumbs
    breadcrumbs = [
        {'name': 'Asosiy sahifa', 'url': '/'},
        {'name': 'Sayt sozlamalari', 'url': None},
    ]

    context = {
        'breadcrumbs': breadcrumbs,
        'site_settings': site_settings
    }

    return render(request, 'pages/settings.html', context)