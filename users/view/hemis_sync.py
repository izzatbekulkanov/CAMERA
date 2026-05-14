# users/view/hemis_sync.py
import json
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from users.view import progress as prog

try:
    from users.tasks import hemis_sync_students_task, hemis_sync_employees_task
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False


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

    if CELERY_AVAILABLE:
        task = hemis_sync_students_task.delay(body)
        return JsonResponse({"success": True, "task_id": task.id})

    hemis_sync_students_task(None, body)
    return JsonResponse({"success": True, "task_id": None, "mode": "direct"})


@login_required
@csrf_exempt
def sync_employees_from_hemis(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    body = json.loads((request.body or b"{}").decode("utf-8") or "{}")

    prog.reset("employees", 0, message="Task queue qilindi...")

    if CELERY_AVAILABLE:
        task = hemis_sync_employees_task.delay(body)
        return JsonResponse({"success": True, "task_id": task.id})

    hemis_sync_employees_task(None, body)
    return JsonResponse({"success": True, "task_id": None, "mode": "direct"})
