try:
    from .celery import app as celery_app
    __all__ = ('celery_app',)
except ImportError:
    # Celery o'rnatilmagan bo'lsa o'tkazib yuborish
    pass