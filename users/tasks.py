# users/tasks.py
from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.core.files.base import ContentFile

from attendance.models import SiteSettings
from .models import CustomUser
import requests
from datetime import datetime

@shared_task(bind=True)
def sync_employees_task(self, user_id, department_id=None):
    progress_key = f"sync_progress_{user_id}"
    cache.set(progress_key, {"status": "starting", "percent": 0, "message": "Tayyorlanmoqda..."}, timeout=600)

    try:
        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            cache.set(progress_key, {"status": "error", "message": "HEMIS sozlamalari yo'q"})
            return

        base_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/employee-list"
        headers = {"accept": "application/json", "Authorization": f"Bearer {settings.hemis_api_token}"}
        params = {"type": "all", "page": 1, "limit": 200}
        if department_id:
            params["_department"] = department_id

        # Birinchi sahifa
        response = requests.get(base_url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            cache.set(progress_key, {"status": "error", "message": f"API xato: {response.status_code}"})
            return

        api_data = response.json()
        pagination = api_data.get("data", {}).get("pagination", {})
        page_count = pagination.get("pageCount", 1)
        total_employees = pagination.get("totalCount", 0)

        cache.set(progress_key, {
            "status": "running",
            "percent": 5,
            "message": f"{page_count} sahifa topildi",
            "total": total_employees
        })

        all_employees = []
        page = 1

        while page <= page_count:
            current_params = params.copy()
            current_params["page"] = page
            response = requests.get(base_url, headers=headers, params=current_params, timeout=30)
            if response.status_code != 200:
                cache.set(progress_key, {"status": "error", "message": f"Sahifa {page} xato"})
                return

            api_data = response.json()
            items = api_data.get("data", {}).get("items", [])

            for emp in items:
                emp_id = emp.get("employee_id_number")
                if not emp_id: continue

                username = str(emp_id)
                birth_date = None
                if emp.get("birth_date"):
                    try:
                        birth_date = datetime.fromtimestamp(int(emp.get("birth_date")))
                    except: pass

                dept = emp.get("department", {}) or {}
                image_file = None
                if emp.get("image"):
                    try:
                        img_res = requests.get(emp["image"], timeout=10)
                        if img_res.status_code == 200 and len(img_res.content) > 1000:
                            image_file = ContentFile(img_res.content, f"{username}.jpg")
                    except: pass

                employee_data = {
                    "username": username,
                    "role": CustomUser.Role.EMPLOYEE,
                    "full_name": emp.get("full_name"),
                    "first_name": emp.get("first_name"),
                    "second_name": emp.get("second_name"),
                    "third_name": emp.get("third_name"),
                    "gender": emp.get("gender", {}).get("name"),
                    "birth_date": birth_date,
                    "year_of_enter": str(emp.get("year_of_enter", "")),
                    "employee_id_number": username,
                    "department_name": dept.get("name"),
                    "department_code": dept.get("code"),
                    "specialty": emp.get("specialty"),
                    "position": emp.get("staffPosition", {}).get("name"),
                    "active": bool(emp.get("active", True)),
                    "image": image_file,
                }
                all_employees.append((username, employee_data))

            # Progress
            progress = int((page / page_count) * 80) + 5
            cache.set(progress_key, {
                "status": "running",
                "percent": progress,
                "message": f"Sahifa {page}/{page_count}",
                "current": len(all_employees)
            })
            page += 1

        # DB
        cache.set(progress_key, {"status": "saving", "percent": 90, "message": "Saqlanmoqda..."})
        created = updated = 0

        with transaction.atomic():
            for idx, (emp_id, defaults) in enumerate(all_employees):
                if defaults.get("image"):
                    _, is_created = CustomUser.objects.update_or_create(
                        employee_id_number=emp_id, defaults=defaults
                    )
                else:
                    safe = {k: v for k, v in defaults.items() if k != "image"}
                    _, is_created = CustomUser.objects.update_or_create(
                        employee_id_number=emp_id, defaults=safe
                    )
                if is_created: created += 1
                else: updated += 1

                if idx % 100 == 0:
                    cache.set(progress_key, {
                        "status": "saving",
                        "percent": 90 + int((idx / len(all_employees)) * 10),
                        "message": f"{idx}/{len(all_employees)}"
                    })

        cache.set(progress_key, {
            "status": "done",
            "percent": 100,
            "message": f"{created} yangi, {updated} yangilandi",
            "created": created, "updated": updated
        })

    except Exception as e:
        cache.set(progress_key, {"status": "error", "message": str(e)})