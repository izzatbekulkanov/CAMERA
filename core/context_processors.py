# core/context_processors.py
from attendance.models import SiteSettings


def site_settings(request):
    """
    Sayt sozlamalarini har bir template uchun global contextga qo'shadi.
    """
    settings_obj = SiteSettings.get_settings()  # singleton metodingdan foydalanamiz
    return {
        'global_site_settings': settings_obj
    }
