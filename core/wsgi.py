import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

application = get_wsgi_application()

# Development uchun whitenoise o'chirilgan
# try:
#     from whitenoise import WhiteNoise
#     application = WhiteNoise(application, root=os.path.join(os.path.dirname(__file__), 'static'))
# except ImportError:
#     pass
