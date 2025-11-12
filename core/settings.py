from pathlib import Path
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# SECURITY SETTINGS
SECRET_KEY = os.getenv("SECRET_KEY", "django-insecure-default-key")
DEBUG = os.getenv("DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "").split(",") if os.getenv("ALLOWED_HOSTS") else ["*"]

# APPLICATIONS
INSTALLED_APPS = [
    # Default Django apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Local apps
    'users',  # foydalanuvchilar uchun app (keyingi qadamda yaratamiz)
    'attendance',  # kirish/chiqish va kamera uchun app (keyingi qadamda)

    # 3rd-party apps (agar kerak bo‘lsa, keyinchalik qo‘shamiz)
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

# TEMPLATE SETTINGS
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],  # ✅ templates papkani ishlatadi
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

# DATABASE SETTINGS (SQLite — boshlang‘ich uchun)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# PASSWORD VALIDATION
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# LANGUAGE & TIME SETTINGS
LANGUAGE_CODE = 'uz'
TIME_ZONE = 'Asia/Tashkent'  # ✅ O‘zbekiston vaqti
USE_I18N = True
USE_TZ = True

# STATIC & MEDIA SETTINGS
STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / 'static',  # developmentda
]
STATIC_ROOT = BASE_DIR / 'staticfiles'  # production uchun (collectstatic bilan)


MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'  # ✅ rasm/video saqlash joyi

# DEFAULTS
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# AUTH MODEL
AUTH_USER_MODEL = 'users.CustomUser'

# Login bo‘lgandan keyin yo‘naltiriladigan sahifa
LOGIN_REDIRECT_URL = 'dashboard'

# Logout bo‘lgandan keyin yo‘naltiriladigan sahifa
LOGOUT_REDIRECT_URL = 'logout'

# Agar login kerak bo‘lgan sahifaga (masalan, @login_required bilan himoyalangan)
# login qilinmagan foydalanuvchi kirsa — shu sahifaga yo‘naltiriladi
LOGIN_URL = 'login'


# ===============================
# 🔹 CELERY konfiguratsiyasi
# ===============================
CELERY_BROKER_URL = 'redis://127.0.0.1:6379/0'
CELERY_RESULT_BACKEND = 'redis://127.0.0.1:6379/1'  # <-- Backendni alohida DB qilib oling
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Tashkent'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 daqiqa limit

# ===============================
# 🔹 CHANNELS (Daphne/WebSocket)
# ===============================
ASGI_APPLICATION = 'core.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [('127.0.0.1', 6379)],  # Redis xost
        },
    },
}

# ===============================
# 🔹 REDIS CACHE (ixtiyoriy, lekin tavsiya etiladi)
# ===============================
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': 'redis://127.0.0.1:6379/2',  # yana bitta DB
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        }
    }
}