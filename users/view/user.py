import datetime
import json
import logging
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime
from attendance.models import SiteSettings
from users.models import CustomUser, FaceEncoding
from django.core.paginator import Paginator
import requests
from django.db.models import Count
from django.db.models import Q, Exists, OuterRef


@login_required(login_url='login')
def profile_view(request):
    user = request.user  # hozirgi foydalanuvchi

    # Breadcrumbs ma’lumotlari
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},  # agar sizda home url mavjud bo‘lsa
        {'name': 'Profil', 'url': None},  # oxirgi element faqat matn, link bo‘lmaydi
    ]

    context = {
        'user': user,
        'breadcrumbs': breadcrumbs,  # shu yerda qo‘shildi
    }
    return render(request, 'users/my_profile.html', context)


@login_required(login_url='login')
def face_encoding_list_view(request):
    # === Asosiy queryset ===
    encodings_qs = FaceEncoding.objects.select_related('user').order_by('-created_at')

    # === Qidiruv (foydalanuvchi ismi, ID raqamlari, email) ===
    search_query = request.GET.get('q', '').strip()
    if search_query:
        encodings_qs = encodings_qs.filter(
            Q(user__full_name__icontains=search_query) |
            Q(user__username__icontains=search_query) |
            Q(user__student_id_number__icontains=search_query) |
            Q(user__employee_id_number__icontains=search_query) |
            Q(user__email__icontains=search_query)
        )

    # === Rol bo‘yicha filter (student/employee/superadmin) ===
    role_filter = request.GET.get('role')
    if role_filter == 'student':
        encodings_qs = encodings_qs.filter(user__role=CustomUser.Role.STUDENT, user__is_superuser=False)
    elif role_filter == 'employee':
        encodings_qs = encodings_qs.filter(user__role=CustomUser.Role.EMPLOYEE, user__is_superuser=False)
    elif role_filter == 'superadmin':
        encodings_qs = encodings_qs.filter(user__is_superuser=True)

    # === Pagination ===
    paginator = Paginator(encodings_qs, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # === Breadcrumbs ===
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Yuz ma’lumotlari (Face Encodings)', 'url': None},
    ]

    # === Context ===
    context = {
        'encodings': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'breadcrumbs': breadcrumbs,
        'search_query': search_query,
        'current_role': role_filter,
    }
    return render(request, 'users/face_encodings_list.html', context)

@login_required(login_url='login')
def users_list_view(request):
    # === Asosiy queryset ===
    users_qs = CustomUser.objects.annotate(
        has_face_encoding=Exists(FaceEncoding.objects.filter(user=OuterRef('id')))
    )

    # === Filter parametrlari ===
    role_filter = request.GET.get('role')
    search_query = request.GET.get('q', '').strip()

    # === Rol bo‘yicha filter ===
    if role_filter == 'student':
        users_qs = users_qs.filter(role=CustomUser.Role.STUDENT, is_superuser=False)
    elif role_filter == 'employee':
        users_qs = users_qs.filter(role=CustomUser.Role.EMPLOYEE, is_superuser=False)
    elif role_filter == 'superadmin':
        users_qs = users_qs.filter(is_superuser=True)
    # agar `role` param yo‘q bo‘lsa — hech narsa filtrlanmaydi

    # === Qidiruv (full_name, username, ID raqamlari, email) ===
    if search_query:
        users_qs = users_qs.filter(
            Q(full_name__icontains=search_query) |
            Q(username__icontains=search_query) |
            Q(student_id_number__icontains=search_query) |
            Q(employee_id_number__icontains=search_query) |
            Q(email__icontains=search_query)
        )

    # === Tartiblash ===
    users_qs = users_qs.order_by('full_name', 'username')

    # === Pagination ===
    paginator = Paginator(users_qs, 50)
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)

    # === Breadcrumbs ===
    breadcrumbs = [
        {'name': 'Bosh sahifa', 'url': '/'},
        {'name': 'Foydalanuvchilar', 'url': None},
    ]

    # === Context ===
    context = {
        'users': page_obj,
        'page_obj': page_obj,
        'paginator': paginator,
        'breadcrumbs': breadcrumbs,
        'current_role': role_filter,
        'search_query': search_query,
    }
    return render(request, 'users/users_list.html', context)


