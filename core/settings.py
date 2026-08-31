from pathlib import Path
import os
from dotenv import load_dotenv
from django.utils.translation import gettext_lazy as _

# ===============================
# 🔹 .env faylini yuklash
# ===============================
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ===============================
# 🔹 Asosiy sozlamalar
# ===============================
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-default-key")
DEBUG = os.getenv("DEBUG", "False") == "True"

# go2rtc sozlamalari (ZLMediaKit o'rniga)
GO2RTC_API_URL = os.getenv("GO2RTC_API_URL", "http://127.0.0.1:1984")
GO2RTC_RTSP_PORT = int(os.getenv("GO2RTC_RTSP_PORT", 8554))
ENABLE_WS = os.getenv("ENABLE_WS", "False") == "True"

# Orqaga moslik uchun (eski kod bilan)
ZLMEDIAKIT_API_URL = GO2RTC_API_URL
ZLMEDIAKIT_SECRET = ""
ZLMEDIAKIT_RTSP_PORT = GO2RTC_RTSP_PORT

# ALLOWED_HOSTS
ALLOWED_HOSTS = [host.strip() for host in os.getenv("ALLOWED_HOSTS", "").split(",") if host.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*"]

# CSRF_TRUSTED_ORIGINS
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
# ===============================
# 🔹 Ilovalar
# ===============================
INSTALLED_APPS = [
    'daphne',
    # Default Django apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Local apps
    'users',
    'attendance',
    'camera',
    'youtube',

    # 3rd-party apps
    'channels',
    'corsheaders',
    # 'rosetta',
]

# ===============================
# 🔹 Middleware
# ===============================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'core.middleware.DefaultLanguageMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# WhiteNoise compressed storage for production static files serving (gzip/brotli)
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

ROOT_URLCONF = 'core.urls'

# ===============================
# 🔹 Templates
# ===============================
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.i18n',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.site_settings',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'

# ===============================
# 🔹 Database (SQLite / PostgreSQL)
# ===============================
DBTYPE = os.getenv('DBTYPE', 'S').upper()

if DBTYPE == 'P':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', 'cameradb'),
            'USER': os.getenv('DB_USER', 'camerauser'),
            'PASSWORD': os.getenv('DB_PASSWORD', 'admin1231'),
            'HOST': os.getenv('DB_HOST', '127.0.0.1'),
            'PORT': os.getenv('DB_PORT', '5432'),
            'CONN_MAX_AGE': 0,  # Disable persistent connections to prevent connection leaks in Channels/ASGI
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# ===============================
# 🔹 Password validators
# ===============================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ===============================
# 🔹 Localization
# ===============================
LANGUAGE_CODE = 'uz'
TIME_ZONE = 'Asia/Tashkent'
USE_I18N = True
USE_L10N = True
USE_TZ = True
LANGUAGES = [
    ('uz', _('Oʻzbekcha')),
    ('ru', _('Ruscha')),
    ('en', _('Inglizcha')),
]
LOCALE_PATHS = [
    BASE_DIR / 'locale',
]

# ===============================
# 🔹 Static va Media
# ===============================
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
DATA_UPLOAD_MAX_MEMORY_SIZE = 250 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ===============================
# 🔹 Custom User
# ===============================
AUTH_USER_MODEL = 'users.CustomUser'

LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'logout'
LOGIN_URL = 'login'

# ===============================
# 🔹 Celery (Development ready)
# ===============================
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'memory://')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'cache+memory://')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = os.getenv('CELERY_TIMEZONE', 'Asia/Tashkent')
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 daqiqa limit
CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_TASK_ALWAYS_EAGER', 'True') == 'True'
CELERY_TASK_EAGER_PROPAGATES = True

# ===============================
# 🔹 Channels (WebSocket)
# ===============================
ENABLE_WS = True
if ENABLE_WS:
    redis_host = os.getenv("CHANNELS_REDIS_HOST", "127.0.0.1")
    redis_port = os.getenv("CHANNELS_REDIS_PORT", "6379")
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                "hosts": [(redis_host, int(redis_port))],
            },
        }
    }

# ===============================
# 🔹 Cache (Simple Memory Cache)
# ===============================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

# ===============================
# 🔹 CORS
# ===============================
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True

# Allow iframe embedding for previews on same domain
X_FRAME_OPTIONS = 'SAMEORIGIN'

# Reverse Proxy (Nginx SSL) sozlamalari
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# Subdomain-lararo umumiy sessiya kukisi (.namspi.uz)
if not DEBUG and not any(h.startswith(('127.0.0.1', 'localhost')) for h in ALLOWED_HOSTS if h != '*'):
    SESSION_COOKIE_DOMAIN = '.namspi.uz'
    CSRF_COOKIE_DOMAIN = '.namspi.uz'




