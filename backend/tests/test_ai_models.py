"""Smoke tests for the production AI models (skipped when a model file isn't present)."""

import numpy as np
import pytest

from apps.cameras import detection
from apps.identity import face_ai


@pytest.mark.skipif(not detection.DFINE_MODEL_PATH.is_file(), reason="D-FINE model not downloaded")
def test_dfine_runs_and_returns_detections():
    detector = detection.DFineObjectDetector()
    frame = np.full((576, 704, 3), 40, dtype=np.uint8)
    assert detector.detect_fast(frame) == []  # empty scene → nothing
    assert detector.last_fast_ms > 0


@pytest.mark.skipif(
    not (face_ai.SCRFD_PATH.is_file() and face_ai.ARCFACE_PATH.is_file()), reason="InsightFace models not downloaded"
)
def test_insightface_engine_shapes():
    assert face_ai.engine() == "insightface"
    assert face_ai.detect_faces(np.zeros((480, 640, 3), dtype=np.uint8)) == []
    vector = face_ai._embed_aligned(np.full((112, 112, 3), 128, dtype=np.uint8))
    assert len(vector) == 512
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-3
