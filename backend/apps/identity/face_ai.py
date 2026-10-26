"""
Local face AI with two engines (FACE_MODEL env):

- ``insightface`` (default when its models are present): SCRFD-10G detector +
  ArcFace R50 (WebFace600K) on ONNX Runtime — the most accurate free set, but
  the InsightFace weights are licensed for NON-COMMERCIAL use only.
- ``sface``: YuNet (MIT) + OpenCV SFace (Apache-2.0) — commercial-safe.

Both produce faces as YuNet-style rows ``[x, y, w, h, 5 landmark pairs, score]``
so callers don't care which engine is active. Switching engines changes the
embedding model; saved people are re-embedded automatically from their stored
enrollment crops (see identity.services.refresh_person_appearance_embeddings).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"
INSIGHTFACE_DIR = Path(os.getenv("MODELS_DIR", "/opt/models")) / "insightface"
SCRFD_PATH = INSIGHTFACE_DIR / "det_10g.onnx"
ARCFACE_PATH = INSIGHTFACE_DIR / "w600k_r50.onnx"
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
def engine() -> str:
    """Active face engine: "insightface" or "sface" (decided once per process)."""
    wanted = os.getenv("FACE_MODEL", "insightface").lower()
    if wanted == "insightface" and SCRFD_PATH.is_file() and ARCFACE_PATH.is_file():
        try:
            import onnxruntime  # noqa: F401

            return "insightface"
        except ImportError:
            pass
    return "sface"


# Per-engine identity: stored vectors are tagged with the model name/version so
# a switch re-embeds the gallery instead of comparing incompatible vectors.
ENGINE_INFO = {
    # threshold: cosine match for a saved person. clear_face: detector score a
    # face needs before a stranger is reported UNKNOWN (SCRFD scores real CCTV
    # faces lower than YuNet does, but makes far fewer fake faces).
    "insightface": {
        # 0.36 sits between the worst same-person (0.39) and best other-person
        # (0.34) score measured on this site's enrolled people.
        "model": "insightface-arcface-r50", "version": "1.0.0", "dim": 512, "threshold": 0.36, "clear_face": 0.5,
    },
    "sface": {"model": "opencv-sface", "version": "3.2.0", "dim": 128, "threshold": 0.32, "clear_face": 0.6},
}


def engine_info() -> dict:
    return ENGINE_INFO[engine()]


@lru_cache(maxsize=1)
def models_available() -> bool:
    """True only when the active engine's models actually load (checked once)."""
    try:
        if engine() == "insightface":
            _scrfd()
            _arcface()
            return True
        if not YUNET_PATH.is_file() or _sface_path() is None:
            return False
        _detector()
        _recognizer()
        return True
    except Exception:
        return False


def _onnx_session(path: Path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = int(os.getenv("OPENCV_THREADS", "2"))
    # SCRFD was exported for 640px; running it at 480 is valid but makes ONNX
    # Runtime warn about output sizes on every call. Show errors only.
    options.log_severity_level = 3
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class _SCRFD:
    """SCRFD face detector decoder (strides 8/16/32, 2 anchors per cell, 5 landmarks)."""

    STRIDES = (8, 16, 32)

    # 480 finds as many faces as 640 on site footage at half the time.
    def __init__(self, path: Path, size: int = 480):
        self.session = _onnx_session(path)
        self.input = self.session.get_inputs()[0].name
        self.size = size

    def detect(self, image_bgr, threshold: float) -> list:
        import cv2

        height, width = image_bgr.shape[:2]
        scale = self.size / max(height, width)
        new_h, new_w = max(1, int(round(height * scale))), max(1, int(round(width * scale)))
        canvas = np.zeros((self.size, self.size, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = cv2.resize(image_bgr, (new_w, new_h))
        blob = cv2.dnn.blobFromImage(
            canvas, 1 / 128.0, (self.size, self.size), (127.5, 127.5, 127.5), swapRB=True
        )
        outputs = self.session.run(None, {self.input: blob})
        levels = len(self.STRIDES)
        rows = []
        for i, stride in enumerate(self.STRIDES):
            scores = outputs[i].reshape(-1)
            boxes = outputs[i + levels].reshape(-1, 4) * stride
            points = outputs[i + 2 * levels].reshape(-1, 10) * stride
            cells = self.size // stride
            grid = np.stack(np.mgrid[:cells, :cells][::-1], axis=-1).reshape(-1, 2).astype(np.float32) * stride
            centers = np.repeat(grid, 2, axis=0)
            for k in np.where(scores >= threshold)[0]:
                cx, cy = centers[k]
                x1, y1 = cx - boxes[k][0], cy - boxes[k][1]
                x2, y2 = cx + boxes[k][2], cy + boxes[k][3]
                landmarks = (points[k].reshape(5, 2) + centers[k]) / scale
                rows.append(
                    np.array(
                        [x1 / scale, y1 / scale, (x2 - x1) / scale, (y2 - y1) / scale, *landmarks.reshape(-1), scores[k]],
                        dtype=np.float32,
                    )
                )
        rows.sort(key=lambda row: -float(row[-1]))
        kept: list = []
        for row in rows:
            if all(_box_iou(row, other) < 0.4 for other in kept):
                kept.append(row)
        return kept


def _box_iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    inter = max(0.0, min(ax2, bx2) - max(a[0], b[0])) * max(0.0, min(ay2, by2) - max(a[1], b[1]))
    return inter / max(1e-6, a[2] * a[3] + b[2] * b[3] - inter)


@lru_cache(maxsize=1)
def _scrfd() -> _SCRFD:
    return _SCRFD(SCRFD_PATH)


@lru_cache(maxsize=1)
def _arcface():
    return _onnx_session(ARCFACE_PATH)


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
    if engine() == "insightface":
        return _scrfd().detect(image_bgr, float(score_threshold))[:max_faces]
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
    """Align face to the standard 112x112 ArcFace template."""
    import cv2

    try:
        if engine() == "insightface":
            raise LookupError  # SFace's alignCrop isn't loaded; use the template below
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

    if engine() == "insightface":
        session = _arcface()
        blob = cv2.dnn.blobFromImage(
            aligned_bgr, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True
        )
        feat = session.run(None, {session.get_inputs()[0].name: blob})[0].reshape(-1).astype(np.float32)
        return (feat / (float(np.linalg.norm(feat)) or 1.0)).tolist()

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
