"""CPU-friendly COCO object detection for the dashboard live preview."""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Live person-watch passes per second per camera (LIVE_DETECT_FPS).
LIVE_DETECT_INTERVAL = 1.0 / max(0.5, float(os.getenv("LIVE_DETECT_FPS", "4")))

# OpenCV native code is not reliably re-entrant on Windows; serialize all cv2 use.
opencv_lock = threading.RLock()

MODEL_CONFIG_URL = "https://raw.githubusercontent.com/AlexeyAB/darknet/master/cfg/yolov4-tiny.cfg"
MODEL_WEIGHTS_URL = (
    "https://github.com/AlexeyAB/darknet/releases/download/yolov4/yolov4-tiny.weights"
)
MODEL_CLASSES_URL = "https://raw.githubusercontent.com/AlexeyAB/darknet/master/data/coco.names"

FALLBACK_COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
)

# Focus groups: humans, animals, everyday objects.
PERSON_CLASSES = frozenset({"person"})
ANIMAL_CLASSES = frozenset(
    {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}
)
# Dangerous / illegal items COCO can see — flagged when near a person (hand/body).
DANGEROUS_CLASSES = frozenset(
    {
        "knife",
        "scissors",
        "baseball bat",
    }
)
DANGER_LABELS = {
    "knife": "KNIFE",
    "scissors": "SCISSORS",
    "baseball bat": "BAT / WEAPON",
}
# Unattended bags — often treated as illegal / security risk on CCTV.
ABANDONED_BAG_CLASSES = frozenset({"backpack", "handbag", "suitcase"})
ABANDONED_LABELS = {
    "backpack": "ABANDONED BAG",
    "handbag": "ABANDONED BAG",
    "suitcase": "ABANDONED SUITCASE",
}
# Mobile phone in hand / near face — capture enrolled people only in this case.
PHONE_CLASSES = frozenset({"cell phone"})
# Tiny / noisy COCO labels that often fire as false positives on phone cameras.
NOISY_CLASSES = frozenset(
    {
        "tie", "frisbee", "skis", "snowboard", "kite", "baseball glove",
        "skateboard", "surfboard", "tennis racket", "fork", "spoon", "toothbrush",
        "hair drier", "parking meter", "fire hydrant", "traffic light", "stop sign",
        "parking meter", "bench", "hot dog", "broccoli", "carrot", "donut",
        "toaster", "microwave", "oven", "sink", "toilet", "airplane", "train",
        "boat", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "horse",
    }
)
# Classes YOLO-tiny often misnames unless very confident.
# Note: cell phone is handled separately (mobile-use tracking needs sensitivity).
STRICT_CLASSES = frozenset(
    {
        "remote", "mouse", "keyboard", "book", "clock", "vase", "bowl",
        "wine glass", "cup", "bottle", "laptop", "tv",
        "potted plant", "chair", "couch", "dining table", "bed",
        "umbrella", "handbag", "backpack", "suitcase", "scissors", "knife",
    }
)
# Minimum gap between top-1 and top-2 class scores (reject ambiguous labels).
CLASS_SCORE_MARGIN = 0.14


@dataclass(frozen=True)
class ObjectDetection:
    x: int
    y: int
    width: int
    height: int
    confidence: float
    class_name: str


def _iou(a: ObjectDetection, b: ObjectDetection) -> float:
    ax2, ay2 = a.x + a.width, a.y + a.height
    bx2, by2 = b.x + b.width, b.y + b.height
    ix1, iy1 = max(a.x, b.x), max(a.y, b.y)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


def _nms(detections: list[ObjectDetection], iou_threshold: float = 0.4) -> list[ObjectDetection]:
    kept: list[ObjectDetection] = []
    for det in sorted(detections, key=lambda item: item.confidence, reverse=True):
        if all(_iou(det, existing) < iou_threshold for existing in kept):
            kept.append(det)
    return kept


def _min_confidence(class_name: str) -> float:
    """Accuracy-first thresholds — prefer correct name over extra boxes."""
    if class_name in PERSON_CLASSES:
        # Low bar so a person is caught on the first frame they step in.
        return 0.20
    if class_name in DANGEROUS_CLASSES:
        return 0.42
    # Phones are tiny on CCTV — keep sensitive for saved-member mobile tracking.
    if class_name in PHONE_CLASSES:
        return 0.18
    if class_name in {"cat", "dog", "bird"}:
        return 0.48
    if class_name in ANIMAL_CLASSES:
        return 0.55
    if class_name in NOISY_CLASSES:
        return 0.72
    if class_name in STRICT_CLASSES:
        return 0.50
    return 0.48


