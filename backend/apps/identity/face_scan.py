"""Local face scan — the active face AI engine only (InsightFace or YuNet+SFace, see face_ai)."""

from __future__ import annotations

from dataclasses import dataclass, field

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
    # "insightface" / "sface" for a real AI face; "none" when no face was found.
    engine: str = "none"
    # Face detector confidence (0 when there is no face).
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


def scan_face_from_bytes(
    image_bytes: bytes,
    *,
    require_face: bool = True,
    min_size: int = 48,
) -> FaceScanResult:
    """
    Detect the clearest face, crop/align it and extract its identity vector.

    Only the AI face detector is used. The old Haar-cascade fallback "found"
    faces in fur, leaves and walls (cows became unknown people), so when the AI
    sees no face we say so: raise when a face is required, otherwise return
    the upper-centre of the image flagged ``used_full_image``.
    ``min_size`` is kept for callers; the AI detector handles scale itself.
    """
    from .face_ai import engine, models_available, quality_from_crop, scan_and_embed

    if models_available():
        try:
            detected = scan_and_embed(
                image_bytes,
                require_face=True,
                score_threshold=0.45,
                fallback_thresholds=(0.32,),
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
                engine=engine(),
            )
        except ValueError:
            pass

    if require_face:
        raise ValueError("No face detected. Upload a clear front-facing photo.")

    import cv2

    image = _decode(image_bytes)
    height, width = image.shape[:2]
    x1, y1 = int(width * 0.2), int(height * 0.08)
    x2, y2 = int(width * 0.8), int(height * 0.55)
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        crop = image
    return FaceScanResult(
        face_jpeg=_encode_jpeg(crop),
        face_count=0,
        quality_score=0.55,
        blur_score=_blur_score(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)),
        bbox=(x1, y1, x2 - x1, y2 - y1),
        used_full_image=True,
    )
