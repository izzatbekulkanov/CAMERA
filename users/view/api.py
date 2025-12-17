# users/services.py

import logging
from typing import Any, Dict, Optional, Tuple

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.contrib.admin.utils import NestedObjects
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from attendance.models import SiteSettings

CustomUser = get_user_model()
logger = logging.getLogger(__name__)

@login_required
@csrf_exempt
def clear_employees(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        deleted_count, _ = (
            CustomUser.objects
            .filter(role=CustomUser.Role.EMPLOYEE)
            .exclude(is_superuser=True)   # 🔒 SUPERUSER SAQLANADI
            .delete()
        )
        return JsonResponse({
            "success": True,
            "message": f"{deleted_count} ta xodim o‘chirildi (superuserlar saqlandi)"
        })
    except Exception as e:
        logger.exception(e)
        return JsonResponse(
            {"success": False, "message": f"O‘chirishda xatolik: {str(e)}"},
            status=500
        )


@login_required
@csrf_exempt
def clear_students(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        deleted_count, _ = (
            CustomUser.objects
            .filter(role=CustomUser.Role.STUDENT)
            .filter(is_superuser=False)   # 🔒 QAT’IY
            .delete()
        )
        return JsonResponse({
            "success": True,
            "message": f"✅ {deleted_count} ta talaba o‘chirildi (superuserlar saqlandi)"
        })
    except Exception as e:
        logger.exception(e)
        return JsonResponse(
            {"success": False, "message": f"O‘chirishda xatolik: {str(e)}"},
            status=500
        )


def _count_collector_objects(collector: NestedObjects) -> int:
    """
    NestedObjects.collect(...) dan keyin:
    collector.data -> {model_class: {instances...}} ko'rinishida bo'ladi.
    Shundan jami obyektlar sonini sanaymiz.
    """
    try:
        total = 0
        for model, objs in collector.data.items():
            total += len(objs)
        return int(total)
    except Exception:
        return 0


@transaction.atomic
def permanently_delete_user(user_id: int, requested_by=None) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        return False, "Foydalanuvchi topilmadi", {"user_id": user_id}

    # 🔒 SUPERUSERNI O‘CHIRISH TAQIQLANADI
    if user.is_superuser:
        logger.critical(
            "[PERM DELETE BLOCKED] Superuser delete attempt: target=%s by=%s",
            user_id, requested_by
        )
        raise PermissionDenied("Superuserni o‘chirish taqiqlangan")

    # 🔒 O‘CHIRUVCHI FAQAT SUPERUSER BO‘LISHI SHART
    if not requested_by or not requested_by.is_superuser:
        raise PermissionDenied("Faqat superuser o‘chirishi mumkin")

    collector = NestedObjects(using="default")
    collector.collect([user])

    total_objects = _count_collector_objects(collector)
    related_objects = max(0, total_objects - 1)

    meta = {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "deleted_by": str(requested_by),
        "related_count": related_objects,
        "total_count": total_objects,
    }

    logger.critical(
        "[PERM DELETE] user=%(username)s id=%(user_id)s "
        "by=%(deleted_by)s related=%(related_count)s",
        meta,
    )

    deleted_total, per_model = user.delete()

    meta["deleted_total"] = int(deleted_total)
    meta["deleted_by_model"] = {str(k): v for k, v in (per_model or {}).items()}

    return True, "Foydalanuvchi butunlay o‘chirildi", meta


@login_required
def get_groups(request):
    try:
        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            return JsonResponse({"error": "HEMIS sozlamalari topilmadi."}, status=400)

        base_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/group-list"
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {settings.hemis_api_token}"
        }

        search = (request.GET.get("search") or "").strip().lower()

        cache_key = "hemis_group_list_all_v1"
        groups_all = cache.get(cache_key)

        if groups_all is None:
            params = {"page": 1, "limit": 200}
            r = requests.get(base_url, headers=headers, params=params, timeout=20)
            r.raise_for_status()

            data = r.json() or {}
            if not data.get("success"):
                return JsonResponse({"error": "HEMIS success=false qaytardi"}, status=500)

            block = data.get("data") or {}
            items = block.get("items") or []
            pagination = block.get("pagination") or {}
            page_count = int(pagination.get("pageCount") or 1)

            all_items = list(items)
            for page in range(2, page_count + 1):
                params["page"] = page
                rr = requests.get(base_url, headers=headers, params=params, timeout=20)
                if rr.status_code != 200:
                    continue
                dd = rr.json() or {}
                all_items.extend(((dd.get("data") or {}).get("items") or []))

            groups_all = [
                {"id": g.get("id"), "name": (g.get("name") or "").strip()}
                for g in all_items
                if g.get("id")
            ]

            cache.set(cache_key, groups_all, timeout=600)

        # search bo‘lsa filter
        if search:
            groups = [g for g in groups_all if search in (g["name"] or "").lower()]
        else:
            groups = groups_all

        return JsonResponse({
            "success": True,
            "count": len(groups),
            "groups": groups,  # ✅ endi kesmaydi
        })

    except requests.exceptions.RequestException as e:
        return JsonResponse({"error": f"HEMIS bilan bog‘lanishda xato: {str(e)}"}, status=500)
    except Exception as e:
        return JsonResponse({"error": f"Kutilmagan xato: {type(e).__name__} - {e}"}, status=500)


@login_required
def get_specialties(request):
    """
    HEMIS dan barcha DEPARTMENT (fakultet, kafedra, markaz, bo‘lim, va boshqalar)
    ma’lumotlarini olish (employee-list asosida).
    """
    try:
        # 1️⃣ HEMIS sozlamalari
        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            return JsonResponse({"error": "HEMIS sozlamalari topilmadi."}, status=400)

        hemis_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/employee-list"
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {settings.hemis_api_token}"
        }

        all_departments = {}
        page = 1
        limit = 200
        max_pages = 50

        # 🔹 Barcha sahifalarni aylanish
        while True:
            response = requests.get(
                f"{hemis_url}?type=all&page={page}&limit={limit}",
                headers=headers,
                timeout=25
            )

            if response.status_code != 200:
                return JsonResponse({
                    "error": f"HEMIS bilan aloqa yo‘q ({response.status_code})."
                }, status=500)

            data = response.json() or {}
            data_block = data.get("data", {})
            if isinstance(data_block, list) and data_block:
                data_block = data_block[0]

            items = data_block.get("items", [])
            pagination = data_block.get("pagination", {})

            if not isinstance(items, list):
                items = []

            # 🔹 Har bir xodimdan department ma’lumotlarini yig‘ish
            for emp in items:
                dep = emp.get("department")
                if not dep or not isinstance(dep, dict):
                    continue

                dep_id = dep.get("id")
                if not dep_id:
                    continue

                structure_type = dep.get("structureType") or {}
                locality_type = dep.get("localityType") or {}
                parent = dep.get("parent") or {}

                # 🔹 Dublikatlardan saqlanish (id orqali)
                if dep_id not in all_departments:
                    all_departments[dep_id] = {
                        "id": dep_id,
                        "name": dep.get("name"),
                        "code": dep.get("code"),
                        "structure_type_code": structure_type.get("code"),
                        "structure_type_name": structure_type.get("name"),
                        "locality_type_code": locality_type.get("code"),
                        "locality_type_name": locality_type.get("name"),
                        "parent_id": parent.get("id") if isinstance(parent, dict) else None,
                        "parent_name": parent.get("name") if isinstance(parent, dict) else None,
                        "active": dep.get("active", False)
                    }

            # 🔹 Sahifalar tugaganini tekshirish
            current_page = pagination.get("page", page)
            total_pages = pagination.get("pageCount", 1)

            if current_page >= total_pages or page >= max_pages:
                break

            page += 1

        departments_list = list(all_departments.values())

        return JsonResponse({
            "success": True,
            "count": len(departments_list),
            "departments": departments_list
        })

    except requests.exceptions.RequestException as e:
        return JsonResponse({"error": f"HEMIS bilan bog‘lanishda xato: {str(e)}"}, status=500)
    except Exception as e:
        return JsonResponse({"error": f"Kutilmagan xato: {type(e).__name__} - {e}"}, status=500)