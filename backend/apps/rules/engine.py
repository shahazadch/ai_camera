"""
Rule engine: match AI results that are already computed against each camera's rules.

Callers (the live person watcher and the face pass) report what they saw; this
module checks schedule + cooldown and runs the rule's actions. It never runs
detection itself, so rules add no model work.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from typing import Any

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

_RULES_TTL_SEC = 60.0
_lock = threading.Lock()
_generation = 0
# camera_id -> (generation, loaded_at, org_id, camera_name, [Rule, ...])
_rules_cache: dict[str, tuple[int, float, str, str, list]] = {}
# (rule_id, camera_id) -> last fired (monotonic)
_last_fired: dict[tuple[str, str], float] = {}

# Recent hits for sound / pop-up clients. Ids only grow; a restart starts over,
# which clients detect because the latest id drops below what they've seen.
_hit_ids = itertools.count(1)
_hits: deque[dict[str, Any]] = deque(maxlen=300)

EVENT_TYPE_FOR_CHECK = {
    "any_person": "person_detected",
    "stairs": "person_detected",
    "unknown_person": "unknown_person",
    "saved_person": "face_recognized",
    "mobile_use": "activity",
}


def invalidate_rules() -> None:
    global _generation
    with _lock:
        _generation += 1
        _rules_cache.clear()


def _load(camera_id: str) -> tuple[str, str, list]:
    from apps.cameras.models import Camera

    from .models import Rule

    key = str(camera_id)
    now = time.monotonic()
    with _lock:
        generation = _generation
        cached = _rules_cache.get(key)
        if cached and cached[0] == generation and now - cached[1] < _RULES_TTL_SEC:
            return cached[2], cached[3], cached[4]
    row = Camera.objects.filter(id=key).values("site__organization_id", "name").first()
    if row is None:
        org_id, name, rules = "", "", []
    else:
        org_id, name = str(row["site__organization_id"]), row["name"]
        rules = list(
            Rule.objects.filter(organization_id=org_id, enabled=True)
            .filter(Q(apply_to_all=True) | Q(cameras__id=key))
            .distinct()
        )
    with _lock:
        _rules_cache[key] = (generation, now, org_id, name, rules)
    return org_id, name, rules


def rules_for(camera_id: str) -> list:
    return _load(camera_id)[2]


def camera_needs(camera_id: str, checks) -> bool:
    """True when an enabled rule on this camera uses any of ``checks`` (any time of day)."""
    wanted = set(checks)
    try:
        return any(rule.detect in wanted for rule in rules_for(camera_id))
    except Exception:
        logger.debug("Rule lookup failed for %s", camera_id, exc_info=True)
        return False


def _saved_person_matches(rule, result: dict | None) -> bool:
    if not result or result.get("status") != "verified":
        return False
    params = rule.params or {}
    person_ids = {str(p) for p in params.get("person_ids") or []}
    person_types = set(params.get("person_types") or [])
    if person_ids and str(result.get("person_id")) not in person_ids:
        return False
    if person_types and result.get("person_type") not in person_types:
        return False
    return True


def trigger(
    camera_id: str,
    check: str,
    *,
    frame=None,
    box=None,
    result: dict | None = None,
    confidence: float = 0.0,
) -> int:
    """Run every matching rule for this camera. Returns how many rules fired."""
    try:
        org_id, camera_name, rules = _load(str(camera_id))
    except Exception:
        logger.debug("Rule lookup failed for %s", camera_id, exc_info=True)
        return 0
    fired = 0
    now_dt = timezone.now()
    for rule in rules:
        if rule.detect != check:
            continue
        if check == "saved_person" and not _saved_person_matches(rule, result):
            continue
        if not rule.active_at(now_dt):
            continue
        key = (str(rule.id), str(camera_id))
        now = time.monotonic()
        with _lock:
            if now - _last_fired.get(key, -1e9) < float(rule.cooldown_sec or 0):
                continue
            _last_fired[key] = now
        try:
            _run_actions(rule, str(camera_id), camera_name, org_id, frame, box, result, confidence)
            fired += 1
        except Exception:
            logger.warning("Rule %s failed on camera %s", rule.name, camera_id, exc_info=True)
    return fired


def _run_actions(rule, camera_id, camera_name, org_id, frame, box, result, confidence) -> None:
    from .models import Rule

    snapshot_path = ""
    event_id = None
    if rule.save_photo or rule.alert_severity:
        snapshot_path = _save_snapshot(frame, box) if rule.save_photo else ""
        event_id = _create_event(rule, camera_id, snapshot_path, result, confidence)
    if rule.sound or rule.popup or rule.speak:
        label = (result or {}).get("name") if rule.detect == "saved_person" else ""
        with _lock:
            _hits.append(
                {
                    "id": next(_hit_ids),
                    "org_id": org_id,
                    "rule_id": str(rule.id),
                    "rule_name": rule.name,
                    "camera_id": camera_id,
                    "camera_name": camera_name,
                    "sound": rule.sound,
                    "speech": rule.speech(camera_name, label or ""),
                    "popup": rule.popup,
                    "message": f"{rule.name}: {label + ' on ' if label else ''}{camera_name}",
                    "event_id": str(event_id) if event_id else None,
                    "at": timezone.now().isoformat(),
                }
            )
    Rule.objects.filter(id=rule.id).update(last_triggered_at=timezone.now())


def _save_snapshot(frame, box) -> str:
    if frame is None:
        return ""
    from apps.cameras.views import _crop_person_jpeg, _write_clear_jpeg

    if box is not None:
        _bytes, path = _crop_person_jpeg(frame, box, subdir="rules", pad_ratio=0.35, min_long_side=640)
        return path or ""
    _bytes, path = _write_clear_jpeg(frame, subdir="rules", min_long_side=960)
    return path or ""


def _create_event(rule, camera_id, snapshot_path, result, confidence):
    from apps.cameras.models import Camera
    from apps.events.pipeline import create_detection_event

    camera = Camera.objects.select_related("site__organization").filter(id=camera_id).first()
    if camera is None:
        return None
    payload = {
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "rule_detect": rule.detect,
        "source": "rule",
        "auto_capture": bool(snapshot_path),
    }
    if snapshot_path:
        payload["snapshot_path"] = snapshot_path
    if result:
        for field in ("person_id", "name", "match_score"):
            if result.get(field) is not None:
                payload[field] = result[field]
    return create_detection_event(
        camera=camera,
        event_type=EVENT_TYPE_FOR_CHECK.get(rule.detect, "person_detected"),
        severity=rule.alert_severity or "info",
        confidence=float(confidence or 0.0),
        model_name="rules",
        model_version="1",
        payload=payload,
        # Severity set = a real alert (notifications etc.); photo-only stays quiet.
        run_hooks=bool(rule.alert_severity),
    ).event.id


def record_test_hit(rule, camera_name: str = "Test") -> dict:
    """Queue a hit for the Test button without touching cooldowns or events."""
    with _lock:
        hit = {
            "id": next(_hit_ids),
            "org_id": str(rule.organization_id),
            "rule_id": str(rule.id),
            "rule_name": rule.name,
            "camera_id": "",
            "camera_name": camera_name,
            "sound": rule.sound or ("" if rule.speak else "chime"),
            "speech": rule.speech(camera_name, "Test person" if rule.detect == "saved_person" else ""),
            "popup": True,
            "message": f"Test: {rule.name}",
            "event_id": None,
            "at": timezone.now().isoformat(),
        }
        _hits.append(hit)
    return hit


def hits_after(after: int | None, org_ids: set[str] | None) -> tuple[int, list[dict]]:
    """(latest_id, hits newer than ``after``) — org_ids None means every org."""
    with _lock:
        snapshot = list(_hits)
    latest = snapshot[-1]["id"] if snapshot else 0
    if after is None:
        return latest, []
    visible = [
        {k: v for k, v in hit.items() if k != "org_id"}
        for hit in snapshot
        if hit["id"] > after and (org_ids is None or hit["org_id"] in org_ids)
    ]
    return latest, visible
