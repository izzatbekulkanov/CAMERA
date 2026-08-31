# camera/ear_pose.py
import logging
import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Standard 3D facial model points for 5 keypoints (COCO keypoint index order: Nose, L Eye, R Eye, L Ear, R Ear)
# In standard 3D coordinate system: +X points right (person's right), +Y points up, +Z points out of the face.
# Left Eye (person's left / image right) -> Negative X
# Right Eye (person's right / image left) -> Positive X
FACE_3D_MODEL_5 = np.array([
    [0.0, 0.0, 0.0],          # 0: Nose tip
    [-2.5, 2.0, -2.0],        # 1: Left Eye
    [2.5, 2.0, -2.0],         # 2: Right Eye
    [-6.0, -1.0, -6.0],       # 3: Left Ear
    [6.0, -1.0, -6.0]         # 4: Right Ear
], dtype=np.float32)


def calculate_ear(eye_points: np.ndarray) -> float:
    """
    Computes the Eye Aspect Ratio (EAR) using standard 6 facial landmarks.
    Formula:
        EAR = (||p2 - p6|| + ||p3 - p5||) / (2.0 * ||p1 - p4||)
    
    Args:
        eye_points (np.ndarray): Shape (6, 2) or (6, 3) representing 2D/3D landmarks.
                                 Ordered as: p1 (inner corner), p2, p3, p4 (outer corner), p5, p6
    Returns:
        float: Eye Aspect Ratio. Returns 0.0 if horizontal distance is zero.
    """
    if eye_points.shape[0] != 6:
        raise ValueError("EAR calculation requires exactly 6 landmark points.")
        
    p1, p2, p3, p4, p5, p6 = eye_points
    
    # Vertical distances between upper and lower eyelids
    d_vertical_1 = np.linalg.norm(p2 - p6)
    d_vertical_2 = np.linalg.norm(p3 - p5)
    
    # Horizontal distance between outer and inner eye corners
    d_horizontal = np.linalg.norm(p1 - p4)
    
    if d_horizontal < 1e-6:
        return 0.0
        
    return (d_vertical_1 + d_vertical_2) / (2.0 * d_horizontal)


def calculate_average_ear(left_eye: np.ndarray, right_eye: np.ndarray) -> float:
    """
    Computes the average Eye Aspect Ratio (EAR) for both eyes.
    Each input must have shape (6, 2) or (6, 3).
    """
    ear_left = calculate_ear(left_eye)
    ear_right = calculate_ear(right_eye)
    return (ear_left + ear_right) / 2.0


def is_eye_closed(ear: float, threshold: float = 0.22) -> bool:
    """
    Determines if the eye is closed based on the EAR value and a threshold.
    """
    return ear < threshold


def estimate_head_pose_pnp(keypoints: np.ndarray, img_size: tuple[int, int] = (640, 480)) -> tuple[float, float, float]:
    """
    Estimates head pose (yaw, pitch, roll) in degrees using the standard PnP solver with 5 COCO landmarks.
    
    Args:
        keypoints (np.ndarray): Shape (17, 3) or (5, 3) or (5, 2) from YOLOv8-pose.
        img_size (tuple[int, int]): Frame dimensions (width, height).
    Returns:
        tuple[float, float, float]: (yaw, pitch, roll) in degrees.
    """
    if keypoints.shape[0] < 5:
        raise ValueError("Head pose estimation requires at least 5 facial keypoints.")
        
    # Extract Nose, L Eye, R Eye, L Ear, R Ear
    image_points = keypoints[0:5, :2].astype(np.float32)
    
    w, h = img_size
    focal_length = max(w, h)
    center = (w / 2.0, h / 2.0)
    
    # Construct intrinsic camera matrix
    camera_matrix = np.array([
        [focal_length, 0.0, center[0]],
        [0.0, focal_length, center[1]],
        [0.0, 0.0, 1.0]
    ], dtype=np.float32)
    
    dist_coeffs = np.zeros((4, 1), dtype=np.float32)  # Assuming no lens distortion
    
    # Perspective-n-Point solver
    success, rvec, tvec = cv2.solvePnP(
        FACE_3D_MODEL_5,
        image_points,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_EPNP
    )
    
    if not success:
        raise RuntimeError("solvePnP failed to converge.")
        
    # Rodrigues transformation to rotation matrix
    rmat, _ = cv2.Rodrigues(rvec)
    
    # Compute Euler angles (Yaw, Pitch, Roll)
    sy = np.sqrt(rmat[0, 0]**2 + rmat[1, 0]**2)
    singular = sy < 1e-6
    if not singular:
        pitch = np.arctan2(rmat[2, 1], rmat[2, 2])
        yaw = np.arctan2(-rmat[2, 0], sy)
        roll = np.arctan2(rmat[1, 0], rmat[0, 0])
    else:
        pitch = np.arctan2(-rmat[1, 2], rmat[1, 1])
        yaw = np.arctan2(-rmat[2, 0], sy)
        roll = 0.0
        
    return float(np.degrees(yaw)), float(np.degrees(pitch)), float(np.degrees(roll))


