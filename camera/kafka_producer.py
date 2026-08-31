# camera/kafka_producer.py
import json
import logging
import os
import threading
from datetime import datetime, date
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.db.models import Model
from django.forms.models import model_to_dict

logger = logging.getLogger(__name__)

class CampusKafkaJSONEncoder(json.JSONEncoder):
    """
    Custom JSON encoder to seamlessly serialize date/time fields,
    decimals, and Django model instances.
    """
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, Model):
            try:
                return model_to_dict(obj)
            except Exception:
                return str(obj)
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


class CampusKafkaProducer:
    """
    Thread-safe Singleton wrapper for streaming telemetries to the Campus Kafka broker.
    Provides automatic fallback to confluent-kafka, kafka-python, or local logging if no
    broker client library is installed.
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(CampusKafkaProducer, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.bootstrap_servers = getattr(settings, 'KAFKA_BOOTSTRAP_SERVERS', os.getenv('KAFKA_BOOTSTRAP_SERVERS', 'localhost:9092'))
        self.producer = None
        self.producer_type = "dummy"

        # 1. Attempt to use confluent-kafka (highly recommended for production performance)
        try:
            from confluent_kafka import Producer
            self.producer = Producer({'bootstrap.servers': self.bootstrap_servers})
            self.producer_type = "confluent-kafka"
            logger.info("[Kafka] CampusKafkaProducer initialized successfully using confluent-kafka.")
        except ImportError:
            # 2. Attempt to fall back to kafka-python
            try:
                from kafka import KafkaProducer
                self.producer = KafkaProducer(
                    bootstrap_servers=self.bootstrap_servers
                )
                self.producer_type = "kafka-python"
                logger.info("[Kafka] CampusKafkaProducer initialized successfully using kafka-python.")
            except ImportError:
                logger.warning("[Kafka] Neither 'confluent-kafka' nor 'kafka-python' is installed. "
                               "CampusKafkaProducer will fall back to DUMMY (local logging) mode.")
                self.producer = None
                self.producer_type = "dummy"

        self._initialized = True

    def serialize(self, value) -> bytes:
        """
        Serialize any payload into a UTF-8 JSON bytes string.
        """
        if isinstance(value, bytes):
            return value
        return json.dumps(value, cls=CampusKafkaJSONEncoder, ensure_ascii=False).encode('utf-8')

    def send(self, topic: str, value, key=None, callback=None) -> bool:
        """
        Generic send method that publishes a serialized message to a Kafka topic.
        
        Args:
            topic (str): Target Kafka topic.
            value (any): Dict, Model, or raw bytes to publish.
            key (any, optional): Message routing key.
            callback (callable, optional): Invoked upon successful or failed delivery.
            
        Returns:
            bool: True if publishing succeeded or was delegated to fallback, False otherwise.
        """
        try:
            serialized_value = self.serialize(value)
        except Exception as exc:
            logger.exception("[Kafka] Serialization failed for payload: %s", exc)
            return False

        serialized_key = None
        if key is not None:
            if not isinstance(key, bytes):
                serialized_key = str(key).encode('utf-8')
            else:
                serialized_key = key

        # 1. confluent-kafka transport
        if self.producer_type == "confluent-kafka" and self.producer:
            try:
                def delivery_report(err, msg):
                    if err is not None:
                        logger.error("[Kafka] Message delivery failed in confluent-kafka: %s", err)
                    else:
                        if callback:
                            callback(msg)
                
                self.producer.produce(topic, value=serialized_value, key=serialized_key, callback=delivery_report)
                self.producer.poll(0)  # non-blocking poll to serve delivery callbacks
                return True
            except Exception as exc:
                logger.exception("[Kafka] Failed to publish message via confluent-kafka: %s", exc)
                return False

        # 2. kafka-python transport
        elif self.producer_type == "kafka-python" and self.producer:
            try:
                future = self.producer.send(topic, value=serialized_value, key=serialized_key)
                if callback:
                    def on_success(record_metadata):
                        callback(record_metadata)
                    def on_error(excp):
                        logger.error("[Kafka] Message delivery failed in kafka-python: %s", excp)
                    future.add_callback(on_success)
                    future.add_errback(on_error)
                return True
            except Exception as exc:
                logger.exception("[Kafka] Failed to publish message via kafka-python: %s", exc)
                return False

        # 3. Dummy / Fallback transport
        else:
            try:
                payload_str = serialized_value.decode('utf-8', errors='ignore')
                key_str = serialized_key.decode('utf-8', errors='ignore') if serialized_key else "None"
                logger.info(
                    "[Kafka-Dummy-Telemetry] Topic: '%s' | Key: '%s' | Payload: %s",
                    topic, key_str, payload_str
                )
                if callback:
                    callback(None)
                return True
            except Exception as exc:
                logger.error("[Kafka-Dummy-Telemetry] Failed to log dummy publish: %s", exc)
                return False

    def flush(self, timeout=None):
        """
        Wait for any outstanding messages to be delivered.
        """
        if self.producer_type == "confluent-kafka" and self.producer:
            self.producer.flush(timeout or -1)
        elif self.producer_type == "kafka-python" and self.producer:
            self.producer.flush(timeout)


def send_face_detection_event(user_id=None, camera_id=None, confidence: float = 0.0, timestamp=None, image_url: str = None, is_known: bool = True, additional_data: dict = None) -> dict:
    """
    Constructs and dispatches a telemetry payload for a face detection event.
    
    Args:
        user_id (int|CustomUser, optional): User ID or instance.
        camera_id (int|Camera, optional): Camera ID or instance.
        confidence (float): Recognition match score / confidence.
        timestamp (datetime, optional): Event occurrence time. Defaults to now.
        image_url (str, optional): Public/storage URL of cropped face snapshot.
        is_known (bool): True if recognized, False if anonymous/unknown.
        additional_data (dict, optional): Additional fields to inject into the payload.
        
    Returns:
        dict: The serialized event payload dictionary.
    """
    producer = CampusKafkaProducer()
    topic = getattr(settings, 'KAFKA_FACE_DETECTION_TOPIC', os.getenv('KAFKA_FACE_DETECTION_TOPIC', 'campus-face-detections'))

    if timestamp is None:
        timestamp = timezone.now()

    payload = {
        "event_type": "face_detection",
        "timestamp": timestamp,
        "is_known": is_known,
        "confidence": float(confidence),
        "image_url": image_url or "",
    }

    # Resolve User details
    if user_id:
        try:
            from users.models import CustomUser
            if isinstance(user_id, CustomUser):
                user = user_id
            else:
                user = CustomUser.objects.filter(pk=user_id).first()

            if user:
                payload.update({
                    "user_id": user.id,
                    "username": user.username,
                    "full_name": user.full_name or f"{user.first_name} {user.second_name}".strip(),
                    "role": user.role,
                })
            else:
                payload.update({
                    "user_id": user_id,
                    "username": None,
                    "full_name": "User Not Found",
                    "role": "unknown",
                })
        except Exception as exc:
            logger.error("[Kafka-Telemetry] Failed to resolve CustomUser details: %s", exc)
            payload.update({
                "user_id": getattr(user_id, 'id', user_id),
                "role": "unknown",
            })
    else:
        payload.update({
            "user_id": None,
            "username": None,
            "full_name": "Unknown Person",
            "role": "unknown",
        })

    # Resolve Camera details
    if camera_id:
        try:
            from camera.models import Camera
            if isinstance(camera_id, Camera):
                camera = camera_id
            else:
                camera = Camera.objects.filter(pk=camera_id).first()

            if camera:
                payload.update({
                    "camera_id": camera.id,
                    "camera_name": camera.name or camera.ip,
                    "camera_ip": camera.ip,
                })
            else:
                payload.update({
                    "camera_id": camera_id,
                    "camera_name": "Camera Not Found",
                    "camera_ip": None,
                })
        except Exception as exc:
            logger.error("[Kafka-Telemetry] Failed to resolve Camera details: %s", exc)
            payload.update({
                "camera_id": getattr(camera_id, 'id', camera_id),
            })
    else:
        payload.update({
            "camera_id": None,
            "camera_name": "Unknown Camera",
            "camera_ip": None,
        })

    if additional_data and isinstance(additional_data, dict):
        payload.update(additional_data)

    routing_key = payload.get("user_id")
    producer.send(topic=topic, value=payload, key=routing_key)
    return payload


def send_stt_dialog_event(schedule_id=None, text: str = "", is_final: bool = False, timestamp=None, user_id=None, additional_data: dict = None) -> dict:
    """
    Constructs and dispatches a telemetry payload for a speech-to-text dialog event.
    
    Args:
        schedule_id (int|LessonSchedule, optional): LessonSchedule ID or instance.
        text (str): Spoken dialog transcription.
        is_final (bool): Whether the transcript represents a finalized phrase.
        timestamp (datetime, optional): Event occurrence time. Defaults to now.
        user_id (int|CustomUser, optional): User ID of the speaker if identifiable.
        additional_data (dict, optional): Additional fields to inject into the payload.
        
    Returns:
        dict: The serialized event payload dictionary.
    """
    producer = CampusKafkaProducer()
    topic = getattr(settings, 'KAFKA_STT_DIALOG_TOPIC', os.getenv('KAFKA_STT_DIALOG_TOPIC', 'campus-stt-dialogs'))

    if timestamp is None:
        timestamp = timezone.now()

    payload = {
        "event_type": "stt_dialog",
        "timestamp": timestamp,
        "text": text,
        "is_final": is_final,
    }

    # Resolve LessonSchedule details
    if schedule_id:
        try:
            from camera.models import LessonSchedule
            if isinstance(schedule_id, LessonSchedule):
                schedule = schedule_id
            else:
                schedule = LessonSchedule.objects.filter(pk=schedule_id).first()

            if schedule:
                payload.update({
                    "schedule_id": schedule.id,
                    "subject_name": schedule.subject.name if schedule.subject else None,
                    "academic_group_name": schedule.academic_group.name if schedule.academic_group else None,
                    "teacher_name": schedule.teacher_name,
                    "weekday": schedule.weekday,
                })
            else:
                payload.update({
                    "schedule_id": schedule_id,
                    "subject_name": None,
                    "academic_group_name": None,
                    "teacher_name": None,
                })
        except Exception as exc:
            logger.error("[Kafka-Telemetry] Failed to resolve LessonSchedule details: %s", exc)
            payload.update({
                "schedule_id": getattr(schedule_id, 'id', schedule_id),
            })
    else:
        payload.update({
            "schedule_id": None,
            "subject_name": None,
            "academic_group_name": None,
            "teacher_name": None,
        })

    # Resolve speaker User details if provided
    if user_id:
        try:
            from users.models import CustomUser
            if isinstance(user_id, CustomUser):
                user = user_id
            else:
                user = CustomUser.objects.filter(pk=user_id).first()

            if user:
                payload.update({
                    "user_id": user.id,
                    "username": user.username,
                    "full_name": user.full_name or f"{user.first_name} {user.second_name}".strip(),
                })
            else:
                payload.update({
                    "user_id": user_id,
                    "username": None,
                    "full_name": "User Not Found",
                })
        except Exception as exc:
            logger.error("[Kafka-Telemetry] Failed to resolve Speaker User details: %s", exc)
            payload.update({
                "user_id": getattr(user_id, 'id', user_id),
            })
    else:
        payload.update({
            "user_id": None,
            "username": None,
            "full_name": None,
        })

    if additional_data and isinstance(additional_data, dict):
        payload.update(additional_data)

    routing_key = payload.get("schedule_id")
    producer.send(topic=topic, value=payload, key=routing_key)
    return payload
