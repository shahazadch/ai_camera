"""Embedding utilities — identity vectors from the active face engine (see face_ai)."""

from __future__ import annotations

import hashlib
import math
from typing import Iterable

import numpy as np

from .face_ai import engine_info

# Name/version/size of the active face model's vectors. A different engine (or a
# version bump) makes the gallery re-embed saved people from their enrollment crops.
_ENGINE = engine_info()
APPEARANCE_MODEL = _ENGINE["model"]
APPEARANCE_VERSION = _ENGINE["version"]
APPEARANCE_DIM = _ENGINE["dim"]

# Cosine thresholds per engine, measured on this site's enrolled people: strict
# enough that strangers are not mislabeled as saved members.
DEFAULT_MATCH_THRESHOLD = _ENGINE["threshold"]
DEFAULT_MATCH_MARGIN = 0.05


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def simulate_embedding_from_bytes(data: bytes, dim: int = 128) -> list[float]:
    digest = hashlib.sha256(data).digest()
    values: list[float] = []
    seed = digest
    while len(values) < dim:
        seed = hashlib.sha256(seed).digest()
        for byte in seed:
            values.append((byte / 127.5) - 1.0)
            if len(values) >= dim:
                break
    return l2_normalize(values)


def appearance_embedding_from_bytes(data: bytes, dim: int = APPEARANCE_DIM) -> list[float]:
    """
    Identity vector for the face in an image, or [] when no real face is found.

    There is deliberately no non-AI fallback: texture descriptors made "faces"
    out of fur and walls, which then showed up as unknown people.
    """
    try:
        from .face_ai import embed_image_bytes, models_available

        if models_available():
            vector = embed_image_bytes(data)
            if vector and len(vector) == dim:
                return l2_normalize(vector)
    except Exception:
        pass
    return []


def best_match(
    query: list[float],
    gallery: Iterable[tuple],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    min_margin: float = DEFAULT_MATCH_MARGIN,
) -> tuple[str | None, float]:
    """
    Return (person_id, score) or (None, best_score).

    Gallery items are (person_id, vector) or (person_id, vector, is_mature).

    Multi-angle strong match:
    - Keep the best score per person across angle + mature vectors
    - Also keep the 2nd-best angle score; if both angles agree, accept
      slightly below the hard threshold (mature multi-view confirmation)
    - Still require a margin vs a different person
    - The fused mature vector scores but never counts as an extra angle
    """
    items = [item for item in gallery if len(item[1]) == len(query)]
    if not items or not query:
        return None, 0.0

    q = np.asarray(query, dtype=np.float32)
    q /= float(np.linalg.norm(q)) or 1.0
    matrix = np.asarray([item[1] for item in items], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    scores_all = (matrix @ q) / norms

    scores_by_person: dict[str, list[float]] = {}
    angle_scores: dict[str, list[float]] = {}
    for item, score in zip(items, scores_all.tolist()):
        person_id = item[0]
        scores_by_person.setdefault(person_id, []).append(score)
        if not (len(item) > 2 and item[2]):
            angle_scores.setdefault(person_id, []).append(score)

    person_best: dict[str, float] = {}
    person_support: dict[str, float] = {}
    person_angles: dict[str, int] = {}
    for person_id, scores in scores_by_person.items():
        best = max(scores)
        ranked_angles = sorted(angle_scores.get(person_id, []), reverse=True)
        second_angle = ranked_angles[1] if len(ranked_angles) > 1 else -1.0
        # Support score: average of top-2 angles (or best alone).
        support = (best + max(second_angle, 0.0)) / 2.0 if second_angle >= 0 else best
        person_best[person_id] = best
        person_support[person_id] = support
        person_angles[person_id] = len(ranked_angles)

    ranked = sorted(person_best.items(), key=lambda item: item[1], reverse=True)
    best_id, best_score = ranked[0]
    rival = ranked[1][1] if len(ranked) > 1 else -1.0
    support = person_support.get(best_id, best_score)
    angle_count = int(person_angles.get(best_id, 0))

    # Strong accept only. Soft path needs multi-angle support + clear margin
    # so one weak random hit never becomes a saved-member name.
    soft_threshold = max(threshold - 0.04, threshold * 0.88)
    accepted = best_score >= threshold
    if not accepted and best_score >= soft_threshold and angle_count >= 2:
        if support >= soft_threshold and (best_score - rival) >= max(0.06, min_margin):
            accepted = True
    if not accepted:
        return None, best_score
    # Reject ambiguous ties between two enrolled people.
    if len(ranked) > 1 and (best_score - rival) < min_margin:
        return None, best_score
    return best_id, best_score