def estimate_head_pose_geometric(keypoints: np.ndarray) -> tuple[float, float, float]:
    """
    A robust pure NumPy geometric approximation of head pose (yaw, pitch, roll) in degrees.
    Acts as a highly reliable fallback for when solvePnP fails.
    
    Args:
        keypoints (np.ndarray): Shape (17, 3) or (5, 3) from YOLOv8-pose.
    Returns:
        tuple[float, float, float]: (yaw, pitch, roll) in degrees.
    """
    nose = keypoints[0, :2]
    left_eye = keypoints[1, :2]
    right_eye = keypoints[2, :2]
    left_ear = keypoints[3, :2]
    right_ear = keypoints[4, :2]
    
    # 1. ROLL (Z-axis rotation)
    # Estimated directly via eye horizontal and vertical differences
    eye_dx = right_eye[0] - left_eye[0]
    eye_dy = right_eye[1] - left_eye[1]  # Y axis increases downwards
    roll = np.degrees(np.arctan2(eye_dy, eye_dx + 1e-6))
    
    # 2. YAW (Y-axis rotation)
    # Analyzes horizontal asymmetry between nose position and the eye midpoint
    eye_mid = (left_eye + right_eye) / 2.0
    eye_dist = np.linalg.norm(right_eye - left_eye)
    
    if eye_dist > 1e-6:
        # Scale-invariant horizontal offset
        yaw_ratio = (nose[0] - eye_mid[0]) / eye_dist
        yaw = float(np.clip(yaw_ratio * 90.0, -90.0, 90.0))
    else:
        yaw = 0.0
        
    # Refine yaw using ear distances if both are highly confident
    if keypoints[3, 2] > 0.4 and keypoints[4, 2] > 0.4:
        ear_mid = (left_ear + right_ear) / 2.0
        ear_dist = np.linalg.norm(right_ear - left_ear)
        if ear_dist > 1e-6:
            ear_yaw_ratio = (nose[0] - ear_mid[0]) / ear_dist
            ear_yaw = float(np.clip(ear_yaw_ratio * 90.0, -90.0, 90.0))
            # Weighted blending
            yaw = 0.5 * yaw + 0.5 * ear_yaw

    # 3. PITCH (X-axis rotation)
    # Analyzes the scale-invariant vertical displacement of the nose relative to the eyes
    if eye_dist > 1e-6:
        nose_y_offset = nose[1] - eye_mid[1]
        pitch_ratio = nose_y_offset / eye_dist
        # Normalized standard frontal pitch ratio is typically around 0.35.
        pitch = float(np.clip((pitch_ratio - 0.35) * 100.0, -90.0, 90.0))
    else:
        pitch = 0.0
        
    return yaw, pitch, roll


def estimate_head_pose(keypoints: np.ndarray, img_size: tuple[int, int] = (640, 480)) -> tuple[float, float, float]:
    """
    Unified head pose estimation with robust error fallback.
    Tries solvePnP first; falls back to pure NumPy geometric estimation on exception.
    """
    try:
        return estimate_head_pose_pnp(keypoints, img_size)
    except Exception as exc:
        logger.debug("[EAR-POSE] PnP Head Pose estimation failed, falling back to geometric: %s", exc)
        return estimate_head_pose_geometric(keypoints)


def estimate_ear_from_pose(keypoints: np.ndarray) -> float:
    """
    Estimates a proxy Eye Aspect Ratio (EAR) based on YOLOv8 keypoint confidences and geometry.
    
    Args:
        keypoints (np.ndarray): Shape (17, 3) from YOLOv8-pose.
    Returns:
        float: Estimated EAR value between 0.10 and 0.40.
    """
    # Extract eye keypoint confidences
    left_eye_conf = keypoints[1, 2]
    right_eye_conf = keypoints[2, 2]
    avg_eye_conf = (left_eye_conf + right_eye_conf) / 2.0
    
    # Calculate head pitch
    _, pitch, _ = estimate_head_pose_geometric(keypoints)
    
    # Establish base EAR value (around 0.30 is open, < 0.22 is closed)
    if avg_eye_conf < 0.4:
        # Lower confidence correlates to closed or heavily occluded eyes
        base_ear = 0.14 + (avg_eye_conf * 0.20)
    else:
        # Standard open eyes
        base_ear = 0.22 + (avg_eye_conf - 0.4) * 0.20
        
    # Sleep state or extreme downward tilt reduces the EAR proxy
    if pitch < -18.0:
        tilt_factor = np.clip((pitch + 18.0) / -30.0, 0.0, 1.0)
        base_ear -= tilt_factor * 0.08
        
    return float(np.clip(base_ear, 0.10, 0.40))


def analyze_student_pose(
    keypoints: np.ndarray,
    img_size: tuple[int, int] = (640, 480),
    ear_threshold: float = 0.21,
    yaw_threshold: float = 28.0
) -> dict:
    """
    Performs high-level student pose, eye-closure, and attention analysis.
    
    Args:
        keypoints (np.ndarray): Shape (17, 3) from YOLOv8-pose.
        img_size (tuple[int, int]): Size of the frame for PnP camera matrix mapping.
        ear_threshold (float): Threshold below which eyes are considered closed.
        yaw_threshold (float): Absolute yaw angle above which yaw is considered deviant.
    Returns:
        dict: Analyzed metrics including yaw, pitch, roll, EAR, and state boolean flags.
    """
    yaw, pitch, roll = estimate_head_pose(keypoints, img_size)
    ear = estimate_ear_from_pose(keypoints)
    
    is_closed = is_eye_closed(ear, ear_threshold)
    yaw_dev = abs(yaw) > yaw_threshold
    
    # Inattentive if either eyes are closed or looking away significantly
    inattentive = is_closed or yaw_dev
    
    return {
        "yaw": round(yaw, 2),
        "pitch": round(pitch, 2),
        "roll": round(roll, 2),
        "ear": round(ear, 3),
        "is_eye_closed": is_closed,
        "yaw_deviation": yaw_dev,
        "inattentive": inattentive
    }
