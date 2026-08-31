import os
import logging
from typing import List, Tuple, Optional, Dict, Any
import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models

logger = logging.getLogger(__name__)

class QdrantVectorStore:
    """
    Qdrant client vector search class for Face Recognition.
    """
    def __init__(self):
        self.host = os.getenv("QDRANT_HOST", "localhost")
        self.port = int(os.getenv("QDRANT_PORT", "6333"))
        self.api_key = os.getenv("QDRANT_API_KEY", None)
        self.url = os.getenv("QDRANT_URL", None)
        self.collection_name = os.getenv("QDRANT_COLLECTION", "face_recognition")
        self.vector_size = 512
        
        # Determine client type
        if self.host == ":memory:":
            self.client = QdrantClient(":memory:")
            logger.info("[QDRANT] Initialized in-memory QdrantClient")
        elif self.url:
            self.client = QdrantClient(url=self.url, api_key=self.api_key)
            logger.info(f"[QDRANT] Initialized QdrantClient with URL: {self.url}")
        else:
            self.client = QdrantClient(host=self.host, port=self.port, api_key=self.api_key)
            logger.info(f"[QDRANT] Initialized QdrantClient with host={self.host}, port={self.port}")
            
        self._initialized = False

    def validate_collection(self) -> bool:
        """
        Validates if the collection exists and has size=512 and cosine distance.
        If it doesn't exist, it creates it.
        Returns True if successful, False otherwise.
        """
        try:
            exists = self.client.collection_exists(self.collection_name)
            if not exists:
                logger.info(f"[QDRANT] Collection '{self.collection_name}' does not exist. Creating it.")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_size,
                        distance=models.Distance.COSINE
                    )
                )
                self._initialized = True
                return True
            
            # Collection exists, check configuration
            info = self.client.get_collection(self.collection_name)
            config = info.config.params.vectors
            
            size = None
            distance = None
            if isinstance(config, models.VectorParams):
                size = config.size
                distance = config.distance
            elif isinstance(config, dict):
                # Dict of named vectors
                first_val = next(iter(config.values()))
                size = getattr(first_val, 'size', None)
                distance = getattr(first_val, 'distance', None)
            else:
                size = getattr(config, 'size', None)
                distance = getattr(config, 'distance', None)
            
            if size != self.vector_size or distance != models.Distance.COSINE:
                logger.warning(
                    f"[QDRANT] Collection '{self.collection_name}' configuration mismatch: "
                    f"size={size} (expected {self.vector_size}), distance={distance} (expected COSINE). "
                    f"Re-creating collection."
                )
                self.client.recreate_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=self.vector_size,
                        distance=models.Distance.COSINE
                    )
                )
            
            self._initialized = True
            self.create_payload_indexes()
            return True
        except Exception as e:
            logger.error(f"[QDRANT] Failed to validate collection '{self.collection_name}': {e}")
            self._initialized = False
            return False

    def create_payload_indexes(self) -> None:
        """
        Creates index fields on payload fields for high-performance filtered searches.
        """
        if not self._initialized:
            return
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="group_id",
                field_schema=models.PayloadSchemaType.INTEGER
            )
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="faculty_id",
                field_schema=models.PayloadSchemaType.INTEGER
            )
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="education_year",
                field_schema=models.PayloadSchemaType.KEYWORD
            )
            logger.info("[QDRANT] Payload indexes successfully created/validated.")
        except Exception as e:
            logger.warning(f"[QDRANT] Failed to create payload indexes: {e}")

    def upsert(self, points: List[Tuple[int, np.ndarray, Dict[str, Any]]]) -> bool:
        """
        Upsert a list of points into the Qdrant collection.
        points is a list of tuples: (user_id, embedding, payload)
        """
        if not self._initialized and not self.validate_collection():
            logger.error("[QDRANT] Collection could not be validated/initialized for upsert.")
            return False
        
        try:
            qdrant_points = []
            for user_id, embedding, payload in points:
                # convert vector to list of floats
                if isinstance(embedding, np.ndarray):
                    vec = embedding.tolist()
                else:
                    vec = list(embedding)
                
                # Check for nan/inf and vector dimension
                if len(vec) != self.vector_size:
                    logger.warning(f"[QDRANT] Skipping point {user_id} due to invalid vector size: {len(vec)}")
                    continue
                
                qdrant_points.append(
                    models.PointStruct(
                        id=user_id,
                        vector=vec,
                        payload=payload
                    )
                )
            
            if not qdrant_points:
                return True
                
            self.client.upsert(
                collection_name=self.collection_name,
                points=qdrant_points
            )
            logger.info(f"[QDRANT] Successfully upserted {len(qdrant_points)} points to collection '{self.collection_name}'")
            return True
        except Exception as e:
            logger.error(f"[QDRANT] Failed to upsert points: {e}")
            return False

    def search(
        self, 
        embedding: np.ndarray, 
        limit: int = 1, 
        allowed_ids: Optional[List[int]] = None,
        group_id: Optional[int] = None,
        faculty_id: Optional[int] = None,
        education_year: Optional[str] = None
    ) -> List[Tuple[int, float, Dict[str, Any]]]:
        """
        Search for the closest face embedding in Qdrant with optional indexing filters.
        returns: List of tuples (user_id, score/similarity, payload)
        """
        if not self._initialized and not self.validate_collection():
            logger.error("[QDRANT] Collection could not be validated/initialized for search.")
            return []
        
        try:
            if isinstance(embedding, np.ndarray):
                vec = embedding.tolist()
            else:
                vec = list(embedding)
                
            if len(vec) != self.vector_size:
                logger.error(f"[QDRANT] Search vector has invalid dimension: {len(vec)} (expected {self.vector_size})")
                return []
                
            must_conditions = []
            if allowed_ids:
                must_conditions.append(models.HasIdCondition(has_id=allowed_ids))
            if group_id:
                must_conditions.append(models.FieldCondition(key="group_id", match=models.MatchValue(value=group_id)))
            if faculty_id:
                must_conditions.append(models.FieldCondition(key="faculty_id", match=models.MatchValue(value=faculty_id)))
            if education_year:
                must_conditions.append(models.FieldCondition(key="education_year", match=models.MatchValue(value=education_year)))

            query_filter = None
            if must_conditions:
                query_filter = models.Filter(must=must_conditions)

            res = self.client.query_points(
                collection_name=self.collection_name,
                query=vec,
                query_filter=query_filter,
                limit=limit
            )
            
            ret = []
            for point in res.points:
                ret.append((point.id, point.score, point.payload or {}))
            return ret
        except Exception as e:
            logger.error(f"[QDRANT] Failed to search Qdrant: {e}")
            return []
