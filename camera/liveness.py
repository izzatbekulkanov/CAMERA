# camera/liveness.py
import logging
import os
import cv2
import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

# Model fayli manzili
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "MiniFASNetV2.onnx")

class LivenessDetector:
    def __init__(self, model_path: str = MODEL_PATH, scale: float = 2.7):
        self.model_path = model_path
        self.scale = scale
        self.session = None
        self.input_name = None
        self.input_size = None
        self.output_name = None
        
        if not os.path.exists(model_path):
            logger.warning("[Liveness] Model file not found at %s. Liveness checks will default to True.", model_path)
            return

        try:
            # CUDA execution provider bo'lsa GPUda ishlaydi, aks holda CPUda
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
            self.session = ort.InferenceSession(model_path, providers=providers)
            
            input_cfg = self.session.get_inputs()[0]
            self.input_name = input_cfg.name
            self.input_size = tuple(input_cfg.shape[2:])

            output_cfg = self.session.get_outputs()[0]
            self.output_name = output_cfg.name
            logger.info("[Liveness] MiniFASNetV2 loaded successfully (device=%s)", self.session.get_providers())
        except Exception as exc:
            logger.exception("[Liveness] Failed to load ONNX model: %s", exc)

    def _xyxy2xywh(self, bbox: list[float]) -> list[int]:
        x1, y1, x2, y2 = bbox
        return [int(x1), int(y1), int(max(1, x2 - x1)), int(max(1, y2 - y1))]

    def _crop_face(self, image: np.ndarray, bbox: list[int]) -> np.ndarray:
        src_h, src_w = image.shape[:2]
        x, y, box_w, box_h = bbox

        scale = min((src_h - 1) / box_h, (src_w - 1) / box_w, self.scale)
        new_w = box_w * scale
        new_h = box_h * scale

        center_x = x + box_w / 2
        center_y = y + box_h / 2

        x1 = max(0, int(center_x - new_w / 2))
        y1 = max(0, int(center_y - new_h / 2))
        x2 = min(src_w - 1, int(center_x + new_w / 2))
        y2 = min(src_h - 1, int(center_y + new_h / 2))

        cropped = image[y1 : y2 + 1, x1 : x2 + 1]
        if cropped.size == 0:
            # Failsafe if crop is empty
            return cv2.resize(image[y:y+box_h, x:x+box_w], self.input_size[::-1])
        return cv2.resize(cropped, self.input_size[::-1])

    def _preprocess(self, image: np.ndarray, bbox: list[int]) -> np.ndarray:
        face = self._crop_face(image, bbox)
        face = face.astype(np.float32)
        # N, C, H, W formatiga o'tkazish
        face = np.transpose(face, (2, 0, 1))
        face = np.expand_dims(face, axis=0)
        return face

    def _softmax(self, x: np.ndarray) -> np.ndarray:
        e_x = np.exp(x - np.max(x, axis=1, keepdims=True))
        return e_x / e_x.sum(axis=1, keepdims=True)

    def is_live(self, image: np.ndarray, bbox_xyxy: list[float]) -> tuple[bool, float]:
        """
        Kadrda berilgan yuzning haqiqiy (real) yoki soxta (spoof) ekanligini tekshiradi.
        Qaytaradi: (is_real, score)
        """
        if self.session is None:
            return True, 1.0

        try:
            bbox_xywh = self._xyxy2xywh(bbox_xyxy)
            input_tensor = self._preprocess(image, bbox_xywh)
            
            outputs = self.session.run([self.output_name], {self.input_name: input_tensor})
            logits = outputs[0]
            probs = self._softmax(logits)
            
            label_idx = int(np.argmax(probs))
            score = float(probs[0, label_idx])
            
            # MiniFASNetda label_idx = 1 haqiqiy yuz
            is_real = (label_idx == 1)
            return is_real, score
        except Exception as exc:
            logger.error("[Liveness] Inference failed: %s", exc)
            return True, 1.0


# Yagona global instansiya (singleton)
_detector = None

def check_liveness(image: np.ndarray, bbox_xyxy: list[float]) -> tuple[bool, float]:
    global _detector
    if _detector is None:
        _detector = LivenessDetector()
    return _detector.is_live(image, bbox_xyxy)
