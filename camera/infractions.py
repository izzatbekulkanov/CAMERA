import os
import logging
import cv2
import numpy as np
import torch
from ultralytics import YOLO

logger = logging.getLogger(__name__)

_BASE_MODEL = None
_FIGHT_MODEL = None
_SMOKE_MODEL = None
_MODELS_LOCK = None

def get_models():
    global _BASE_MODEL, _FIGHT_MODEL, _SMOKE_MODEL, _MODELS_LOCK
    if _MODELS_LOCK is None:
        import threading
        _MODELS_LOCK = threading.Lock()
        
    with _MODELS_LOCK:
        if _BASE_MODEL is None:
            # Model path
            base_path = os.path.join(os.path.dirname(__file__), "..", "yolov8n.pt")
            if not os.path.exists(base_path):
                base_path = "yolov8n.pt"  # Fallback to auto-download if missing
            try:
                _BASE_MODEL = YOLO(base_path)
                logger.info("[INFRACTION] Base YOLOv8 model loaded successfully.")
            except Exception as e:
                logger.error("[INFRACTION] Failed to load base YOLOv8 model: %s", e)
                _BASE_MODEL = None

        if _FIGHT_MODEL is None:
            fight_path = os.path.join(os.path.dirname(__file__), "..", "models", "yolov8_fight.pt")
            try:
                _FIGHT_MODEL = YOLO(fight_path)
                logger.info("[INFRACTION] Dedicated Fight/Violence model loaded successfully.")
            except Exception as e:
                logger.error("[INFRACTION] Failed to load fight model: %s", e)
                _FIGHT_MODEL = None

        if _SMOKE_MODEL is None:
            smoke_path = os.path.join(os.path.dirname(__file__), "..", "models", "yolov8_smoking_behavior.pt")
            try:
                _SMOKE_MODEL = YOLO(smoke_path)
                logger.info("[INFRACTION] Dedicated Smoking Behavior model loaded successfully.")
            except Exception as e:
                logger.error("[INFRACTION] Failed to load smoking model: %s", e)
                _SMOKE_MODEL = None

        return _BASE_MODEL, _FIGHT_MODEL, _SMOKE_MODEL


def calculate_iou(box1: list[int], box2: list[int]) -> float:
    """
    Computes Intersection over Union (IoU) between two bounding boxes.
    Each box is in [x1, y1, x2, y2] format.
    """
    x_left = max(box1[0], box2[0])
    y_top = max(box1[1], box2[1])
    x_right = min(box1[2], box2[2])
    y_bottom = min(box1[3], box2[3])

    if x_right <= x_left or y_bottom <= y_top:
        return 0.0

    intersection_area = (x_right - x_left) * (y_bottom - y_top)
    box1_area = (box1[2] - box1[0]) * (box1[3] - box1[1])
    box2_area = (box2[2] - box2[0]) * (box2[3] - box2[1])
    union_area = float(box1_area + box2_area - intersection_area)

    if union_area <= 0:
        return 0.0
    return intersection_area / union_area


def analyze_head_hsv(head_crop) -> tuple[bool, bool, int, int]:
    """
    Analyzes head crop for:
    1. Burning orange-red tip of cigarette.
    2. Surrounding whitish/greyish smoke waves.
    Returns (has_red_tip, has_smoke_waves, red_pixels, smoke_pixels)
    """
    if head_crop is None or head_crop.size == 0:
        return False, False, 0, 0

    hsv = cv2.cvtColor(head_crop, cv2.COLOR_BGR2HSV)

    # Red/Orange ranges in HSV for cigarette burning tip
    lower_red1 = np.array([0, 50, 100])
    upper_red1 = np.array([22, 255, 255])
    lower_red2 = np.array([165, 50, 100])
    upper_red2 = np.array([180, 255, 255])

    mask_red1 = cv2.inRange(hsv, lower_red1, upper_red1)
    mask_red2 = cv2.inRange(hsv, lower_red2, upper_red2)
    mask_red = cv2.bitwise_or(mask_red1, mask_red2)

    # Whitish/greyish smoke ranges in HSV
    lower_smoke = np.array([0, 0, 100])
    upper_smoke = np.array([180, 60, 255])

    mask_smoke = cv2.inRange(hsv, lower_smoke, upper_smoke)

    red_pixels = cv2.countNonZero(mask_red)
    smoke_pixels = cv2.countNonZero(mask_smoke)
    total_pixels = head_crop.shape[0] * head_crop.shape[1]

    # Needs at least a couple of pixels for the tiny burning tip,
    # and some smoke pixels (e.g. >= 8 pixels and > 0.6% of crop area)
    # Limit red pixels to <= 35 to avoid matching skin/hands/fingers
    has_red_tip = 2 <= red_pixels <= 35
    has_smoke_waves = smoke_pixels >= 8 and (smoke_pixels / total_pixels > 0.006)

    return has_red_tip, has_smoke_waves, red_pixels, smoke_pixels


