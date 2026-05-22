"""
Service status utilities for the Service Logs Dashboard.

Provides functions to query systemd service status for the three
allowed services: daphne, camera-daemon, and celery.
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.utils import timezone


# ============================================================
# Constants
# ============================================================

ALLOWED_SERVICES = [
    "daphne.service",
    "camera-daemon.service",
    "celery.service",
]

ALLOWED_ACTIONS = {"start", "stop", "restart", "enable", "disable"}

SERVICE_META = {
    "daphne.service": {
        "display_name": "Daphne (ASGI)",
        "icon": "mdi-web",
        "color": "primary",
    },
    "camera-daemon.service": {
        "display_name": "Camera Daemon",
        "icon": "mdi-camera",
        "color": "success",
    },
    "celery.service": {
        "display_name": "Celery Worker",
        "icon": "mdi-cog-transfer",
        "color": "warning",
    },
}


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ServiceStatus:
    """Runtime status of a systemd service (not persisted)."""

    name: str
    display_name: str
    status: str  # "active" | "inactive" | "failed" | "activating" | "unknown"
    sub_state: str  # "running" | "dead" | "failed" | "auto-restart" | "unknown"
    pid: Optional[int]
    uptime: Optional[str]
    memory_mb: Optional[float]
    cpu_percent: Optional[float]
    description: str
    icon: str
    color: str


# ============================================================
# Helper Functions
# ============================================================

def parse_systemctl_properties(output: str) -> dict:
    """
    Parse key=value output from `systemctl show --property=...`.

    Each line is in the format: Key=Value
    """
    props = {}
    for line in output.strip().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def calculate_uptime(timestamp_str: Optional[str]) -> Optional[str]:
    """
    Convert a systemctl ActiveEnterTimestamp to a human-readable uptime string.

    Args:
        timestamp_str: Timestamp string from systemctl (e.g., "Mon 2024-01-15 10:30:00 +05")
                       or empty/None if service is not active.

    Returns:
        Human-readable uptime like "2h 15m", "3d 4h", or None if not parseable.
    """
    if not timestamp_str or timestamp_str.strip() == "":
        return None

    try:
        # systemctl timestamps look like: "Mon 2024-01-15 10:30:00 +05"
        # or "Fri 2025-01-10 14:22:33 +05" etc.
        # We try multiple formats to be resilient
        ts_str = timestamp_str.strip()

        # Try parsing with timezone info
        # Format: "Day YYYY-MM-DD HH:MM:SS TZ"
        # Remove the day-of-week prefix if present
        parts = ts_str.split()
        if len(parts) >= 3:
            # Try to find the date part (YYYY-MM-DD)
            date_part = None
            time_part = None
            for i, part in enumerate(parts):
                if len(part) == 10 and part[4] == "-" and part[7] == "-":
                    date_part = part
                    if i + 1 < len(parts):
                        time_part = parts[i + 1]
                    break

            if date_part and time_part:
                dt_str = f"{date_part} {time_part}"
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                # Make it timezone-aware using the system timezone
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
                now = timezone.now()
                delta = now - dt

                total_seconds = int(delta.total_seconds())
                if total_seconds < 0:
                    return None

                days = total_seconds // 86400
                hours = (total_seconds % 86400) // 3600
                minutes = (total_seconds % 3600) // 60

                if days > 0:
                    return f"{days}d {hours}h"
                elif hours > 0:
                    return f"{hours}h {minutes}m"
                elif minutes > 0:
                    return f"{minutes}m"
                else:
                    return "just started"

        return None
    except (ValueError, TypeError, IndexError, OverflowError):
        return None


# ============================================================
# Main Functions
# ============================================================

def get_service_status(service_name: str) -> ServiceStatus:
    """
    Fetch the current status of a systemd service using `systemctl show`.

    Args:
        service_name: The systemd service name (e.g., "daphne.service").
                      Must be in ALLOWED_SERVICES.

    Returns:
        A ServiceStatus dataclass with the current state of the service.
        On any failure, returns a ServiceStatus with status="unknown".
    """
    meta = SERVICE_META.get(service_name, {
        "display_name": service_name,
        "icon": "mdi-help",
        "color": "secondary",
    })

    properties = [
        "ActiveState",
        "SubState",
        "MainPID",
        "ActiveEnterTimestamp",
        "MemoryCurrent",
        "Description",
    ]

    cmd = [
        "systemctl", "show", service_name,
        "--property=" + ",".join(properties),
    ]

    try:
        output = subprocess.check_output(cmd, text=True, timeout=5)
        parsed = parse_systemctl_properties(output)

        # Calculate uptime from ActiveEnterTimestamp
        uptime = calculate_uptime(parsed.get("ActiveEnterTimestamp"))

        # Convert memory from bytes to MB
        memory_raw = parsed.get("MemoryCurrent", "0") or "0"
        # MemoryCurrent can be "[not set]" or a number
        try:
            memory_bytes = int(memory_raw)
        except (ValueError, TypeError):
            memory_bytes = 0
        memory_mb = round(memory_bytes / (1024 * 1024), 1) if memory_bytes > 0 else None

        # Parse PID
        pid_raw = parsed.get("MainPID", "0") or "0"
        try:
            pid = int(pid_raw)
        except (ValueError, TypeError):
            pid = 0
        pid = pid if pid != 0 else None

        return ServiceStatus(
            name=service_name,
            display_name=meta["display_name"],
            status=parsed.get("ActiveState", "unknown"),
            sub_state=parsed.get("SubState", "unknown"),
            pid=pid,
            uptime=uptime,
            memory_mb=memory_mb,
            cpu_percent=None,
            description=parsed.get("Description", ""),
            icon=meta["icon"],
            color=meta["color"],
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, Exception):
        return ServiceStatus(
            name=service_name,
            display_name=meta["display_name"],
            status="unknown",
            sub_state="unknown",
            pid=None,
            uptime=None,
            memory_mb=None,
            cpu_percent=None,
            description="",
            icon=meta["icon"],
            color=meta["color"],
        )


def get_all_services_status() -> list:
    """
    Fetch status for all 3 allowed services.

    Returns:
        A list of exactly 3 dictionaries, one per allowed service.
        Each dict contains: name, status, display_name, uptime, memory_mb,
        pid, icon, color, sub_state, description, cpu_percent.

    This function never raises an exception. If a service status
    cannot be fetched, it returns status="unknown" for that service.
    """
    results = []
    for service_name in ALLOWED_SERVICES:
        try:
            svc_status = get_service_status(service_name)
        except Exception:
            # Should never happen since get_service_status handles errors,
            # but as an extra safety net:
            meta = SERVICE_META.get(service_name, {})
            svc_status = ServiceStatus(
                name=service_name,
                display_name=meta.get("display_name", service_name),
                status="unknown",
                sub_state="unknown",
                pid=None,
                uptime=None,
                memory_mb=None,
                cpu_percent=None,
                description="",
                icon=meta.get("icon", "mdi-help"),
                color=meta.get("color", "secondary"),
            )

        results.append({
            "name": svc_status.name,
            "display_name": svc_status.display_name,
            "status": svc_status.status,
            "sub_state": svc_status.sub_state,
            "pid": svc_status.pid,
            "uptime": svc_status.uptime,
            "memory_mb": svc_status.memory_mb,
            "cpu_percent": svc_status.cpu_percent,
            "description": svc_status.description,
            "icon": svc_status.icon,
            "color": svc_status.color,
        })

    return results
