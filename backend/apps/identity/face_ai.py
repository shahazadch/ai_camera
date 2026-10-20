"""Real local face AI — YuNet detect + OpenCV SFace (FaceRecognizerSF)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
YUNET_PATH = WEIGHTS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_CANDIDATES = (
    WEIGHTS_DIR / "face_recognition_sface_2021dec_v2.onnx",
    WEIGHTS_DIR / "face_recognition_sface_2021dec.onnx",
)

# ArcFace / SFace 112x112 reference landmarks (fallback align only)
_SFACE_REF = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


@dataclass
class DetectedFace:
    bbox: tuple[int, int, int, int]
    landmarks: np.ndarray
    score: float
    crop_jpeg: bytes
    feature: list[float]


def _sface_path() -> Path | None:
    for path in SFACE_CANDIDATES:
        if path.is_file() and path.stat().st_size > 30_000_000:
            return path
    return None


# Monitor thread and request threads share the cached YuNet/SFace objects;
# setInputSize + detect must not interleave between threads.
_model_lock = threading.RLock()


@lru_cache(maxsize=1)
def models_available() -> bool:
    """True only when YuNet + SFace can actually load via OpenCV (checked once)."""
    if not YUNET_PATH.is_file() or _sface_path() is None:
        return False
    try:
        _detector()
        _recognizer()
        return True
    except Exception:
        return False


@lru_cache(maxsize=1)
def _detector():
    import cv2

    if not YUNET_PATH.is_file():
        raise FileNotFoundError(f"YuNet model missing: {YUNET_PATH}")
    return cv2.FaceDetectorYN.create(str(YUNET_PATH), "", (320, 320), 0.55, 0.3, 5000)


@lru_cache(maxsize=1)
def _recognizer():
    """OpenCV FaceRecognizerSF — no onnxruntime DLL required."""
    import cv2

    path = _sface_path()
    if path is None:
        raise FileNotFoundError("SFace ONNX model missing")
    return cv2.FaceRecognizerSF.create(str(path), "")


def _encode_jpeg(image, quality: int = 92) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("Could not encode face crop.")
    return encoded.tobytes()


def _blur_score(gray) -> float:
    import cv2

    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def detect_faces(image_bgr, *, max_faces: int = 5, score_threshold: float = 0.55) -> list:
    if image_bgr is None or image_bgr.size == 0:
        return []
    height, width = image_bgr.shape[:2]
    detector = _detector()
    with _model_lock:
        detector.setInputSize((width, height))
        detector.setScoreThreshold(float(score_threshold))
        _retval, faces = detector.detect(image_bgr)
    if faces is None or len(faces) == 0:
        return []
    ranked = sorted(faces, key=lambda row: float(row[-1]), reverse=True)
    return ranked[:max_faces]


def _align_face(image_bgr, face_row) -> np.ndarray:
    """Align face to 112x112 — prefer OpenCV SFace alignCrop."""
    import cv2

    try:
        recognizer = _recognizer()
        with _model_lock:
            aligned = recognizer.alignCrop(image_bgr, face_row)
        if aligned is not None and getattr(aligned, "size", 0) > 0:
            return aligned
    except Exception:
        pass

    landmarks = np.array(face_row[4:14], dtype=np.float32).reshape(5, 2)
    matrix, _ = cv2.estimateAffinePartial2D(landmarks, _SFACE_REF, method=cv2.LMEDS)
    if matrix is None:
        x, y, w, h = [int(round(v)) for v in face_row[:4]]
        pad = int(0.2 * max(w, h))
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2 = min(image_bgr.shape[1], x + w + pad)
        y2 = min(image_bgr.shape[0], y + h + pad)
        crop = image_bgr[y1:y2, x1:x2]
        if crop.size == 0:
            crop = image_bgr
        return cv2.resize(crop, (112, 112), interpolation=cv2.INTER_AREA)
    return cv2.warpAffine(image_bgr, matrix, (112, 112), flags=cv2.INTER_LINEAR)


def _embed_aligned(aligned_bgr: np.ndarray) -> list[float]:
    import cv2

    recognizer = _recognizer()
    with _model_lock:
        feat = recognizer.feature(aligned_bgr)
    feat = np.asarray(feat).reshape(-1).astype(np.float32)
    norm = float(np.linalg.norm(feat)) or 1.0
    return (feat / norm).tolist()


def feature_from_face_row(image_bgr, face_row) -> tuple[list[float], bytes, tuple[int, int, int, int]]:
    aligned = _align_face(image_bgr, face_row)
    feature = _embed_aligned(aligned)
    x, y, w, h = [int(round(v)) for v in face_row[:4]]
    x, y = max(0, x), max(0, y)
    return feature, _encode_jpeg(aligned), (x, y, max(1, w), max(1, h))


def scan_and_embed(
    image_bytes: bytes,
    *,
    require_face: bool = True,
    score_threshold: float = 0.45,
    fallback_thresholds: tuple[float, ...] = (),
) -> DetectedFace:
    """Detect once at the softest threshold, then prefer the strictest tier with a face."""
    import cv2

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Could not decode image.")

    tiers = (float(score_threshold), *(float(t) for t in fallback_thresholds))
    all_faces = detect_faces(image, max_faces=8, score_threshold=min(tiers))
    faces = []
    for tier in tiers:
        faces = [row for row in all_faces if float(row[-1]) >= tier]
        if faces:
            break
    if not faces:
        if require_face:
            raise ValueError("No face detected. Upload a clear front-facing photo.")
        raise ValueError("No face in frame.")

    height, width = image.shape[:2]
    best = None
    best_rank = -1.0
    for row in faces:
        x, y, w, h = [float(v) for v in row[:4]]
        conf = float(row[-1])
        area = max(1.0, w * h)
        cy = y + h / 2.0
        upper_bonus = 1.2 if cy < height * 0.65 else 0.8
        size_bonus = min(2.0, area / max(1.0, width * height * 0.015))
        rank = conf * upper_bonus * (1.0 + 0.4 * size_bonus)
        if rank > best_rank:
            best_rank = rank
            best = row

    feature, crop_jpeg, bbox = feature_from_face_row(image, best)
    return DetectedFace(
        bbox=bbox,
        landmarks=np.asarray(best, dtype=np.float32),
        score=float(best[-1]),
        crop_jpeg=crop_jpeg,
        feature=feature,
    )


def embed_image_bytes(image_bytes: bytes) -> list[float]:
    """
    Embed a face image. Enrollment crops are often already 112x112 aligned —
    embed those directly so we do not re-detect and drift the vector.
    """
    import cv2

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Could not decode image.")
    height, width = image.shape[:2]
    if abs(width - 112) <= 16 and abs(height - 112) <= 16:
        return _embed_aligned(image)
    return scan_and_embed(image_bytes, require_face=True).feature


def quality_from_crop(crop_jpeg: bytes) -> tuple[float, float]:
    import cv2

    array = np.frombuffer(crop_jpeg, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return 0.4, 0.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur = _blur_score(gray)
    quality = min(0.99, 0.55 + min(blur, 300) / 600)
    return float(quality), float(blur)
