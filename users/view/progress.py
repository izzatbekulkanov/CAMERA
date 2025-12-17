# users/progress.py
from typing import Any, Dict, Optional

from django.core.cache import cache
from django.utils import timezone

PROGRESS_TTL = 60 * 30  # 30 min
CACHE_KEY_PREFIX = "hemis_sync_progress:"


def key(sync_type: str) -> str:
    return f"{CACHE_KEY_PREFIX}{sync_type}"


def reset(sync_type: str, total: int = 0, message: str = "Boshlanmoqda...") -> None:
    now = timezone.now().isoformat()
    cache.set(
        key(sync_type),
        {
            "type": sync_type,
            "status": "running",
            "processed": 0,
            "total": int(total or 0),
            "message": message,
            "started_at": now,
            "updated_at": now,
        },
        timeout=PROGRESS_TTL,
    )


def update(sync_type: str, processed: int, total: Optional[int] = None, message: str = "") -> None:
    k = key(sync_type)
    data: Dict[str, Any] = cache.get(k) or {
        "type": sync_type,
        "status": "running",
        "processed": 0,
        "total": 0,
        "message": "",
        "started_at": timezone.now().isoformat(),
        "updated_at": timezone.now().isoformat(),
    }

    if total is not None:
        data["total"] = int(total or 0)

    data["processed"] = int(processed or 0)
    data["updated_at"] = timezone.now().isoformat()

    if message:
        data["message"] = message

    # complete
    if data.get("total") and data["processed"] >= data["total"]:
        data["status"] = "completed"
        data["processed"] = data["total"]
        if not data.get("message"):
            data["message"] = "Yakunlandi"

    cache.set(k, data, timeout=PROGRESS_TTL)


def error(sync_type: str, err: str) -> None:
    k = key(sync_type)
    data: Dict[str, Any] = cache.get(k) or {"type": sync_type}
    data.update(
        {
            "status": "error",
            "message": str(err),
            "updated_at": timezone.now().isoformat(),
        }
    )
    cache.set(k, data, timeout=PROGRESS_TTL)


def get(sync_type: str) -> Dict[str, Any]:
    data = cache.get(key(sync_type)) or {
        "type": sync_type,
        "status": "idle",
        "processed": 0,
        "total": 0,
        "message": "Kutilmoqda...",
        "updated_at": None,
    }

    total = int(data.get("total") or 0)
    processed = int(data.get("processed") or 0)
    percent = round((processed / total) * 100, 1) if total else 0.0

    return {
        "type": sync_type,
        "status": data.get("status", "idle"),
        "processed": processed,
        "total": total,
        "percent": percent,
        "message": data.get("message", ""),
        "updated_at": data.get("updated_at"),
    }