@login_required
def get_groups(request):
    """
    HEMIS dan akademik guruhlar ro‘yxatini olish.
    Foydalanuvchi yozgan matn bo‘yicha (search) filtrlanadi.
    """
    try:
        # 1️⃣ HEMIS sozlamalari
        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            return JsonResponse({"error": "HEMIS sozlamalari topilmadi."}, status=400)

        hemis_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/group-list"
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {settings.hemis_api_token}"
        }

        # 2️⃣ Qidiruv parametri
        search = request.GET.get("search", "").strip().lower()

        # 3️⃣ So‘rov yuborish
        response = requests.get(f"{hemis_url}?page=1&limit=200", headers=headers, timeout=10)

        if response.status_code != 200:
            return JsonResponse({"error": f"HEMIS bilan aloqa yo‘q ({response.status_code})"}, status=500)

        # 4️⃣ JSON javobni xavfsiz tahlil qilish
        try:
            data = response.json() or {}
        except Exception:
            return JsonResponse({"error": "HEMIS noto‘g‘ri JSON format qaytardi."}, status=500)

        # 5️⃣ Ma’lumotlar strukturasini aniqlash
        data_block = data.get("data")
        if isinstance(data_block, list) and data_block:
            items = data_block[0].get("items", [])
        elif isinstance(data_block, dict):
            items = data_block.get("items", [])
        else:
            items = []

        # 6️⃣ Filtrlash
        groups = []
        for g in items:
            name = g.get("name", "")
            if not search or search in name.lower():
                groups.append({
                    "id": g.get("id"),
                    "name": name,
                })

        return JsonResponse({
            "success": True,
            "count": len(groups),
            "groups": groups[:30]  # 30 ta natija bilan cheklaymiz
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


@login_required(login_url='login')
def import_users_view(request):
    """
    Foydalanuvchilarni import qilish sahifasi.
    Statistik ma'lumotlar ham qo'shildi.
    """
    # Umumiy sonlar
    staff_count = CustomUser.objects.filter(role=CustomUser.Role.EMPLOYEE).count()
    student_count = CustomUser.objects.filter(role=CustomUser.Role.STUDENT).count()

    # Hodimlar bo'yicha statistika (bo'lim nomi bo'yicha)
    staff_stats = (
        CustomUser.objects
        .filter(role=CustomUser.Role.EMPLOYEE, department_name__isnull=False)
        .values('department_name')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]  # Top 10 bo'lim
    )

    # Talabalar bo'yicha statistika (guruh nomi bo'yicha)
    student_stats = (
        CustomUser.objects
        .filter(role=CustomUser.Role.STUDENT, group_name__isnull=False)
        .values('group_name')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]  # Top 10 guruh
    )

    return render(request, "users/users_import.html", {
        "breadcrumbs": [
            {'name': 'Bosh sahifa', 'url': '/'},
            {'name': 'Foydalanuvchilarni import qilish', 'url': None},
        ],
        "staff_count": staff_count,
        "student_count": student_count,
        "staff_stats": list(staff_stats),
        "student_stats": list(student_stats),
    })


logger = logging.getLogger(__name__)

# Global progress tracker
SYNC_PROGRESS = {
    "students": {"total": 0, "processed": 0, "status": "idle"},
    "employees": {"total": 0, "processed": 0, "status": "idle"}
}


def _reset_progress(sync_type):
    SYNC_PROGRESS[sync_type] = {"total": 0, "processed": 0, "status": "running"}


def _update_progress(sync_type, processed, total=None):
    if total:
        SYNC_PROGRESS[sync_type]["total"] = total
    SYNC_PROGRESS[sync_type]["processed"] = processed
    if processed >= SYNC_PROGRESS[sync_type]["total"]:
        SYNC_PROGRESS[sync_type]["status"] = "completed"


