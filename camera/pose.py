# camera/pose.py
import logging
import os
import numpy as np
import torch
from ultralytics import YOLO

logger = logging.getLogger(__name__)

# Model faylini saqlash joyi
MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "yolov8n-pose.pt")

_pose_model = None

def get_pose_model():
    global _pose_model
    if _pose_model is None:
        try:
            # Agar models papkasida model bo'lmasa, yuklab olinadi
            if not os.path.exists(MODEL_DIR):
                os.makedirs(MODEL_DIR, exist_ok=True)
            
            # Ultralytics modelini yuklaymiz
            # Agar model pathda bo'lmasa, ultralytics internetdan yuklab beradi
            _pose_model = YOLO(MODEL_PATH if os.path.exists(MODEL_PATH) else "yolov8n-pose.pt")
            
            # Agar model internetdan yuklansa, uni models papkasiga saqlab qo'yamiz
            if not os.path.exists(MODEL_PATH):
                try:
                    _pose_model.save(MODEL_PATH)
                except Exception as se:
                    logger.warning("[Pose] Could not save model to destination: %s", se)

            device = "cuda" if torch.cuda.is_available() else "cpu"
            _pose_model.to(device)
            logger.info("[Pose] YOLOv8-pose loaded successfully on device=%s", device)
        except Exception as exc:
            logger.exception("[Pose] Failed to load YOLOv8-pose: %s", exc)
            _pose_model = None
    return _pose_model


def check_is_sleeping(keypoints: np.ndarray) -> bool:
    """
    Keypointlar asosida inson uxlayotgan yoki yo'qligini aniqlaydi.
    keypoints: [17, 3] o'lchamli massiv (x, y, confidence)
    """
    try:
        # 5: chap yelka (left shoulder), 6: o'ng yelka (right shoulder)
        ls = keypoints[5]
        rs = keypoints[6]
        
        # Har ikkala yelka ham kamida 40% ishonchlilik bilan ko'ringan bo'lishi kerak
        if ls[2] < 0.4 or rs[2] < 0.4:
            return False
            
        y_shoulders = (ls[1] + rs[1]) / 2
        shoulder_width = np.sqrt((ls[0] - rs[0])**2 + (ls[1] - rs[1])**2)
        
        # Agar odam juda uzoqda bo'lsa yoki noto'g'ri o'lchov bo'lsa
        if shoulder_width < 15:
            return False
            
        # Boshning yuqori qismidagi nuqtalar (0: burun, 1: chap ko'z, 2: o'ng ko'z, 3: chap quloq, 4: o'ng quloq)
        head_ys = []
        for idx in range(5):
            if keypoints[idx][2] > 0.4:
                head_ys.append(keypoints[idx][1])
                
        if not head_ys:
            # Agar bosh umuman ko'rinmasa, lekin yelkalar pastga engashgan bo'lsa, 
            # bu ham uxlash belgisi bo'lishi mumkin. Ammo false positive kamaytirish uchun False qaytaramiz.
            return False
            
        y_head = np.mean(head_ys)
        
        # Bosh va yelkalar orasidagi vertikal masofa
        # Eslatma: Rasm koordinatasida y yuqoridan pastga qarab o'sadi.
        # Shuning uchun bosh tepada bo'lsa y_head kichik, yelkalar pastda bo'lsa y_shoulders katta bo'ladi.
        head_height = y_shoulders - y_head
        
        # Odamning masofasiga bog'liq bo'lmasligi uchun yelka kengligiga bo'lib nisbatini olamiz
        ratio = head_height / shoulder_width
        
        # Normal holatda nisbat 0.4 - 0.7 atrofida bo'ladi.
        # Agar nisbat 0.20 dan kichik bo'lsa (yoki manfiy, ya'ni bosh yelkadan pastda),
        # demak, talabaning boshi egilgan yoki partaga yotib olgan (uxlamoqda).
        if ratio < 0.20:
            return True
            
        return False
    except Exception as e:
        logger.error("[Pose] Error in check_is_sleeping: %s", e)
        return False


def detect_sleeping_students(frame: np.ndarray) -> list[dict]:
    """
    Kadrda uxlayotgan talabalarni aniqlaydi.
    Qaytaradi: [{'bbox': [x1, y1, x2, y2], 'confidence': float, 'is_sleeping': bool, 'keypoints': ndarray}]
    """
    model = get_pose_model()
    if model is None:
        return []
        
    results_list = []
    try:
        # Batch inference (bitta kadr uchun)
        # verbose=False orqali ortiqcha printlarni o'chiramiz
        results = model(frame, verbose=False)
        
        for r in results:
            if r.boxes is None or r.keypoints is None:
                continue
                
            boxes = r.boxes.xyxy.cpu().numpy()
            confidences = r.boxes.conf.cpu().numpy()
            
            # Keypointlar: [N, 17, 3] yoki [N, 17, 2] bo'lishi mumkin
            # Bizga [N, 17, 3] (x, y, conf) kerak
            if hasattr(r.keypoints, 'data'):
                kpts = r.keypoints.data.cpu().numpy()
            else:
                kpts = r.keypoints.xy.cpu().numpy()
                
            for idx in range(len(boxes)):
                bbox = boxes[idx].tolist()
                conf = float(confidences[idx])
                
                # Agar odam ekanligiga ishonch past bo'lsa tashlab ketamiz
                if conf < 0.5:
                    continue
                    
                keypoint = kpts[idx]
                
                # Keypoint formatini tekshirish [17, 3]
                if keypoint.shape[1] == 2:
                    # Agar confidence bo'lmasa, unga dummy 1.0 beramiz
                    dummy_conf = np.ones((keypoint.shape[0], 1))
                    keypoint = np.hstack([keypoint, dummy_conf])
                
                is_sleeping = check_is_sleeping(keypoint)
                
                # Integration hook: Analyze Eye Aspect Ratio (EAR) and Head Pose
                yaw, pitch, roll = 0.0, 0.0, 0.0
                ear = 0.30
                is_eye_closed = False
                yaw_deviation = False
                inattentive = False
                
                try:
                    from camera.ear_pose import analyze_student_pose
                    h, w = frame.shape[:2]
                    pose_analysis = analyze_student_pose(keypoint, img_size=(w, h))
                    yaw = pose_analysis["yaw"]
                    pitch = pose_analysis["pitch"]
                    roll = pose_analysis["roll"]
                    ear = pose_analysis["ear"]
                    is_eye_closed = pose_analysis["is_eye_closed"]
                    yaw_deviation = pose_analysis["yaw_deviation"]
                    inattentive = pose_analysis["inattentive"]
                    
                    # Robustly update sleeping status if eyes are closed and head pitch is leaning down
                    if is_eye_closed and pitch < 0.0:
                        is_sleeping = True
                except Exception as exc:
                    logger.warning("[Pose] EAR/Head Pose integration hook failed: %s", exc)
                
                results_list.append({
                    'bbox': bbox,
                    'confidence': conf,
                    'is_sleeping': is_sleeping,
                    'keypoints': keypoint,
                    'yaw': yaw,
                    'pitch': pitch,
                    'roll': roll,
                    'ear': ear,
                    'is_eye_closed': is_eye_closed,
                    'yaw_deviation': yaw_deviation,
                    'inattentive': inattentive
                })
    except Exception as exc:
        logger.error("[Pose] Sleeping detection failed: %s", exc)
        
    return results_list
