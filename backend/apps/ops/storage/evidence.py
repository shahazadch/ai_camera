"""Object storage backends for evidence (local / S3 / MinIO)."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class StoredObject:
    key: str
    url: str
    checksum: str
    size_bytes: int
    content_type: str
    width: int | None = None
    height: int | None = None
    backend: str = "local"


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _image_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            return img.width, img.height
    except Exception:
        return None, None


class EvidenceStorage:
    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> StoredObject:
        raise NotImplementedError

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalEvidenceStorage(EvidenceStorage):
    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> StoredObject:
        path = Path(settings.MEDIA_ROOT) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        width, height = _image_size(data)
        base = settings.MEDIA_URL if settings.MEDIA_URL.endswith("/") else settings.MEDIA_URL + "/"
        return StoredObject(
            key=key,
            url=urljoin(base, key),
            checksum=_checksum(data),
            size_bytes=len(data),
            content_type=content_type,
            width=width,
            height=height,
            backend="local",
        )

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        base = settings.MEDIA_URL if settings.MEDIA_URL.endswith("/") else settings.MEDIA_URL + "/"
        return urljoin(base, key)

    def delete(self, key: str) -> None:
        root = Path(settings.MEDIA_ROOT).resolve()
        path = (root / key).resolve()
        if root in path.parents:
            path.unlink(missing_ok=True)


class S3EvidenceStorage(EvidenceStorage):
    def __init__(self):
        import boto3
        from botocore.client import Config

        kwargs = {
            "service_name": "s3",
            "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
            "region_name": settings.AWS_S3_REGION_NAME,
            "config": Config(signature_version=settings.AWS_S3_SIGNATURE_VERSION),
        }
        if settings.AWS_S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
        self.client = boto3.client(**kwargs)
        self.bucket = settings.AWS_STORAGE_BUCKET_NAME
        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as exc:
                logger.warning("Could not ensure evidence bucket: %s", exc)

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg") -> StoredObject:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        width, height = _image_size(data)
        return StoredObject(
            key=key,
            url=self.signed_url(key),
            checksum=_checksum(data),
            size_bytes=len(data),
            content_type=content_type,
            width=width,
            height=height,
            backend="s3" if not settings.AWS_S3_ENDPOINT_URL else "minio",
        )

    def signed_url(self, key: str, ttl_seconds: int | None = None) -> str:
        ttl = ttl_seconds or settings.EVIDENCE_SIGNED_URL_TTL_SECONDS
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_evidence_storage() -> EvidenceStorage:
    backend = settings.EVIDENCE_STORAGE_BACKEND
    if backend in {"s3", "minio"}:
        try:
            return S3EvidenceStorage()
        except Exception as exc:
            logger.warning("S3/MinIO unavailable (%s); falling back to local storage", exc)
    return LocalEvidenceStorage()


def put_file_obj(key: str, file_obj: BinaryIO, content_type: str = "image/jpeg") -> StoredObject:
    data = file_obj.read()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return get_evidence_storage().put(key, data, content_type=content_type)
