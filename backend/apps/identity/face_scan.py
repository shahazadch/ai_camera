"""Local face scan — YuNet + SFace when available, with Haar fallback for CCTV."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np


@dataclass
class FaceScanResult:
    face_jpeg: bytes
    face_count: int
    quality_score: float
    blur_score: float
    bbox: tuple[int, int, int, int]
    used_full_image: bool = False
    feature: list[float] = field(default_factory=list)
    engine: str = "haar"
    # YuNet detection confidence; 0 for the Haar fallback (which has none).
    face_score: float = 0.0


def _blur_score(gray) -> float:
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _decode(image_bytes: bytes):
    import cv2

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Could not decode enrollment image.")
    return image


def _encode_jpeg(image, quality: int = 92) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("Could not encode face crop.")
    return encoded.tobytes()


def _sface_feature_from_crop(crop_jpeg: bytes) -> list[float]:
    """Best-effort SFace particles from an already-cropped face JPEG."""
    try:
        from .face_ai import embed_image_bytes, models_available

        if not models_available():
            return []
        feature = embed_image_bytes(crop_jpeg)
        return list(feature) if feature else []
    except Exception:
        return []


def scan_face_from_bytes(
    image_bytes: bytes,
    *,
    require_face: bool = True,
    min_size: int = 48,
) -> FaceScanResult:
    """
    Detect the clearest face, crop it, and extract identity particles when
    SFace is available.

    Live CCTV: YuNet often misses at default score — retry softer, then Haar.
    Enrollment still prefers YuNet+SFace but will use Haar if needed.
    """
    # Prefer real face AI (YuNet + SFace) with soft retries for distant faces.
    try:
        from .face_ai import models_available, quality_from_crop, scan_and_embed

        if models_available():
            try:
                detected = scan_and_embed(
                    image_bytes,
                    require_face=True,
                    score_threshold=0.45,
                    fallback_thresholds=(0.32, 0.22),
                )
                quality, blur = quality_from_crop(detected.crop_jpeg)
                return FaceScanResult(
                    face_jpeg=detected.crop_jpeg,
                    face_count=1,
                    quality_score=max(quality, float(detected.score)),
                    face_score=float(detected.score),
                    blur_score=blur,
                    bbox=detected.bbox,
                    used_full_image=False,
                    feature=detected.feature,
                    engine="sface",
                )
            except ValueError:
                # Do NOT re-raise — Haar often still finds the face on CCTV crops.
                pass
    except Exception:
        pass

    return _haar_scan(image_bytes, require_face=require_face, min_size=min_size)


# Cached cascades are shared across threads; detectMultiScale is not thread-safe.
_cascade_lock = threading.Lock()


@lru_cache(maxsize=4)
def _cascade(name: str):
    import cv2

    return cv2.CascadeClassifier(cv2.data.haarcascades + name)


def _haar_scan(
    image_bytes: bytes,
    *,
    require_face: bool = True,
    min_size: int = 48,
) -> FaceScanResult:
    import cv2

    image = _decode(image_bytes)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    face_min = max(16, int(min_size))

    with _cascade_lock:
        faces = _cascade("haarcascade_frontalface_default.xml").detectMultiScale(
            gray,
            scaleFactor=1.07,
            minNeighbors=3,
            minSize=(face_min, face_min),
            flags=cv2.CASCADE_SCALE_IMAGE,
        )
        if len(faces) == 0:
            faces = _cascade("haarcascade_profileface.xml").detectMultiScale(
                gray,
                scaleFactor=1.07,
                minNeighbors=3,
                minSize=(face_min, face_min),
            )

    height, width = image.shape[:2]

    if len(faces) == 0:
        if require_face:
            raise ValueError("No face detected. Upload a clear front-facing photo.")
        x1, y1 = int(width * 0.2), int(height * 0.08)
        x2, y2 = int(width * 0.8), int(height * 0.55)
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            crop = image
        crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        return FaceScanResult(
            face_jpeg=_encode_jpeg(crop),
            face_count=0,
            quality_score=0.55,
            blur_score=_blur_score(crop_gray),
            bbox=(x1, y1, x2 - x1, y2 - y1),
            used_full_image=True,
            engine="haar",
        )

    ranked = []
    for x, y, w, h in faces:
        pad_x = int(w * 0.28)
        pad_y = int(h * 0.35)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(width, x + w + pad_x)
        y2 = min(height, y + h + pad_y)
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = _blur_score(crop_gray)
        area = w * h
        cy = y + h / 2
        upper = 1.2 if cy < height * 0.65 else 0.8
        ranked.append((area * upper * (0.4 + min(blur, 400) / 400), crop, blur, (x1, y1, x2 - x1, y2 - y1)))

    if not ranked:
        raise ValueError("Face detected but crop failed. Try another photo.")

    ranked.sort(key=lambda item: item[0], reverse=True)
    _, best_crop, blur, bbox = ranked[0]

    side = max(160, min(320, max(best_crop.shape[:2])))
    best_crop = cv2.resize(best_crop, (side, side), interpolation=cv2.INTER_AREA)
    face_jpeg = _encode_jpeg(best_crop)

    face_area_ratio = (bbox[2] * bbox[3]) / max(1, width * height)
    quality = min(0.98, 0.45 + face_area_ratio * 1.4 + min(blur, 250) / 700)

    # Try to attach real SFace particles even when detection used Haar.
    feature = _sface_feature_from_crop(face_jpeg)
    engine = "haar+sface" if feature else "haar"

    return FaceScanResult(
        face_jpeg=face_jpeg,
        face_count=int(len(faces)),
        quality_score=float(quality),
        blur_score=float(blur),
        bbox=(int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])),
        used_full_image=False,
        feature=feature,
        engine=engine,
    )
