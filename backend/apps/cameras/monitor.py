"""Background camera monitoring — detect/capture even when UI leaves /cameras."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

_LAST_VISION: dict[str, float] = {}
_VISION_COOLDOWN_SEC = 12.0


def monitor_active_cameras(
    *,
    organization_ids: list | None = None,
    run_vision: bool = True,
) -> dict[str, Any]:
    """
    Poll every active camera with a stream URL:
    - live object/person detection for ALL humans in frame
    - capture unknown/threat always; known only when using mobile
    """
    from apps.cameras.models import Camera
    from apps.cameras.views import _capture_frame, _detect_and_record

    cameras = (
        Camera.objects.filter(is_active=True)
        .exclude(rtsp_url="")
        .select_related("site", "site__organization", "zone")
    )
    if organization_ids is not None:
        cameras = cameras.filter(site__organization_id__in=organization_ids)

    results: list[dict[str, Any]] = []
    now = time.time()

    for camera in cameras:
        entry: dict[str, Any] = {
            "camera_id": str(camera.id),
            "camera_name": camera.name,
            "ok": False,
            "persons": 0,
            "objects": [],
            "vision": None,
        }
        try:
            frame, _, _, _ = _capture_frame(camera.rtsp_url, prefer_fast=True)
            persons, labels, verify = _detect_and_record(frame, camera)
            entry["persons"] = persons
            entry["objects"] = labels
            entry["verify"] = {
                "verified": verify.get("verified", 0),
                "unknown": verify.get("unknown", 0),
                "blacklist": verify.get("blacklist", 0),
                "labels": verify.get("labels") or [],
                "people": len(verify.get("people") or []),
                "activity": bool(verify.get("activity")),
            }
            entry["ok"] = True

            camera.last_seen_at = timezone.now()
            camera.status = "online"
            camera.save(update_fields=["last_seen_at", "status", "updated_at"])

            if run_vision:
                key = str(camera.id)
                if now - _LAST_VISION.get(key, 0.0) >= _VISION_COOLDOWN_SEC:
                    vision = _maybe_save_vision(camera, frame, verify=verify)
                    _LAST_VISION[key] = now
                    if vision is not None:
                        entry["vision"] = {
                            "id": str(vision.id),
                            "summary": vision.summary,
                            "people": len(vision.people or []),
                            "objects": len(vision.detected_objects or []),
                        }
        except Exception as exc:
            logger.info("Monitor tick failed for %s: %s", camera.name, exc)
            entry["error"] = str(exc)
            try:
                camera.status = "offline"
                camera.save(update_fields=["status", "updated_at"])
            except Exception:
                pass
        results.append(entry)

    return {
        "checked": len(results),
        "ok": sum(1 for item in results if item.get("ok")),
        "cameras": results,
        "timestamp": timezone.now().isoformat(),
    }


def _maybe_save_vision(camera, frame, verify=None):
    """Save a crop covering every detected human + their activity."""
    from apps.cameras.models import VisionCapture
    from apps.cameras.views import (
        _analysis_has_real_activity,
        _crop_person_jpeg,
        _focus_box_for_people,
        _has_actionable_activity,
    )
    from apps.cameras.vision_llm import analyze_frame_jpeg

    verify = verify or {}
    unknown_count = int(verify.get("unknown") or 0)
    threat_count = int(verify.get("blacklist") or 0)
    verified_count = int(verify.get("verified") or 0)
    if not _has_actionable_activity(verify):
        return None

    focus_box = _focus_box_for_people(verify)
    if not focus_box:
        return None

    multi = len(verify.get("person_boxes") or []) >= 2
    has_unknown = unknown_count > 0
    image_bytes, relative_path = _crop_person_jpeg(
        frame,
        focus_box,
        subdir="vision",
        pad_ratio=0.40 if has_unknown else (0.18 if multi else 0.12),
        min_long_side=800 if has_unknown or multi else 640,
    )
    if not image_bytes or not relative_path:
        return None

    analysis = analyze_frame_jpeg(image_bytes)
    if not _analysis_has_real_activity(analysis, require_people=True):
        return None

    if unknown_count:
        person_status = "unknown"
    elif threat_count:
        person_status = "blacklist"
    else:
        person_status = "verified"

    detected_people = [p for p in (verify.get("people") or []) if isinstance(p, dict)]
    people = detected_people or list(analysis.people or [])
    if not people:
        labels = [
            str(label)
            for label in (verify.get("labels") or [])
            if label and not str(label).lower().startswith("issue")
        ]
        if (
            not labels
            and threat_count <= 0
            and unknown_count <= 0
            and verified_count <= 0
            and not verify.get("activity")
        ):
            return None
        people = [
            {
                "description": label,
                "activity": "present on camera",
                "status": person_status,
            }
            for label in labels[:12]
        ] or [
            {
                "description": "Person on camera",
                "activity": "present on camera",
                "status": person_status,
            }
        ]
    else:
        for person in people:
            if isinstance(person, dict) and "status" not in person:
                person["status"] = person_status

    human_total = unknown_count + threat_count + verified_count
    summary = analysis.summary.strip() or (
        f"{human_total} person(s) on {camera.name} "
        f"({verified_count} known, {unknown_count} unknown"
        + (f", {threat_count} threat" if threat_count else "")
        + ")"
    )

    latest = VisionCapture.objects.filter(camera=camera).first()
    if (
        latest
        and latest.summary.strip().lower() == summary.strip().lower()
        and latest.created_at >= timezone.now() - timedelta(seconds=60)
    ):
        return latest

    event = None
    try:
        from apps.events.pipeline import create_detection_event

        if threat_count:
            severity = "critical"
        elif unknown_count:
            severity = "warning"
        else:
            severity = "info"

        event = create_detection_event(
            camera=camera,
            event_type="unknown_person" if (unknown_count or threat_count) else "person_detected",
            severity=severity,
            confidence=analysis.confidence or 0.7,
            model_name=f"vision-llm:{analysis.provider}",
            model_version=analysis.model_name,
            payload={
                "source": "background_monitor",
                "capture_kind": "multi_human_activity" if human_total >= 2 else "human_activity",
                "summary": summary,
                "scene": analysis.scene,
                "people": people,
                "objects": [],
                "risks": analysis.risks,
                "image_path": relative_path,
                "bbox": focus_box,
                "person_count": human_total,
                "verified_count": verified_count,
                "unknown_count": unknown_count,
                "blacklist_count": threat_count,
            },
            run_hooks=True,
        ).event
    except Exception:
        event = None

    return VisionCapture.objects.create(
        organization=camera.site.organization,
        camera=camera,
        event=event,
        image_path=relative_path,
        summary=summary,
        scene=analysis.scene or "people activity",
        people=people,
        detected_objects=[],
        risks=analysis.risks,
        text_visible=analysis.text_visible,
        confidence=analysis.confidence,
        model_name=analysis.model_name,
        provider=analysis.provider,
        raw_response=analysis.raw_response[:8000],
    )