def _is_plausible_box(det: ObjectDetection, frame_w: int, frame_h: int) -> bool:
    """Keep only solid, well-sized detections to stop wrong names."""
    area = det.width * det.height
    frame_area = max(1, frame_w * frame_h)
    ratio = area / frame_area
    if det.class_name in PHONE_CLASSES:
        # Handheld phones: allow small boxes near the body.
        min_ratio = 0.0008
        min_side = 8
    elif det.class_name in DANGEROUS_CLASSES:
        min_ratio = 0.003
        min_side = 16
    elif det.class_name in PERSON_CLASSES:
        # Far / edge-of-frame people still count while they are on camera.
        min_ratio = 0.002
        min_side = 14
    else:
        min_ratio = 0.012
        min_side = 28
    if ratio < min_ratio:
        return False
    if ratio > 0.92:
        return False
    if det.width < min_side or det.height < min_side:
        return False
    aspect = det.width / max(1, det.height)
    if det.class_name in PHONE_CLASSES:
        if aspect > 8 or aspect < 0.12:
            return False
    elif det.class_name in PERSON_CLASSES:
        # Allow seated / angled / walking people.
        if aspect > 3.0 or aspect < 0.12:
            return False
    elif aspect > 6.5 or aspect < 0.14:
        return False
    if det.confidence < _min_confidence(det.class_name):
        return False
    return True


def lookup_label_by_iou(
    box: tuple[int, int, int, int] | ObjectDetection,
    labels: dict | None,
    *,
    min_iou: float = 0.25,
):
    """Match a draw/track box to person_labels by IoU (smoothed coords drift)."""
    if not labels:
        return None
    if isinstance(box, ObjectDetection):
        query = ObjectDetection(
            x=box.x, y=box.y, width=box.width, height=box.height,
            confidence=1.0, class_name=box.class_name,
        )
    else:
        x, y, w, h = box
        query = ObjectDetection(x=x, y=y, width=w, height=h, confidence=1.0, class_name="person")
    exact = labels.get((query.x, query.y, query.width, query.height))
    if exact is not None:
        return exact
    best_info = None
    best = min_iou
    for key, info in labels.items():
        if not isinstance(key, tuple) or len(key) != 4:
            continue
        kx, ky, kw, kh = key
        score = _iou(
            query,
            ObjectDetection(x=kx, y=ky, width=kw, height=kh, confidence=1.0, class_name="person"),
        )
        if score > best:
            best = score
            best_info = info
    return best_info


def _prominence(det: ObjectDetection, frame_w: int, frame_h: int) -> float:
    """Score how clearly an item is 'in front of' the camera (size + center + confidence)."""
    area_ratio = (det.width * det.height) / max(1, frame_w * frame_h)

    cx = det.x + det.width / 2
    cy = det.y + det.height / 2
    dx = (cx - frame_w / 2) / max(1, frame_w / 2)
    dy = (cy - frame_h / 2) / max(1, frame_h / 2)
    dist = (dx * dx + dy * dy) ** 0.5  # 0 at center, ~1.4 at corners
    center_score = max(0.0, 1.0 - dist / 1.4)

    return det.confidence * (0.55 + 0.30 * area_ratio * 6 + 0.15 * center_score)


def _focus_only(
    detections: list[ObjectDetection],
    frame_w: int,
    frame_h: int,
) -> list[ObjectDetection]:
    """
    Live detection is human-first.

    - Persons always kept (drawn on the live frame).
    - Phones / weapons / bags kept only for internal modules (not drawn as boxes).
    - Everyday objects, animals, furniture, etc. are dropped.
    """
    if not detections:
        return []

    persons: list[ObjectDetection] = []
    dangers: list[ObjectDetection] = []
    bags: list[ObjectDetection] = []
    phones: list[ObjectDetection] = []
    for det in detections:
        if det.class_name in PERSON_CLASSES:
            persons.append(det)
            continue
        if det.class_name in DANGEROUS_CLASSES:
            dangers.append(det)
            continue
        if det.class_name in ABANDONED_BAG_CLASSES:
            bags.append(det)
            continue
        if det.class_name in PHONE_CLASSES:
            phones.append(det)

    persons.sort(key=lambda d: _prominence(d, frame_w, frame_h), reverse=True)
    dangers.sort(key=lambda d: d.confidence, reverse=True)
    bags.sort(key=lambda d: d.confidence, reverse=True)
    phones.sort(key=lambda d: d.confidence, reverse=True)

    # Persons dominate; helper classes stay available for mobile / illegal checks.
    focused = persons[:12] + dangers[:4] + bags[:3] + phones[:3]
    focused.sort(key=lambda d: _prominence(d, frame_w, frame_h), reverse=True)
    return focused