@login_required
@csrf_exempt
def sync_students_from_hemis(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        body = json.loads(request.body.decode('utf-8'))
        sync_type = body.get("type")
        group_id = body.get("group_id")

        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            return JsonResponse({"error": "HEMIS sozlamalari topilmadi"}, status=400)

        base_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/student-list"
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {settings.hemis_api_token}"
        }
        params = {"page": 1, "limit": 200}
        if sync_type == "group" and group_id:
            params["_group"] = str(group_id)

        # Birinchi sahifa: umumiy sonni olish
        response = requests.get(base_url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            return JsonResponse({"error": f"API xato: {response.status_code}"}, status=400)

        api_data = response.json()
        if not api_data.get("success"):
            return JsonResponse({"error": "API success=False"}, status=400)

        pagination = api_data.get("data", {}).get("pagination", {})
        page_count = pagination.get("pageCount", 1)
        total_items = pagination.get("totalCount", 0)

        _reset_progress("students")
        _update_progress("students", 0, total_items)

        created = updated = 0
        all_students = []

        # Barcha sahifalarni olish
        for page in range(1, page_count + 1):
            params["page"] = page
            resp = requests.get(base_url, headers=headers, params=params, timeout=30)
            if resp.status_code != 200:
                continue

            data = resp.json().get("data", {}).get("items", [])
            for student in data:
                sid = student.get("student_id_number")
                if not sid:
                    continue

                username = str(sid).strip()
                if not username:
                    continue

                birth_date = None
                if student.get("birth_date"):
                    try:
                        birth_date = datetime.fromtimestamp(int(student["birth_date"])).date()
                    except:
                        pass

                image_file = None
                if student.get("image"):
                    try:
                        img_res = requests.get(student["image"], timeout=10)
                        if img_res.status_code == 200 and len(img_res.content) > 500:
                            image_file = ContentFile(img_res.content, f"{username}.jpg")
                    except:
                        pass

                defaults = {
                    "username": username,
                    "role": CustomUser.Role.STUDENT,
                    "full_name": student.get("full_name"),
                    "short_name": student.get("short_name"),
                    "first_name": student.get("first_name"),
                    "second_name": student.get("second_name"),
                    "third_name": student.get("third_name"),
                    "gender": student.get("gender", {}).get("name"),
                    "birth_date": birth_date,
                    "student_id_number": username,
                    "department_name": student.get("department", {}).get("name"),
                    "department_code": student.get("department", {}).get("code"),
                    "specialty": student.get("specialty", {}).get("name"),
                    "group_name": student.get("group", {}).get("name"),
                    "education_year": student.get("educationYear", {}).get("name"),
                    "gpa": student.get("avg_gpa"),
                    "year_of_enter": student.get("year_of_enter"),
                    "active": True,
                }
                if image_file:
                    defaults["image"] = image_file

                all_students.append((username, defaults))

            _update_progress("students", len(all_students))

        # Bazaga yozish
        with transaction.atomic():
            for username, defaults in all_students:
                safe_defaults = {k: v for k, v in defaults.items() if k != "image" or v}
                obj, is_created = CustomUser.objects.update_or_create(
                    student_id_number=username,
                    defaults=safe_defaults
                )
                if is_created:
                    created += 1
                else:
                    updated += 1

        _update_progress("students", total_items)
        return JsonResponse({
            "success": True,
            "message": f"{created} yangi, {updated} yangilandi",
            "created": created,
            "updated": updated,
            "total": total_items
        })

    except Exception as e:
        logger.exception(e)
        SYNC_PROGRESS["students"]["status"] = "error"
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@csrf_exempt
def sync_employees_from_hemis(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        data = json.loads(request.body)
        department_id = data.get('department_id')

        settings = SiteSettings.objects.first()
        if not settings or not settings.hemis_url or not settings.hemis_api_token:
            return JsonResponse({"error": "HEMIS sozlamalari topilmadi"}, status=400)

        base_url = f"{settings.hemis_url.rstrip('/')}/rest/v1/data/employee-list"
        headers = {"accept": "application/json", "Authorization": f"Bearer {settings.hemis_api_token}"}
        params = {"type": "all", "page": 1, "limit": 200}
        if department_id:
            params["_department"] = department_id

        response = requests.get(base_url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            return JsonResponse({"error": f"API xato: {response.status_code}"}, status=500)

        api_data = response.json()
        pagination = api_data.get("data", {}).get("pagination", {})
        page_count = pagination.get("pageCount", 1)
        total_items = pagination.get("totalCount", 0)

        _reset_progress("employees")
        _update_progress("employees", 0, total_items)

        created = updated = 0
        all_employees = []

        for page in range(1, page_count + 1):
            params["page"] = page
            resp = requests.get(base_url, headers=headers, params=params, timeout=30)
            if resp.status_code != 200:
                continue

            items = resp.json().get("data", {}).get("items", [])
            for emp in items:
                emp_id = emp.get("employee_id_number")
                if not emp_id:
                    continue

                username = str(emp_id).strip()
                if not username:
                    continue

                birth_date = None
                if emp.get("birth_date"):
                    try:
                        birth_date = datetime.fromtimestamp(int(emp["birth_date"])).date()
                    except:
                        pass

                image_file = None
                if emp.get("image"):
                    try:
                        img_res = requests.get(emp["image"], timeout=10)
                        if img_res.status_code == 200 and len(img_res.content) > 1000:
                            image_file = ContentFile(img_res.content, f"{username}.jpg")
                    except:
                        pass

                dept = emp.get("department", {}) or {}
                defaults = {
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
                }
                if image_file:
                    defaults["image"] = image_file

                all_employees.append((username, defaults))

            _update_progress("employees", len(all_employees))

        with transaction.atomic():
            for username, defaults in all_employees:
                safe_defaults = {k: v for k, v in defaults.items() if k != "image" or v}
                obj, is_created = CustomUser.objects.update_or_create(
                    employee_id_number=username,
                    defaults=safe_defaults
                )
                if is_created:
                    created += 1
                else:
                    updated += 1

        _update_progress("employees", total_items)
        return JsonResponse({
            "success": True,
            "message": f"{created} yangi, {updated} yangilandi",
            "created": created,
            "updated": updated,
            "total": total_items
        })

    except Exception as e:
        logger.exception(e)
        SYNC_PROGRESS["employees"]["status"] = "error"
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def get_sync_progress(request):
    sync_type = request.GET.get("type", "students")
    progress = SYNC_PROGRESS.get(sync_type, {"total": 0, "processed": 0, "status": "idle"})

    total = progress["total"]
    processed = progress["processed"]
    percent = round((processed / total) * 100, 1) if total > 0 else 0

    return JsonResponse({
        "type": sync_type,
        "status": progress["status"],
        "processed": processed,
        "total": total,
        "percent": percent
    })


@login_required
@csrf_exempt
def clear_employees(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        # Faqat xodimlarni o'chiramiz (talabalar qoladi)
        deleted_count, _ = CustomUser.objects.filter(role=CustomUser.Role.EMPLOYEE).delete()

        return JsonResponse({
            "success": True,
            "message": f"{deleted_count} ta xodim o‘chirildi"
        })

    except Exception as e:
        logger.exception(e)
        return JsonResponse({
            "success": False,
            "message": f"O‘chirishda xatolik: {str(e)}"
        }, status=500)

@login_required
@csrf_exempt
def clear_students(request):
    if request.method != 'POST':
        return JsonResponse({"error": "POST method required"}, status=405)

    try:
        # 🔒 Faqat talabalardan iborat foydalanuvchilarni o‘chirish (superadminlarni saqlab qolamiz)
        deleted_count, _ = (
            CustomUser.objects
            .filter(role=CustomUser.Role.STUDENT)
            .exclude(is_superuser=True)
            .delete()
        )

        return JsonResponse({
            "success": True,
            "message": f"✅ {deleted_count} ta talaba o‘chirildi"
        })

    except Exception as e:
        logger.exception(e)
        return JsonResponse({
            "success": False,
            "message": f"O‘chirishda xatolik: {str(e)}"
        }, status=500)