def detect_infractions(frame) -> list[dict]:
    """
    Detects infractions (fights, smoking) in the frame according to strict AI rules:
    1. Urush va Janjallar:
       Detect persons, compute IoU of pairs. If IoU > 0.40, verify using the dedicated fight model.
    2. Sigaret Chekish:
       For each person, crop Head/Face (Head Crop). Perform HSV analysis for red tip and smoke waves.
       Verify using the dedicated smoking model.
    3. Cooldown:
       Handled at caller level (rtsp_runner.py), keeping 30s threshold.
    """
    base_model, fight_model, smoke_model = get_models()
    if base_model is None or fight_model is None or smoke_model is None:
        logger.error("[INFRACTION] Models are not fully loaded.")
        return []

    device = "cuda" if torch.cuda.is_available() else "cpu"
    img_h, img_w = frame.shape[:2]
    infractions = []

    # Detect persons
    person_boxes = []
    try:
        results_person = base_model.predict(frame, classes=[0], device=device, verbose=False)
        if results_person and len(results_person) > 0:
            for box in results_person[0].boxes:
                coords = box.xyxy[0].cpu().numpy().astype(int).tolist()
                conf = float(box.conf[0].cpu().item())
                # Filter out low-confidence base persons to keep system robust
                if conf > 0.35:
                    person_boxes.append(coords)
    except Exception as pe:
        logger.error("[INFRACTION] Person detection failed: %s", pe)

    # 1. Fight/Violence Detection (Rule 1)
    # Check if there are 2 or more persons whose boxes overlap by more than 40% (IoU > 0.40)
    overlapping_pairs = []
    if len(person_boxes) >= 2:
        for i in range(len(person_boxes)):
            for j in range(i + 1, len(person_boxes)):
                iou = calculate_iou(person_boxes[i], person_boxes[j])
                if iou > 0.40:
                    overlapping_pairs.append((person_boxes[i], person_boxes[j]))

    if overlapping_pairs:
        # Run dedicated fight model
        try:
            results_fight = fight_model.predict(frame, device=device, verbose=False)
            if results_fight and len(results_fight) > 0:
                for box in results_fight[0].boxes:
                    cls = int(box.cls[0].cpu().item())
                    conf = float(box.conf[0].cpu().item())
                    # Class 1 is 'violence'
                    if cls == 1 and conf > 0.45:
                        fbox = box.xyxy[0].cpu().numpy().astype(int).tolist()
                        
                        # Match with the overlapping pairs: the fight model box must overlap
                        # with the region of the overlapping people.
                        for p1, p2 in overlapping_pairs:
                            # Union of the two person boxes
                            union_box = [
                                min(p1[0], p2[0]),
                                min(p1[1], p2[1]),
                                max(p1[2], p2[2]),
                                max(p1[3], p2[3])
                            ]
                            # Check overlap between fbox and union_box
                            ox1 = max(fbox[0], union_box[0])
                            oy1 = max(fbox[1], union_box[1])
                            ox2 = min(fbox[2], union_box[2])
                            oy2 = min(fbox[3], union_box[3])
                            
                            if ox2 > ox1 and oy2 > oy1:
                                infractions.append({
                                    "type": "fight",
                                    "confidence": round(conf, 2),
                                    "bbox": union_box
                                })
                                break # Avoid duplicates for the same fight detection box
        except Exception as e:
            logger.error("[INFRACTION] Fight model prediction failed: %s", e)

    # 2. Smoking Detection (Rule 2)
    # Check head crops using HSV and verify using the dedicated smoking model
    if person_boxes:
        try:
            # First, run the dedicated smoking model to detect smoke regions
            results_smoke = smoke_model.predict(frame, device=device, verbose=False)
            smoke_boxes = []
            if results_smoke and len(results_smoke) > 0:
                for box in results_smoke[0].boxes:
                    cls = int(box.cls[0].cpu().item())
                    conf = float(box.conf[0].cpu().item())
                    # Class 0 is 'cigarette', Class 2 is 'smoke' (from fine_tune_YOLOv8n.pt)
                    # Filter raw detections above 0.60 to apply tighter rules later
                    if conf > 0.60:
                        sbox = box.xyxy[0].cpu().numpy().astype(int).tolist()
                        smoke_boxes.append({"bbox": sbox, "conf": conf, "cls": cls})

            # Crop heads of each person and verify
            for p_bbox in person_boxes:
                px1, py1, px2, py2 = p_bbox
                p_w = px2 - px1
                p_h = py2 - py1
                
                # Head crop: top 25% of the person's bounding box
                hx1 = max(0, min(px1, img_w - 1))
                hx2 = max(0, min(px2, img_w))
                hy1 = max(0, min(py1, img_h - 1))
                hy2 = max(0, min(py1 + int(p_h * 0.25), img_h))
                
                if hx2 <= hx1 or hy2 <= hy1:
                    continue
                
                head_crop = frame[hy1:hy2, hx1:hx2]
                has_red, has_smoke, red_pix, smoke_pix = analyze_head_hsv(head_crop)
                
                # Verify smoking signs based on the dedicated model detections overlapping with the person
                is_smoking_verified = False
                best_conf = 0.0
                matched_smoke_box = None
                
                for s in smoke_boxes:
                    sbox = s["bbox"]
                    s_cls = s["cls"]
                    s_conf = s["conf"]
                    
                    s_w = sbox[2] - sbox[0]
                    s_h = sbox[3] - sbox[1]
                    
                    # 1. Size constraint: cigarette should be small (<= 80px in 1280x720 frame)
                    if s_cls == 0 and (s_w > 80 or s_h > 80):
                        continue
                        
                    # 2. Position constraint: must be in the upper 45% of the person's vertical height
                    s_cy = (sbox[1] + sbox[3]) / 2.0
                    limit_y = py1 + int(p_h * 0.45)
                    if s_cy > limit_y:
                        continue
                        
                    # 3. Overlap check with the person box
                    ox1 = max(sbox[0], px1)
                    oy1 = max(sbox[1], py1)
                    ox2 = min(sbox[2], px2)
                    oy2 = min(sbox[3], py2)
                    
                    if ox2 > ox1 and oy2 > oy1:
                        # We have overlap! Now apply strict validation logic:
                        if s_cls == 0:  # Cigarette
                            if s_conf >= 0.82:
                                is_smoking_verified = True
                                best_conf = max(best_conf, s_conf)
                                matched_smoke_box = sbox
                            elif s_conf >= 0.70:
                                # Requires smoke support (either another YOLO smoke box or HSV smoke waves)
                                has_yolo_smoke = any(k["cls"] == 2 and k["conf"] >= 0.60 for k in smoke_boxes)
                                if has_yolo_smoke or has_smoke:
                                    is_smoking_verified = True
                                    best_conf = max(best_conf, s_conf)
                                    matched_smoke_box = sbox
                        elif s_cls == 2:  # Smoke
                            if s_conf >= 0.78:
                                is_smoking_verified = True
                                best_conf = max(best_conf, s_conf)
                                matched_smoke_box = sbox
                                
                if is_smoking_verified:
                    infractions.append({
                        "type": "smoking",
                        "confidence": round(best_conf, 2),
                        "bbox": matched_smoke_box or [hx1, hy1, hx2, hy2]
                    })
        except Exception as e:
            logger.error("[INFRACTION] Smoking model/HSV analysis failed: %s", e)

    # 3. Sleeping Detection (using YOLOv8-pose keypoints analysis)
    try:
        from camera.pose import detect_sleeping_students
        sleeping_results = detect_sleeping_students(frame)
        for s in sleeping_results:
            if s["is_sleeping"]:
                infractions.append({
                    "type": "sleeping",
                    "confidence": round(s["confidence"], 2),
                    "bbox": [int(v) for v in s["bbox"]]
                })
    except Exception as exc:
        logger.error("[INFRACTION] Sleeping detection execution failed: %s", exc)

    return infractions
