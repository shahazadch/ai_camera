"""Free local vision analysis — no API key required.

Primary mode uses on-device YOLOv4-tiny COCO-80 (cached for live detection).
Optional Ollama enrichment is used only when available and configured.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

VISION_PROMPT = """You are a CCTV security analyst. Focus only on SECURITY ACTIVITY.

Return ONLY valid JSON with this exact shape:
{
  "summary": "one short sentence about activity (or say empty scene)",
  "scene": "room/outdoor/office/street/etc and lighting",
  "people": [{"description": "age/gender/clothing/pose if visible", "activity": "what they are doing"}],
  "objects": [{"name": "object name", "detail": "color/size/location"}],
  "risks": [{"level": "info|warning|critical", "detail": "why this matters"}],
  "text_visible": ["any readable text/signs/labels"],
  "confidence": 0.0
}

Rules:
- If the frame is empty, idle furniture, or no person/threat activity, set people=[] objects=[] risks=[] and summary="Empty scene — no security activity".
- Only list people when a real human is clearly visible and doing something (walking, standing, loitering, face hidden, carrying item).
- Do NOT invent people, weapons, or risks from shadows/reflections.
- Ignore chairs, bottles, TVs, plants and other junk unless tied to a security risk.
- confidence is 0..1 for how clear the image is.
"""


@dataclass
class VisionAnalysis:
    summary: str = ""
    scene: str = ""
    people: list[dict[str, Any]] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    text_visible: list[str] = field(default_factory=list)
    confidence: float = 0.0
    model_name: str = ""
    provider: str = ""
    raw_response: str = ""


def _provider() -> str:
    return (getattr(settings, "VISION_LLM_PROVIDER", "free") or "free").lower()


_OLLAMA_PROBE_TTL_SEC = 60.0
_ollama_probe: tuple[float, bool] = (0.0, False)


def _ollama_reachable() -> bool:
    """Probe at most once a minute; each probe can block up to 2 s."""
    global _ollama_probe
    checked_at, reachable = _ollama_probe
    if time.monotonic() - checked_at < _OLLAMA_PROBE_TTL_SEC:
        return reachable
    base = (getattr(settings, "VISION_LLM_BASE_URL", "") or "http://127.0.0.1:11434/v1").rstrip("/")
    # OpenAI-compat base ends with /v1 — health is on host root /api/tags
    root = base[:-3] if base.endswith("/v1") else base
    try:
        response = requests.get(f"{root}/api/tags", timeout=2)
        reachable = response.status_code < 500
    except requests.RequestException:
        reachable = False
    _ollama_probe = (time.monotonic(), reachable)
    return reachable


def vision_llm_configured() -> bool:
    """Free local mode is always ready; cloud providers need a key."""
    provider = _provider()
    if provider in {"none", "off", "disabled"}:
        return False
    if provider in {"free", "local", "offline", "mobilenet", "ollama", "auto"}:
        return True
    return bool(getattr(settings, "VISION_LLM_API_KEY", ""))


def vision_llm_status() -> dict[str, Any]:
    provider = _provider()
    ollama_up = _ollama_reachable() if provider in {"ollama", "auto", "free", "local"} else False
    active = "free-local"
    model = "yolov4-tiny-coco-80"
    if provider == "ollama" and ollama_up:
        active = "ollama"
        model = getattr(settings, "VISION_LLM_MODEL", "llava")
    elif provider not in {"free", "local", "offline", "mobilenet", "ollama", "auto", "", "none"}:
        active = provider
        model = getattr(settings, "VISION_LLM_MODEL", "")

    return {
        "configured": vision_llm_configured(),
        "provider": active,
        "requested_provider": provider or "free",
        "model": model,
        "base_url": getattr(settings, "VISION_LLM_BASE_URL", ""),
        "has_api_key": bool(getattr(settings, "VISION_LLM_API_KEY", "")),
        "requires_api_key": False if active in {"free-local", "ollama"} else bool(getattr(settings, "VISION_LLM_API_KEY", "")),
        "ollama_available": ollama_up,
        "mode": "Free local AI — no API key",
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return {}
    return {}


def _decode_jpeg(image_bytes: bytes):
    import cv2

    array = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("Could not decode camera JPEG for free vision analysis.")
    return frame


def _analyze_free_local(image_bytes: bytes) -> VisionAnalysis:
    """On-device COCO-80 scene description — zero API key, offline after first download."""
    from apps.cameras.detection import object_detector

    frame = _decode_jpeg(image_bytes)
    detections = object_detector.detect(frame, confidence_threshold=0.45)

    people_boxes = [d for d in detections if d.class_name == "person"]
    object_boxes = [d for d in detections if d.class_name != "person"]
    counts = Counter(d.class_name for d in object_boxes)

    people = [
        {
            "description": f"Person detected ({box.confidence:.0%} confidence)",
            "activity": "visible in camera frame",
        }
        for box in people_boxes[:8]
    ]
    objects = [
        {
            "name": name,
            "detail": (
                f"{count} visible"
                if count > 1
                else f"{next(d.confidence for d in object_boxes if d.class_name == name):.0%} confidence"
            ),
        }
        for name, count in counts.most_common(12)
    ]

    # Object-only scenes (chair/bottle/tv) are not security activity.
    if people_boxes:
        parts = [f"{len(people_boxes)} person{'s' if len(people_boxes) != 1 else ''}"]
        if objects:
            parts.append(
                ", ".join(
                    f"{c} {n}{'s' if c > 1 else ''}" if c > 1 else n
                    for n, c in counts.most_common(3)
                )
            )
        summary = "Activity: " + " with ".join(parts) + "."
    else:
        summary = "Empty scene — no security activity."
        people = []
        objects = []

    height, width = frame.shape[:2]
    brightness = float(np.mean(frame))
    lighting = "bright" if brightness > 140 else "dim" if brightness < 70 else "normal lighting"
    scene = f"Camera frame {width}×{height}, {lighting}"

    risks: list[dict[str, Any]] = []
    if len(people_boxes) >= 5:
        risks.append({"level": "warning", "detail": f"Crowd-like activity: {len(people_boxes)} people visible"})

    avg_conf = (
        sum(d.confidence for d in detections) / len(detections) if detections else 0.0
    )

    payload = {
        "summary": summary,
        "scene": scene,
        "people": people,
        "objects": objects,
        "risks": risks,
        "detections": [
            {
                "label": d.class_name,
                "confidence": round(d.confidence, 3),
                "bbox": [d.x, d.y, d.width, d.height],
            }
            for d in detections
        ],
    }

    return VisionAnalysis(
        summary=summary,
        scene=scene,
        people=people,
        objects=objects,
        risks=risks,
        text_visible=[],
        confidence=max(0.0, min(1.0, float(avg_conf))),
        model_name=object_detector.model_name,
        provider="free-local",
        raw_response=json.dumps(payload),
    )


def _analyze_ollama(image_bytes: bytes) -> VisionAnalysis:
    model = getattr(settings, "VISION_LLM_MODEL", "llava") or "llava"
    base_url = (getattr(settings, "VISION_LLM_BASE_URL", "") or "http://127.0.0.1:11434/v1").rstrip("/")
    timeout = int(getattr(settings, "VISION_LLM_TIMEOUT_SEC", 90))

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/jpeg;base64,{b64}"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Ollama error {response.status_code}: {response.text[:300]}")

    body = response.json()
    content = (
        (((body.get("choices") or [{}])[0].get("message") or {}).get("content"))
        or ""
    )
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )

    parsed = _extract_json(content)
    confidence = parsed.get("confidence", 0.7)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.7

    return VisionAnalysis(
        summary=str(parsed.get("summary") or content[:280] or "No description returned."),
        scene=str(parsed.get("scene") or ""),
        people=list(parsed.get("people") or []),
        objects=list(parsed.get("objects") or []),
        risks=list(parsed.get("risks") or []),
        text_visible=[str(item) for item in (parsed.get("text_visible") or [])],
        confidence=max(0.0, min(1.0, confidence)),
        model_name=model,
        provider="ollama",
        raw_response=content if isinstance(content, str) else json.dumps(content),
    )


def analyze_frame_jpeg(image_bytes: bytes) -> VisionAnalysis:
    """Analyze a camera JPEG with free local AI (no API key). Optionally use Ollama if up."""
    provider = _provider()

    # Prefer free-local unless user explicitly forced ollama-only and it's up.
    if provider in {"ollama", "auto"} and _ollama_reachable():
        try:
            return _analyze_ollama(image_bytes)
        except Exception as exc:
            logger.warning("Ollama vision failed (%s); falling back to free local AI", exc)

    if provider not in {
        "free",
        "local",
        "offline",
        "mobilenet",
        "ollama",
        "auto",
        "",
    }:
        # Cloud providers still supported but require key — not the default path.
        api_key = getattr(settings, "VISION_LLM_API_KEY", "")
        if not api_key:
            logger.info("No API key for %s; using free local AI", provider)
            return _analyze_free_local(image_bytes)

    return _analyze_free_local(image_bytes)
