import logging
from django.contrib.auth import get_user_model
from django.db import transaction
from django.core.exceptions import PermissionDenied
from django.contrib.admin.utils import NestedObjects
from django.conf import settings

CustomUser = get_user_model()
logger = logging.getLogger(__name__)

@transaction.atomic
def permanently_delete_user(user_id: int, requested_by=None):
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        msg = f"[O‘CHIRISH XATOSI] ID {user_id} topilmadi"
        if settings.DEBUG:
            print("XATO:", msg)
        logger.warning(msg)
        return False, "Foydalanuvchi topilmadi"

    if requested_by and not requested_by.is_superuser:
        msg = f"[RUXSAT YO‘Q] {requested_by} → {user}"
        if settings.DEBUG:
            print("RUXSAT YO‘Q:", msg)
        logger.warning(msg)
        raise PermissionDenied("Faqat superuser o‘chirishi mumkin")

    collector = NestedObjects(using='default')
    collector.collect([user])
    to_delete = collector.nested()
    related_count = len(to_delete) - 1

    deleted_info = {
        'user_id': user.id,
        'full_name': user.full_name or user.username,
        'username': user.username,
        'role': user.role,
        'deleted_by': str(requested_by) if requested_by else 'system',
        'related_count': related_count,
    }

    if settings.DEBUG:
        print("\n" + "="*60)
        print("BUTUNLAY O‘CHIRILMOQDA".center(60))
        print(f"ID: {user.id} | {deleted_info['full_name']} | {user.role}")
        print(f"O‘chiruvchi: {deleted_info['deleted_by']}")
        print(f"Bog‘liq obyektlar: {related_count} ta")
        print("="*60 + "\n")

    logger.critical(
        "BUTUNLAY O‘CHIRILDI → %(full_name)s | ID: %(user_id)s | "
        "O‘chirgan: %(deleted_by)s | Bog‘liq: %(related_count)s ta",
        deleted_info
    )

    deleted_count, _ = user.delete()

    success_msg = f"Foydalanuvchi va {deleted_count} ta obyekt o‘chirildi"
    if settings.DEBUG:
        print("MUVOFFAQIYATLI:", success_msg)

    return True, success_msg