class OpenCVObjectDetector:
    """YOLOv4-tiny COCO detector — only confident, visible boxes."""

    def __init__(self):
        self._net = None
        self._classes = list(FALLBACK_COCO_CLASSES)
        self._load_error: str | None = None
        self.model_name = "yolov4-tiny"
        self.model_version = "coco-80"
        self.last_fast_ms = 0.0

    def _download(self, url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        response = requests.get(url, timeout=(10, 120), stream=True)
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        partial.replace(destination)

    def _load(self):
        if self._net is not None:
            return self._net
        if self._load_error is not None:
            return None
        # Two cold callers must not download into the same .part file at once.
        with opencv_lock:
            if self._net is not None or self._load_error is not None:
                return self._net
            return self._load_locked()

    def _load_locked(self):
        try:
            import cv2

            model_dir = Path(settings.BASE_DIR) / "model_cache" / "yolov4_tiny"
            config_path = model_dir / "yolov4-tiny.cfg"
            weights_path = model_dir / "yolov4-tiny.weights"
            classes_path = model_dir / "coco.names"
            if not config_path.exists():
                self._download(MODEL_CONFIG_URL, config_path)
            if not weights_path.exists():
                self._download(MODEL_WEIGHTS_URL, weights_path)
            if not classes_path.exists():
                self._download(MODEL_CLASSES_URL, classes_path)

            self._classes = [
                line.strip()
                for line in classes_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] or list(FALLBACK_COCO_CLASSES)
            network = cv2.dnn.readNetFromDarknet(str(config_path), str(weights_path))
            network.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            network.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            try:
                # One inference at a time (opencv_lock). OPENCV_THREADS trades
                # latency for CPU; 2 keeps the box quick without pinning the host.
                cv2.setNumThreads(int(os.getenv("OPENCV_THREADS", "2")))
            except Exception:
                pass
            self._net = network
            logger.info("Loaded YOLOv4-tiny COCO-80 object detector on CPU")
        except Exception as exc:
            self._load_error = str(exc)
            logger.warning("Object detector unavailable; using person-only fallback: %s", exc)
        return self._net

    def _enhance(self, frame):
        """Mild contrast boost — helps YOLO read labels without inventing ghosts."""
        import cv2

        mean_luma = float(np.mean(frame))
        if mean_luma >= 95:
            return frame
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        lightness, a_channel, b_channel = cv2.split(lab)
        clip = 2.0 if mean_luma >= 55 else 2.8
        lightness = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8)).apply(lightness)
        return cv2.cvtColor(cv2.merge([lightness, a_channel, b_channel]), cv2.COLOR_LAB2BGR)

    def _infer_single(
        self,
        frame,
        confidence_threshold: float,
        *,
        input_size: int = 416,
    ) -> list[ObjectDetection]:
        import cv2

        net = self._load()
        if net is None:
            return []

        height, width = frame.shape[:2]
        working = frame
        if max(height, width) > 1280:
            scale = 1280 / max(height, width)
            working = cv2.resize(frame, (int(width * scale), int(height * scale)))
        else:
            scale = 1.0

        size = max(320, int(input_size))
        # YOLO darknet prefers multiples of 32.
        size = max(320, (size // 32) * 32)
        blob = cv2.dnn.blobFromImage(
            working, scalefactor=1 / 255.0, size=(size, size), swapRB=True, crop=False
        )
        net.setInput(blob)
        outputs = net.forward(net.getUnconnectedOutLayersNames())

        wh = working.shape[1]
        hh = working.shape[0]
        detections: list[ObjectDetection] = []
        for output in outputs:
            for row in output:
                scores = np.asarray(row[5:], dtype=np.float32)
                if scores.size == 0:
                    continue
                class_id = int(np.argmax(scores))
                confidence = float(scores[class_id])
                if confidence < confidence_threshold:
                    continue
                if class_id < 0 or class_id >= len(self._classes):
                    continue
                class_name = self._classes[class_id]
                if confidence < _min_confidence(class_name):
                    continue
                # Reject ambiguous class picks (top-1 barely beats top-2).
                # Persons + phones stay exempt — margin was dropping real humans.
                if (
                    class_name not in PHONE_CLASSES
                    and class_name not in PERSON_CLASSES
                    and scores.size >= 2
                ):
                    top2 = np.partition(scores, -2)[-2:]
                    second = float(np.min(top2))
                    if (confidence - second) < CLASS_SCORE_MARGIN and confidence < 0.72:
                        continue
                cx, cy, bw, bh = [float(value) for value in row[:4]]
                box_w = int(bw * wh)
                box_h = int(bh * hh)
                x = int(cx * wh - box_w / 2)
                y = int(cy * hh - box_h / 2)
                if scale != 1.0:
                    x = int(x / scale)
                    y = int(y / scale)
                    box_w = int(box_w / scale)
                    box_h = int(box_h / scale)
                x = max(0, min(width - 1, x))
                y = max(0, min(height - 1, y))
                box_w = max(1, min(width - x, box_w))
                box_h = max(1, min(height - y, box_h))
                detections.append(
                    ObjectDetection(
                        x=x,
                        y=y,
                        width=box_w,
                        height=box_h,
                        confidence=confidence,
                        class_name=class_name,
                    )
                )
        return detections

    def detect_fast(
        self,
        frame,
        confidence_threshold: float = 0.20,
        input_size: int = 320,
        focus: bool = True,
    ) -> list[ObjectDetection]:
        """
        One small YOLO pass for live human lock.

        320 (sometimes 416) finishes several times faster than the old
        608 + 416 double pass, so a person is caught on the frame they enter.
        Phones and weapons come from the same pass — no second forward.
        """
        import time

        with opencv_lock:
            if frame is None or getattr(frame, "size", 0) == 0:
                return []

            started = time.perf_counter()
            orig_h, orig_w = frame.shape[:2]
            # CLAHE on every frame was delaying the first look. Only very dark
            # scenes need it; normal indoor light is already readable.
            working = frame
            if float(np.mean(frame)) < 42:
                working = self._enhance(frame)
            raw = self._infer_single(working, confidence_threshold, input_size=input_size)
            candidates = [det for det in raw if _is_plausible_box(det, orig_w, orig_h)]
            deduped = _nms(candidates, iou_threshold=0.45)
            if focus:
                deduped = _focus_only(deduped, orig_w, orig_h)
            self.last_fast_ms = (time.perf_counter() - started) * 1000.0
            return deduped

    def detect(
        self,
        frame,
        confidence_threshold: float = 0.30,
        focus: bool = True,
    ) -> list[ObjectDetection]:
        """Single 416 pass. Live watch uses detect_fast at 320 for lower delay."""
        return self.detect_fast(
            frame,
            confidence_threshold=confidence_threshold,
            input_size=416,
            focus=focus,
        )


object_detector = OpenCVObjectDetector()


class LiveDetectionTracker:
    """
    Instant human lock + sticky hold while the person stays on camera.

    - Persons paint on the first YOLO hit
    - Tracks survive brief YOLO misses / motion until they leave frame
    """

    def __init__(self, *, hold_sec: float = 16.0, max_miss: int = 14, confirm_hits: int = 1):
        # Long hold: keep box+name painted between detect ticks and short occlusions.
        self.hold_sec = hold_sec
        self.max_miss = max_miss
        self.confirm_hits = confirm_hits
        self._tracks: dict[str, list[dict]] = {}
        self._labels: dict[str, dict] = {}
        self._stairs_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def forget(self, camera_id: str) -> None:
        """Drop all state for a camera nobody is watching any more."""
        key = str(camera_id)
        with self._lock:
            self._tracks.pop(key, None)
            self._labels.pop(key, None)
            self._stairs_until.pop(key, None)

    def update(
        self,
        camera_id: str,
        detections: list[ObjectDetection],
        person_labels: dict | None = None,
        frame_h: int | None = None,
    ) -> tuple[list[ObjectDetection], dict]:
        import time

        now = time.time()
        key = str(camera_id)
        person_labels = person_labels or {}
        with self._lock:
            tracks = self._tracks.get(key, [])
            matched_ids: set[int] = set()
            next_tracks: list[dict] = []

            for det in detections:
                best_i = -1
                # Loose IoU so walking / turning people stay the same track.
                best_iou = 0.12 if det.class_name == "person" else 0.28
                for i, track in enumerate(tracks):
                    if i in matched_ids:
                        continue
                    if track["class_name"] != det.class_name:
                        continue
                    score = _iou(det, track["det"])
                    if score > best_iou:
                        best_iou = score
                        best_i = i
                # Center-distance match when IoU is low (fast movement).
                if best_i < 0 and det.class_name == "person":
                    dcx, dcy = det.x + det.width / 2, det.y + det.height / 2
                    best_dist = 1e18
                    for i, track in enumerate(tracks):
                        if i in matched_ids or track["class_name"] != "person":
                            continue
                        t = track["det"]
                        tcx, tcy = t.x + t.width / 2, t.y + t.height / 2
                        dist = ((dcx - tcx) ** 2 + (dcy - tcy) ** 2) ** 0.5
                        scale = max(det.width, det.height, t.width, t.height, 1)
                        if dist < scale * 0.85 and dist < best_dist:
                            best_dist = dist
                            best_i = i
                label_info = lookup_label_by_iou(det, person_labels, min_iou=0.12)
                # Every human paints immediately — no second-hit wait.
                instant_ok = det.class_name == "person"
                if best_i >= 0:
                    matched_ids.add(best_i)
                    prev = tracks[best_i]
                    hits = int(prev.get("hits", 1)) + 1
                    px, py, pw, ph = prev["det"].x, prev["det"].y, prev["det"].width, prev["det"].height
                    # Persons follow the live box. Heavy smoothing made a walking
                    # human look late even after YOLO had already seen them.
                    if det.class_name == "person" or hits <= 2:
                        old_w, new_w = 0.15, 0.85
                    else:
                        old_w, new_w = 0.55, 0.45
                    smooth = ObjectDetection(
                        x=int(round(px * old_w + det.x * new_w)),
                        y=int(round(py * old_w + det.y * new_w)),
                        width=int(round(pw * old_w + det.width * new_w)),
                        height=int(round(ph * old_w + det.height * new_w)),
                        confidence=max(float(prev["det"].confidence) * 0.4, float(det.confidence)),
                        class_name=det.class_name,
                    )
                    next_label = label_info if label_info is not None else prev.get("label")
                    # Keep verified name only when this tick has no new identity yet.
                    # If face AI says UNKNOWN (or a different person), do not force old name.
                    if (
                        isinstance(label_info, dict)
                        and label_info.get("status") == "unknown"
                        and isinstance(prev.get("label"), dict)
                        and prev["label"].get("status") == "verified"
                        and float(label_info.get("match_score") or 0.0) >= 0.20
                    ):
                        # Clear wrong sticky name — stranger scored away from saved gallery.
                        next_label = label_info
                    elif (
                        isinstance(label_info, dict)
                        and label_info.get("status") == "verified"
                        and isinstance(prev.get("label"), dict)
                        and prev["label"].get("status") == "verified"
                        and label_info.get("person_id")
                        and prev["label"].get("person_id")
                        and label_info.get("person_id") != prev["label"].get("person_id")
                    ):
                        # Different saved person won — switch name.
                        next_label = label_info
                    if next_label is None:
                        next_label = prev.get("label")
                    next_tracks.append(
                        {
                            "det": smooth,
                            "class_name": det.class_name,
                            "miss": 0,
                            "hits": hits,
                            "seen_at": now,
                            "first_seen": float(prev.get("first_seen") or prev.get("seen_at") or now),
                            "stairs_alerted": bool(prev.get("stairs_alerted")),
                            "label": next_label,
                        }
                    )
                else:
                    next_tracks.append(
                        {
                            "det": det,
                            "class_name": det.class_name,
                            "miss": 0,
                            "hits": self.confirm_hits if instant_ok else 1,
                            "seen_at": now,
                            "first_seen": now,
                            "stairs_alerted": False,
                            "label": label_info,
                        }
                    )

            for i, track in enumerate(tracks):
                if i in matched_ids:
                    continue
                age = now - float(track["seen_at"])
                hold = self.hold_sec if track["class_name"] != "person" else self.hold_sec * 1.35
                if age > hold:
                    continue
                if int(track.get("hits", 0)) < self.confirm_hits:
                    continue
                miss = int(track.get("miss", 0)) + 1
                miss_limit = self.max_miss if track["class_name"] != "person" else self.max_miss + 6
                if miss > miss_limit:
                    continue
                next_tracks.append({**track, "miss": miss})

            # Someone just stepped in from the bottom of the frame (stairs).
            # Pulse is short so the live view can sound the alert immediately.
            if frame_h and int(frame_h) > 0:
                entered = False
                height = int(frame_h)
                for track in next_tracks:
                    if track.get("class_name") != "person" or track.get("stairs_alerted"):
                        continue
                    first = float(track.get("first_seen") or track.get("seen_at") or now)
                    if now - first > 2.2:
                        continue
                    if _entered_from_below(track["det"], height):
                        track["stairs_alerted"] = True
                        entered = True
                if entered:
                    self._stairs_until[key] = now + 1.6

            self._tracks[key] = next_tracks
            confirmed = [
                t for t in next_tracks if int(t.get("hits", 0)) >= self.confirm_hits
            ]
            sticky_dets = [t["det"] for t in confirmed]
            sticky_labels: dict = {}
            for track in confirmed:
                det = track["det"]
                info = track.get("label")
                if info is not None:
                    sticky_labels[(det.x, det.y, det.width, det.height)] = info
            self._labels[key] = {
                "detections": sticky_dets,
                "person_labels": sticky_labels,
                "updated_at": now,
                "class_names": list(dict.fromkeys(t["class_name"] for t in confirmed)),
            }
            return sticky_dets, sticky_labels

    def stairs_hot(self, camera_id: str) -> bool:
        """True for a short moment after a person enters from the bottom."""
        import time

        with self._lock:
            return time.time() < float(self._stairs_until.get(str(camera_id), 0.0))

    def peek(self, camera_id: str) -> tuple[list[ObjectDetection], dict, list[str]]:
        import time

        key = str(camera_id)
        with self._lock:
            payload = self._labels.get(key) or {}
            updated = float(payload.get("updated_at") or 0.0)
            if not payload or (time.time() - updated) > self.hold_sec + 2.5:
                return [], {}, []
            return (
                list(payload.get("detections") or []),
                dict(payload.get("person_labels") or {}),
                list(payload.get("class_names") or []),
            )


live_detection_tracker = LiveDetectionTracker()


class LivePersonWatcher:
    """
    Scan the newest camera frame in a loop and publish person boxes immediately.

    The HTTP detect call used to run a slow model and then face recognition
    before any box was drawn. A person could leave during that wait. This
    loop only does the fast person pass and updates the live tracker, so the
    preview paints a box on the next frame.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cameras: dict[str, dict] = {}
        self._latest: dict[str, tuple[list, float]] = {}
        self._thread: threading.Thread | None = None
        self._pass = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def touch(self, camera_id: str, source_url: str) -> None:
        if not camera_id or not source_url:
            return
        now = time.time()
        with self._lock:
            current = self._cameras.get(str(camera_id))
            if current is None:
                self._cameras[str(camera_id)] = {
                    "url": source_url,
                    "touched": now,
                    "inferred": 0.0,
                }
            else:
                current["url"] = source_url
                current["touched"] = now
            if not self.running:
                self._thread = threading.Thread(
                    target=self._loop, name="live-person-watch", daemon=True
                )
                self._thread.start()

    def latest(self, camera_id: str, max_age: float = 0.9) -> list | None:
        with self._lock:
            item = self._latest.get(str(camera_id))
            if not item:
                return None
            dets, ts = item
            if time.time() - ts > max_age:
                return None
            return list(dets)

    def _loop(self) -> None:
        import time

        logger.info("Live person watcher started")
        while True:
            with self._lock:
                now = time.time()
                stale = [cid for cid, info in self._cameras.items() if now - info["touched"] >= 40.0]
                for cid in stale:
                    self._cameras.pop(cid, None)
                    self._latest.pop(cid, None)
            for cid in stale:
                live_detection_tracker.forget(cid)
            with self._lock:
                cams = [
                    (cid, info["url"], float(info.get("inferred") or 0.0))
                    for cid, info in self._cameras.items()
                    if info.get("url")
                ]
            if not cams:
                time.sleep(0.25)
                continue

            cams.sort(key=lambda item: item[2])
            camera_id, url, inferred = cams[0]
            # Cap passes per camera; back-to-back inference pinned a CPU core.
            wait = LIVE_DETECT_INTERVAL - (time.time() - inferred)
            if wait > 0:
                time.sleep(wait)
            try:
                from .live_stream import live_stream_pool

                frame = live_stream_pool.get_frame(url, prefer_fast=True)
                self._pass += 1
                # 320 on most looks so the first sighting is quick. Every 4th
                # look uses 416 when the CPU is keeping up, for smaller people.
                input_size = 320
                if self._pass % 4 == 0 and object_detector.last_fast_ms < 180:
                    input_size = 416
                detections = object_detector.detect_fast(frame, input_size=input_size)
                overlay = [
                    det
                    for det in detections
                    if det.class_name in PERSON_CLASSES or det.class_name in PHONE_CLASSES
                ]
                # None labels: keep any name the face pass already attached.
                live_detection_tracker.update(
                    camera_id, overlay, None, frame_h=int(frame.shape[0])
                )
                with self._lock:
                    self._latest[camera_id] = (detections, time.time())
                    info = self._cameras.get(camera_id)
                    if info is not None:
                        info["inferred"] = time.time()
            except Exception as exc:
                logger.debug("Person watch miss %s: %s", camera_id, exc)
                with self._lock:
                    info = self._cameras.get(camera_id)
                    if info is not None:
                        info["inferred"] = time.time()
                time.sleep(0.08)


live_person_watcher = LivePersonWatcher()


def boot_person_watcher() -> None:
    """Start watching active cameras as soon as the server is up."""

    def _run() -> None:
        import time

        time.sleep(2.0)
        try:
            from apps.cameras.models import Camera

            for camera in Camera.objects.filter(is_active=True).exclude(rtsp_url=""):
                live_person_watcher.touch(str(camera.id), camera.rtsp_url)
        except Exception:
            logger.debug("Person watcher boot skipped", exc_info=True)

    threading.Thread(target=_run, name="person-watch-boot", daemon=True).start()


def _entered_from_below(det: ObjectDetection, frame_h: int) -> bool:
    """Person is in the lower band of the picture — coming up from the stairs."""
    if frame_h <= 0:
        return False
    foot = det.y + det.height
    mid = det.y + det.height * 0.5
    return foot >= frame_h * 0.68 or mid >= frame_h * 0.60


def _box_center(det: ObjectDetection) -> tuple[float, float]:
    return det.x + det.width / 2.0, det.y + det.height / 2.0


def _point_in_expanded_person(px: float, py: float, person: ObjectDetection, pad: float = 0.15) -> bool:
    """True if point is inside person box (expanded) — typically hand/arm area."""
    pad_x = person.width * pad
    pad_y = person.height * pad
    return (
        person.x - pad_x <= px <= person.x + person.width + pad_x
        and person.y - pad_y <= py <= person.y + person.height + pad_y
    )


def find_phones_in_use(detections: list[ObjectDetection]) -> list[dict]:
    """
    Detect cell phones held / used near a person's hands, ear, or torso.

    Returns {phone, person, confidence, associated} for each phone near a body.
    """
    persons = [d for d in detections if d.class_name in PERSON_CLASSES]
    phones = [d for d in detections if d.class_name in PHONE_CLASSES]
    if not phones or not persons:
        return []

    findings: list[dict] = []
    for phone in phones:
        cx, cy = _box_center(phone)
        associated = None
        best_score = -1.0
        for person in persons:
            # Generous pad: hand / ear / lap / pocket still count as using mobile.
            near = _point_in_expanded_person(cx, cy, person, pad=0.65)
            px = person.x + person.width / 2
            py = person.y + person.height * 0.35  # bias toward upper body / hands
            scale = max(person.width, person.height, 1)
            dist_n = (((cx - px) ** 2 + (cy - py) ** 2) ** 0.5) / scale
            # IoU of phone vs person (phone often sits inside the person box).
            overlap = _iou(phone, person)
            if not near and dist_n > 1.15 and overlap < 0.02:
                continue
            score = overlap * 2.0 + max(0.0, 1.2 - dist_n) + (0.3 if near else 0.0)
            if score > best_score:
                best_score = score
                associated = person

        if associated is None:
            continue
        findings.append(
            {
                "kind": "phone_use",
                "phone": phone,
                "person": associated,
                "confidence": phone.confidence,
                "associated": True,
                "label": "MOBILE IN USE",
            }
        )
    return findings


def person_using_phone(person: ObjectDetection, phone_findings: list[dict]) -> bool:
    """True when a phone finding is associated with this person box."""
    for item in phone_findings:
        other = item.get("person")
        if other is None:
            continue
        if (
            other.x == person.x
            and other.y == person.y
            and other.width == person.width
            and other.height == person.height
        ):
            return True
        # Fallback: phone person overlap / center near this body.
        if _iou(person, other) >= 0.25:
            return True
        cx, cy = _box_center(other)
        if _point_in_expanded_person(cx, cy, person, pad=0.55):
            return True
    return False


def find_hand_dangers(
    detections: list[ObjectDetection],
) -> list[dict]:
    """
    Find dangerous COCO objects that appear in/near a person's hands/body.

    Returns list of {weapon_type, label, confidence, danger, person, kind}.
    """
    persons = [d for d in detections if d.class_name in PERSON_CLASSES]
    dangers = [d for d in detections if d.class_name in DANGEROUS_CLASSES]
    if not dangers:
        return []

    findings: list[dict] = []
    for danger in dangers:
        cx, cy = _box_center(danger)
        associated = None
        best_dist = 1e18
        for person in persons:
            hand_band_top = person.y + person.height * 0.25
            hand_band_bot = person.y + person.height * 0.95
            in_hand_band = hand_band_top <= cy <= hand_band_bot
            near = _point_in_expanded_person(cx, cy, person, pad=0.25)
            if not (near or in_hand_band and abs(cx - (person.x + person.width / 2)) < person.width):
                if not _point_in_expanded_person(cx, cy, person, pad=0.35):
                    continue
            dist = (cx - (person.x + person.width / 2)) ** 2 + (cy - (person.y + person.height / 2)) ** 2
            if dist < best_dist:
                best_dist = dist
                associated = person

        findings.append(
            {
                "kind": "weapon",
                "weapon_type": danger.class_name,
                "label": DANGER_LABELS.get(danger.class_name, danger.class_name.upper()),
                "confidence": danger.confidence,
                "danger": danger,
                "person": associated,
                "associated": associated is not None,
            }
        )
    return findings


def find_abandoned_bags(
    detections: list[ObjectDetection],
    *,
    min_separation: float = 0.18,
) -> list[dict]:
    """Bags/suitcases with no nearby person — possible illegal unattended item."""
    persons = [d for d in detections if d.class_name in PERSON_CLASSES]
    bags = [d for d in detections if d.class_name in ABANDONED_BAG_CLASSES]
    findings: list[dict] = []
    for bag in bags:
        bx, by = _box_center(bag)
        near_person = False
        for person in persons:
            px, py = _box_center(person)
            # Normalize distance by frame-ish person size.
            scale = max(person.width, person.height, 1)
            dist = ((bx - px) ** 2 + (by - py) ** 2) ** 0.5 / scale
            if dist < min_separation * 4 or _point_in_expanded_person(bx, by, person, pad=0.5):
                near_person = True
                break
        if near_person:
            continue
        findings.append(
            {
                "kind": "abandoned",
                "weapon_type": bag.class_name,
                "label": ABANDONED_LABELS.get(bag.class_name, "ABANDONED ITEM"),
                "confidence": bag.confidence,
                "danger": bag,
                "person": None,
                "associated": False,
            }
        )
    return findings


def find_illegal_activity(frame, detections: list[ObjectDetection]) -> list[dict]:
    """
    Any illegal / security-critical signal we can detect locally:
    weapons in hand, abandoned bags.

    Color-based fire/smoke heuristics are intentionally omitted here — they
    false-trigger on phone / indoor lighting and spam Emergency Command.
    Use a dedicated fire/smoke model (edge SafetyProfile path) instead.
    """
    findings: list[dict] = []
    findings.extend(find_hand_dangers(detections))
    findings.extend(find_abandoned_bags(detections))
    return findings
