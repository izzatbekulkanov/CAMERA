# camera/engagement.py
import math
import logging

logger = logging.getLogger(__name__)

class EngagementEngine:
    """
    Cognitive Engagement Engine.
    Computes a scientifically grounded engagement score [0.0 - 100.0] 
    by weighing eye opening factors, pose deviation, phone usage, and spoken participation.
    """
    def __init__(self):
        # Weights allocated for each indicator (must sum to 1.0)
        self.w_ear = 0.30
        self.w_pose = 0.25
        self.w_phone = 0.25
        self.w_participation = 0.20

    def compute_score(self, ear: float, yaw: float, phone_visible: bool, stt_words_count: int) -> float:
        """
        Calculates student engagement score based on parameters.
        - ear: Eye Aspect Ratio [0.0 - 0.4]
        - yaw: Head yaw angle in degrees [0 - 180]
        - phone_visible: Boolean indicating if a mobile phone is detected
        - stt_words_count: Number of spoken words captured via real-time STT
        """
        try:
            # 1. Eye Aspect Ratio Factor: standard open eyes range is around 0.25
            ear_factor = min(1.0, max(0.0, ear / 0.25))

            # 2. Attention Pose: Yaw deviation from center. 0 degrees is facing camera.
            # Beyond 45 degrees yaw, attention factor goes to 0
            attention_pose = max(0.0, 1.0 - abs(yaw) / 45.0)

            # 3. Phone Distraction Factor: 0.0 if distracted, 1.0 if not
            phone_factor = 0.0 if phone_visible else 1.0

            # 4. Speech Participation Factor: logarithmic scaling for speech activity
            participation_factor = min(1.0, math.log1p(stt_words_count) / 4.0)

            # Weighted sum calculation
            weighted_sum = (
                self.w_ear * ear_factor +
                self.w_pose * attention_pose +
                self.w_phone * phone_factor +
                self.w_participation * participation_factor
            )

            # Scale to 0.0 - 100.0
            return round(weighted_sum * 100.0, 1)

        except Exception as e:
            logger.error("[Engagement Engine] Calculation error: %s", e)
            return 75.0  # Safe default baseline score
