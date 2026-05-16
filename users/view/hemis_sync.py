# users/view/hemis_sync.py
import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from users.view import progress as prog


def _get_hemis_tasks():
    try:
        from users.tasks import hemis_sync_students_task, hemis_sync_employees_task

        celery_available = hasattr(hemis_sync_students_task, "delay") and hasattr(hemis_sync_employees_task, "delay")
        return celery_available, hemis_sync_students_task, hemis_sync_employees_task
    except ImportError:
        return False, None, None


@login_required
def get_sync_progress(request):
    sync_type = request.GET.get("type", "students")
    return JsonResponse(prog.get(sync_type))


@login_required
@csrf_exempt
def sync_students_from_hemis(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    body = json.loads((request.body or b"{}").decode("utf-8") or "{}")

    # progressni darhol running qilib qo'yamiz (task ichida ham reset qiladi)
    prog.reset("students", 0, message="Task queue qilindi...")

    celery_available, hemis_sync_students_task, _ = _get_hemis_tasks()
    if celery_available:
        task = hemis_sync_students_task.delay(body)
        return JsonResponse({"success": True, "task_id": task.id})

    if hemis_sync_students_task is None:
        return JsonResponse({"success": False, "message": "HEMIS task import xatosi"}, status=500)

    hemis_sync_students_task(None, body)
    return JsonResponse({"success": True, "task_id": None, "mode": "direct"})


@login_required
@csrf_exempt
def sync_employees_from_hemis(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    body = json.loads((request.body or b"{}").decode("utf-8") or "{}")

    prog.reset("employees", 0, message="Task queue qilindi...")

    celery_available, _, hemis_sync_employees_task = _get_hemis_tasks()
    if celery_available:
        task = hemis_sync_employees_task.delay(body)
        return JsonResponse({"success": True, "task_id": task.id})

    if hemis_sync_employees_task is None:
        return JsonResponse({"success": False, "message": "HEMIS task import xatosi"}, status=500)

    hemis_sync_employees_task(None, body)
    return JsonResponse({"success": True, "task_id": None, "mode": "direct"})
