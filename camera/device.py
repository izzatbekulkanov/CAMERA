import logging
import os

logger = logging.getLogger(__name__)


def _normalize_device(value: str | None) -> str:
    value = (value or "").strip().lower()
    if value in {"gpu", "cuda"}:
        return "gpu"
    if value in {"cpu"}:
        return "cpu"
    return "auto"


def requested_face_device() -> str:
    """
    Face processing device source of truth:
    1) env FACE_PROCESSING_DEVICE / CAMERA_FACE_DEVICE / FACE_DEVICE
    2) SiteSettings.face_processing_device
    3) auto

    `auto` GPU bor joyda GPU, Windows/dev kabi joyda CPU fallback qiladi.
    """
    for key in ("FACE_PROCESSING_DEVICE", "CAMERA_FACE_DEVICE", "FACE_DEVICE"):
        if os.getenv(key):
            return _normalize_device(os.getenv(key))

    try:
        from django.apps import apps

        if not apps.ready:
            return "auto"

        from attendance.models import SiteSettings

        settings_obj = SiteSettings.objects.first()
        if settings_obj:
            return _normalize_device(getattr(settings_obj, "face_processing_device", None))
    except Exception as exc:
        logger.debug("[DEVICE] SiteSettings read skipped: %s", exc)

    return "auto"


def _onnx_cuda_available() -> bool:
    try:
        import onnxruntime as ort

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _torch_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def get_face_runtime() -> dict:
    requested = requested_face_device()
    onnx_cuda_available = _onnx_cuda_available()
    torch_cuda_available = _torch_cuda_available()
    use_cuda = requested == "gpu" or (requested == "auto" and onnx_cuda_available)

    if use_cuda and not onnx_cuda_available:
        logger.warning("[DEVICE] GPU requested, but CUDA providers are not available. Falling back to CPU.")
        use_cuda = False

    if use_cuda:
        return {
            "requested": requested,
            "device_type": "cuda",
            "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            "ctx_id": 0,
            "onnx_cuda_available": onnx_cuda_available,
            "torch_cuda_available": torch_cuda_available,
        }

    return {
        "requested": requested,
        "device_type": "cpu",
        "providers": ["CPUExecutionProvider"],
        "ctx_id": -1,
        "onnx_cuda_available": onnx_cuda_available,
        "torch_cuda_available": torch_cuda_available,
    }
