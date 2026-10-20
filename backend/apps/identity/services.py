"""Attendance and recognition business logic."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.cameras.models import Camera
from apps.events.models import DetectionEvent, EventType, Severity
from apps.events.pipeline import create_detection_event

from .embeddings import (
    APPEARANCE_DIM,
    APPEARANCE_MODEL,
    APPEARANCE_VERSION,
    appearance_embedding_from_bytes,
    best_match,
)
from .models import (
    AttendanceDirection,
    AttendanceRecord,
    BiometricConsent,
    FaceEmbedding,
    FaceImage,
    Person,
    PersonSighting,
    PersonType,
    UnknownSighting,
    VisitorRegistration,
    VisitorStatus,
)

MAX_FACE_ANGLES = 5
REQUIRED_ENROLL_ANGLES = ("front", "left", "right", "up", "down")


def enroll_face_bytes(
    *,
    person: Person,
    raw_bytes: bytes,
    consent: BiometricConsent | None,
    filename: str = "photo.jpg",
    angle_label: str = "front",
    make_primary: bool = False,
) -> dict:
    """
    AI-scan one enrollment angle photo with strict quality gates.
    """
    import uuid

    from django.core.files.base import ContentFile

    from .embeddings import l2_normalize
    from .face_scan import scan_face_from_bytes

    angle_label = (angle_label or "front").strip().lower()
    if angle_label not in REQUIRED_ENROLL_ANGLES:
        raise ValueError(f"Invalid angle '{angle_label}'. Use: {', '.join(REQUIRED_ENROLL_ANGLES)}.")

    try:
        scanned = scan_face_from_bytes(raw_bytes, require_face=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if getattr(scanned, "used_full_image", False) or int(getattr(scanned, "face_count", 0) or 0) < 1:
        raise ValueError(
            f"No clear human face found on {angle_label} photo. "
            "Show the full face clearly for this angle."
        )

    quality = float(scanned.quality_score)
    blur = float(scanned.blur_score)
    # Ultra-strict enrollment quality — weak photos are rejected.
    if quality < 0.48:
        raise ValueError(
            f"Face quality too low on {angle_label} ({quality:.0%}). "
            "Use brighter light and a sharper photo."
        )
    if blur < 28:
        raise ValueError(
            f"Face too blurry on {angle_label} (blur {blur:.0f}). "
            "Hold steady and upload a sharper image."
        )
    engine = getattr(scanned, "engine", "unknown")
    if engine not in {"sface", "yunet", "haar"} and not getattr(scanned, "feature", None):
        raise ValueError(f"Face AI could not extract identity particles on {angle_label} photo.")

    if make_primary:
        FaceImage.objects.filter(person=person, is_primary=True).update(is_primary=False)

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        ext = "jpg"

    face_image = FaceImage.objects.create(
        person=person,
        quality_score=quality,
        blur_score=blur,
        is_primary=make_primary,
        source="upload",
        metadata={
            "face_count": int(scanned.face_count),
            "bbox": [int(v) for v in scanned.bbox],
            "ai_face_scan": True,
            "display": "original",
            "angle": angle_label,
        },
    )
    face_image.image.save(
        f"{person.id}-{angle_label}-{uuid.uuid4().hex[:8]}.{ext}",
        ContentFile(raw_bytes),
        save=True,
    )
    face_image.image_path = face_image.image.name
    face_image.save(update_fields=["image_path"])

    crop_image = FaceImage.objects.create(
        person=person,
        quality_score=quality,
        blur_score=blur,
        is_primary=False,
        source="enrollment_scan",
        metadata={
            "face_count": int(scanned.face_count),
            "bbox": [int(v) for v in scanned.bbox],
            "ai_face_scan": True,
            "display": "face_crop",
            "angle": angle_label,
            "parent_image_id": str(face_image.id),
        },
    )
    crop_image.image.save(
        f"{person.id}-crop-{angle_label}-{uuid.uuid4().hex[:8]}.jpg",
        ContentFile(scanned.face_jpeg),
        save=True,
    )
    crop_image.image_path = crop_image.image.name
    crop_image.save(update_fields=["image_path"])

    if getattr(scanned, "feature", None):
        embedding = l2_normalize(list(scanned.feature))
    else:
        embedding = appearance_embedding_from_bytes(scanned.face_jpeg)

    emb = FaceEmbedding.objects.create(
        person=person,
        face_image=crop_image,
        consent=consent,
        model_name=APPEARANCE_MODEL,
        model_version=APPEARANCE_VERSION,
        vector=embedding,
        quality_score=quality,
        is_active=True,
    )

    # Keep only the newest MAX_FACE_ANGLES active embeddings for this person.
    active_ids = list(
        FaceEmbedding.objects.filter(person=person, is_active=True)
        .order_by("-created_at")
        .values_list("id", flat=True)[:MAX_FACE_ANGLES]
    )
    FaceEmbedding.objects.filter(person=person, is_active=True).exclude(id__in=active_ids).update(
        is_active=False
    )
    invalidate_gallery_cache()

    return {
        "face_image_id": str(face_image.id),
        "embedding_id": str(emb.id),
        "angle": angle_label,
        "quality_score": quality,
        "blur_score": blur,
        "face_particles": len(embedding),
        "face_engine": getattr(scanned, "engine", "unknown"),
        "bbox": [int(v) for v in scanned.bbox],
        "primary_image_url": face_image.image.url if face_image.image else "",
    }


def person_enrolled_angles(person: Person) -> set[str]:
    """Return which required angles already have an active enrollment embedding."""
    angles: set[str] = set()
    for emb in (
        FaceEmbedding.objects.filter(person=person, is_active=True)
        .exclude(model_name__endswith="-mature")
        .select_related("face_image")
    ):
        meta = (emb.face_image.metadata if emb.face_image_id and emb.face_image else {}) or {}
        angle = str(meta.get("angle") or "").strip().lower()
        if angle in REQUIRED_ENROLL_ANGLES:
            angles.add(angle)
    return angles


def mature_person_identity(person: Person) -> dict:
    """
    After all 5 angles are enrolled, fuse them into a strong identity profile.

    - Builds a centroid (average) embedding from all angle vectors
    - Marks person.metadata.enrollment_complete / identity_mature
    - Keeps per-angle embeddings active for multi-view matching
    """
    from .embeddings import l2_normalize

    embeddings = list(
        FaceEmbedding.objects.filter(person=person, is_active=True)
        .exclude(model_name__endswith="-mature")
        .select_related("face_image")
        .order_by("-quality_score", "-created_at")
    )
    by_angle: dict[str, list[float]] = {}
    qualities: list[float] = []
    for emb in embeddings:
        meta = (emb.face_image.metadata if emb.face_image_id and emb.face_image else {}) or {}
        angle = str(meta.get("angle") or "").strip().lower()
        if angle not in REQUIRED_ENROLL_ANGLES:
            continue
        if not emb.vector or angle in by_angle:
            continue
        by_angle[angle] = l2_normalize(list(emb.vector))
        qualities.append(float(emb.quality_score or 0))

    missing = [a for a in REQUIRED_ENROLL_ANGLES if a not in by_angle]
    if missing:
        person.metadata = {
            **(person.metadata or {}),
            "enrollment_complete": False,
            "identity_mature": False,
            "enrollment_angles": sorted(by_angle.keys()),
            "missing_angles": missing,
        }
        person.save(update_fields=["metadata", "updated_at"])
        return {
            "ok": False,
            "complete": False,
            "missing_angles": missing,
            "angles": sorted(by_angle.keys()),
        }

    vectors = [by_angle[a] for a in REQUIRED_ENROLL_ANGLES]
    dim = len(vectors[0])
    centroid = [sum(vec[i] for vec in vectors) / len(vectors) for i in range(dim)]
    fused = l2_normalize(centroid)
    avg_quality = sum(qualities) / max(1, len(qualities))

    # Replace previous mature vector.
    FaceEmbedding.objects.filter(person=person, model_name__endswith="-mature").update(is_active=False)
    mature_emb = FaceEmbedding.objects.create(
        person=person,
        face_image=None,
        consent=None,
        model_name=f"{APPEARANCE_MODEL}-mature",
        model_version=APPEARANCE_VERSION,
        vector=fused,
        quality_score=min(1.0, avg_quality + 0.08),
        is_active=True,
    )

    person.metadata = {
        **(person.metadata or {}),
        "enrollment_complete": True,
        "identity_mature": True,
        "enrollment_angles": list(REQUIRED_ENROLL_ANGLES),
        "missing_angles": [],
        "mature_embedding_id": str(mature_emb.id),
        "mature_quality": round(float(mature_emb.quality_score), 3),
        "angle_count": len(REQUIRED_ENROLL_ANGLES),
    }
    person.save(update_fields=["metadata", "updated_at"])
    return {
        "ok": True,
        "complete": True,
        "mature_embedding_id": str(mature_emb.id),
        "angles": list(REQUIRED_ENROLL_ANGLES),
        "quality": float(mature_emb.quality_score),
        "face_particles": len(fused),
    }


def _read_face_image_bytes(face_image) -> bytes | None:
    if getattr(face_image, "image", None):
        try:
            face_image.image.open("rb")
            data = face_image.image.read()
            face_image.image.close()
            if data:
                return data
        except Exception:
            pass
    if face_image.image_path:
        path = Path(face_image.image_path)
        if not path.is_absolute():
            path = Path(settings.MEDIA_ROOT) / path
        if path.is_file():
            return path.read_bytes()
    return None


def refresh_person_appearance_embeddings(organization_id=None) -> int:
    """Recompute gallery vectors from enrolled face crops using current model."""
    from .models import FaceImage

    qs = (
        FaceEmbedding.objects.filter(is_active=True, person__is_active=True)
        .exclude(model_name__endswith="-mature")
        .select_related("person", "face_image")
    )
    if organization_id is not None:
        qs = qs.filter(person__organization_id=organization_id)

    updated = 0
    for emb in qs:
        already_current = (
            emb.model_name == APPEARANCE_MODEL
            and emb.model_version == APPEARANCE_VERSION
            and bool(emb.vector)
            and len(emb.vector) == APPEARANCE_DIM
        )
        if already_current:
            continue

        face_image = emb.face_image
        # Prefer the AI face crop used at enrollment — never the full display photo.
        if face_image is None or getattr(face_image, "source", "") != "enrollment_scan":
            alt = (
                FaceImage.objects.filter(person_id=emb.person_id, source="enrollment_scan")
                .order_by("-created_at")
                .first()
            )
            if alt is not None:
                face_image = alt
        raw = _read_face_image_bytes(face_image) if face_image is not None else None
        if not raw:
            # No source photo to re-embed from: retire it so it isn't retried forever.
            emb.is_active = False
            emb.save(update_fields=["is_active", "updated_at"])
            continue
        emb.vector = appearance_embedding_from_bytes(raw)
        emb.face_image = face_image
        emb.model_name = APPEARANCE_MODEL
        emb.model_version = APPEARANCE_VERSION
        emb.save(
            update_fields=["vector", "face_image", "model_name", "model_version", "updated_at"]
        )
        updated += 1
    return updated


def _ensure_mature_identities(organization_id) -> None:
    """Build / refresh mature fused vectors when all 5 angles exist."""
    saved_types = [choice for choice, _ in PersonType.choices if choice != PersonType.UNKNOWN]
    people = Person.objects.filter(
        organization_id=organization_id,
        is_active=True,
        person_type__in=saved_types,
    )
    for person in people:
        angles = person_enrolled_angles(person)
        if len(angles) < len(REQUIRED_ENROLL_ANGLES):
            continue
        mature_current = FaceEmbedding.objects.filter(
            person=person,
            is_active=True,
            model_name__endswith="-mature",
            model_version=APPEARANCE_VERSION,
        ).exists()
        if mature_current:
            continue
        try:
            mature_person_identity(person)
        except Exception:
            continue


# Per-org gallery cache. Rebuilding (refresh + mature + load) costs hundreds of
# queries, so it only runs after a Person / FaceEmbedding / FaceImage change
# (see apps.identity.signals) or when the safety TTL expires.
_GALLERY_TTL_SEC = 300.0
_gallery_lock = threading.Lock()
_gallery_generation = 0
_gallery_cache: dict[str, tuple[int, float, list]] = {}


def invalidate_gallery_cache() -> None:
    global _gallery_generation
    with _gallery_lock:
        _gallery_generation += 1
        _gallery_cache.clear()


def get_gallery(organization_id) -> list[tuple[str, list[float], FaceEmbedding]]:
    key = str(organization_id)
    now = time.monotonic()
    with _gallery_lock:
        generation = _gallery_generation
        cached = _gallery_cache.get(key)
        if cached and cached[0] == generation and now - cached[1] < _GALLERY_TTL_SEC:
            return cached[2]
    gallery = _build_gallery(organization_id)
    with _gallery_lock:
        # Saves made while building bump the generation; the next call rebuilds
        # once more and finds nothing left to refresh.
        _gallery_cache[key] = (generation, now, gallery)
    return gallery


def _build_gallery(organization_id) -> list[tuple[str, list[float], FaceEmbedding]]:
    refresh_person_appearance_embeddings(organization_id)
    try:
        _ensure_mature_identities(organization_id)
    except Exception:
        pass
    mature_model = f"{APPEARANCE_MODEL}-mature"
    # Include every saved role (student/staff/intern/…) — old filter missed most enrollments.
    saved_types = [choice for choice, _ in PersonType.choices if choice != PersonType.UNKNOWN]
    rows = (
        FaceEmbedding.objects.filter(
            is_active=True,
            person__organization_id=organization_id,
            person__is_active=True,
            person__person_type__in=saved_types,
            model_name__in=[APPEARANCE_MODEL, mature_model],
        )
        .select_related("person")
        .order_by("-created_at")
    )
    gallery = []
    for emb in rows:
        if emb.vector:
            gallery.append((str(emb.person_id), emb.vector, emb))
    return gallery


def recognize_embedding(
    organization_id,
    embedding: list[float],
    threshold: float = 0.32,
    min_margin: float = 0.05,
    person_types: list[str] | None = None,
) -> tuple[Person | None, float, FaceEmbedding | None]:
    """
    Match against all active angle embeddings + mature fused vector.

    Prefer correct name over a forced match — weak scores stay Unknown.
    """
    gallery = get_gallery(organization_id)
    if person_types is not None:
        allowed = set(person_types)
        gallery = [
            (pid, vec, emb)
            for pid, vec, emb in gallery
            if emb.person.person_type in allowed
        ]
    flat = [(pid, vec, emb.model_name.endswith("-mature")) for pid, vec, emb in gallery]
    person_id, score = best_match(
        embedding, flat, threshold=threshold, min_margin=min_margin
    )
    if not person_id:
        return None, score, None
    person = Person.objects.filter(id=person_id, organization_id=organization_id).first()
    if person is None:
        return None, score, None
    meta = person.metadata or {}
    # Partial enrollments need a slightly stronger hit (fewer angles to trust).
    if not meta.get("identity_mature") and score < max(threshold, 0.34):
        return None, score, None
    emb = next((e for pid, _, e in gallery if pid == person_id), None)
    return person, score, emb


def recognize_blacklist(
    organization_id,
    embedding: list[float],
    threshold: float = 0.30,
) -> tuple[Person | None, float, FaceEmbedding | None]:
    """Match only against enrolled thieves / blacklist profiles."""
    return recognize_embedding(
        organization_id,
        embedding,
        threshold=threshold,
        min_margin=0.025,
        person_types=[PersonType.BLACKLIST],
    )


def record_attendance(
    person: Person,
    camera: Camera | None,
    confidence: float,
    event: DetectionEvent | None = None,
    now=None,
) -> AttendanceRecord | None:
    """Auto clock-in/out with late/overtime flags. Debounced per direction window."""
    now = now or timezone.now()
    recent = (
        AttendanceRecord.objects.filter(person=person, recorded_at__gte=now - timedelta(minutes=10))
        .order_by("-recorded_at")
        .first()
    )
    last_today = (
        AttendanceRecord.objects.filter(person=person, recorded_at__date=now.date())
        .order_by("-recorded_at")
        .first()
    )

    if last_today is None or last_today.direction == AttendanceDirection.OUT:
        direction = AttendanceDirection.IN
    else:
        direction = AttendanceDirection.OUT

    if recent and recent.direction == direction:
        return None

    is_late = False
    is_overtime = False
    local_t = timezone.localtime(now).time()
    if direction == AttendanceDirection.IN and person.shift_start:
        late_after = (
            datetime.combine(now.date(), person.shift_start) + timedelta(minutes=15)
        ).time()
        is_late = local_t > late_after
    if direction == AttendanceDirection.OUT and person.shift_end:
        is_overtime = local_t > person.shift_end

    return AttendanceRecord.objects.create(
        organization=person.organization,
        person=person,
        site=camera.site if camera else None,
        camera=camera,
        event=event,
        direction=direction,
        recorded_at=now,
        confidence=confidence,
        is_late=is_late,
        is_overtime=is_overtime,
        notes="auto",
    )


def create_person_sighting(
    *,
    person: Person,
    camera: Camera | None,
    snapshot_path: str,
    confidence: float,
    match_score: float,
    event: DetectionEvent | None = None,
    notes: str = "camera_match",
) -> PersonSighting:
    return PersonSighting.objects.create(
        organization=person.organization,
        person=person,
        camera=camera,
        event=event,
        snapshot_path=snapshot_path,
        confidence=confidence,
        match_score=match_score,
        notes=notes or "camera_match",
    )


def save_profile_image_from_snapshot(
    *,
    person: Person,
    snapshot_path: str,
    camera: Camera | None = None,
    match_score: float = 0.0,
    reason: str = "mobile_in_use",
) -> FaceImage | None:
    """
    Attach a camera snapshot to an enrolled person's profile gallery.

    Used when a saved member is detected using a mobile phone.
    Does not change primary enrollment photo unless they have none.
    """
    import uuid
    from django.core.files.base import ContentFile

    if not snapshot_path or str(snapshot_path).startswith(("http://", "https://")):
        return None

    path = Path(snapshot_path)
    if not path.is_absolute():
        path = Path(settings.MEDIA_ROOT) / str(snapshot_path).lstrip("/\\")
    if not path.is_file():
        return None

    # Avoid flooding profile: max one camera_mobile image every ~20s per person.
    recent = (
        FaceImage.objects.filter(person=person, source="camera_mobile")
        .order_by("-created_at")
        .first()
    )
    if recent and recent.created_at >= timezone.now() - timedelta(seconds=20):
        return recent

    raw = path.read_bytes()
    if not raw or len(raw) < 200:
        return None

    quality = 0.0
    blur = 0.0
    try:
        from .face_scan import scan_face_from_bytes

        scanned = scan_face_from_bytes(raw, require_face=False, min_size=20)
        quality = float(getattr(scanned, "quality_score", 0) or 0)
        blur = float(getattr(scanned, "blur_score", 0) or 0)
        if getattr(scanned, "face_jpeg", None) and len(scanned.face_jpeg) > 200:
            raw = scanned.face_jpeg
    except Exception:
        pass

    make_primary = not person.face_images.filter(is_primary=True).exists()
    if make_primary:
        FaceImage.objects.filter(person=person, is_primary=True).update(is_primary=False)

    face_image = FaceImage.objects.create(
        person=person,
        quality_score=quality,
        blur_score=blur,
        is_primary=make_primary,
        source="camera_mobile",
        metadata={
            "reason": reason,
            "camera_id": str(camera.id) if camera else "",
            "camera_name": camera.name if camera else "",
            "match_score": round(float(match_score or 0.0), 3),
            "auto_profile_save": True,
        },
    )
    face_image.image.save(
        f"{person.id}-mobile-{uuid.uuid4().hex[:10]}.jpg",
        ContentFile(raw),
        save=True,
    )
    face_image.image_path = face_image.image.name
    face_image.save(update_fields=["image_path"])

    # Keep last 12 mobile captures on profile to limit disk use.
    keep_ids = list(
        FaceImage.objects.filter(person=person, source="camera_mobile")
        .order_by("-created_at")
        .values_list("id", flat=True)[:12]
    )
    FaceImage.objects.filter(person=person, source="camera_mobile").exclude(id__in=keep_ids).delete()
    return face_image


def create_unknown_sighting(
    organization,
    camera: Camera | None,
    embedding: list[float],
    confidence: float,
    track_id: int | None = None,
    snapshot_path: str = "",
    event: DetectionEvent | None = None,
) -> UnknownSighting:
    sighting = UnknownSighting.objects.create(
        organization=organization,
        camera=camera,
        event=event,
        snapshot_path=snapshot_path,
        embedding_vector=embedding,
        confidence=confidence,
        track_id=track_id,
        gallery_person=None,
    )
    if event is None and camera is not None:
        event = create_detection_event(
            camera=camera,
            event_type=EventType.UNKNOWN_PERSON,
            severity=Severity.WARNING,
            confidence=confidence,
            model_name=APPEARANCE_MODEL,
            model_version=APPEARANCE_VERSION,
            payload={"track_id": track_id, "unknown_sighting_id": str(sighting.id)},
        ).event
        sighting.event = event
        sighting.save(update_fields=["event"])
    return sighting


def handle_visitor_face_match(person: Person, confidence: float) -> VisitorRegistration | None:
    """If matched person is linked to an approved visitor, check them in."""
    now = timezone.now()
    visitor = (
        VisitorRegistration.objects.filter(
            person=person,
            status=VisitorStatus.APPROVED,
            valid_from__lte=now,
            valid_until__gte=now,
        )
        .order_by("-created_at")
        .first()
    )
    if not visitor:
        return None
    visitor.status = VisitorStatus.CHECKED_IN
    visitor.checked_in_at = now
    visitor.match_confidence = confidence
    visitor.metadata = {**visitor.metadata, "host_notified": True, "notify_stub": "email/sms planned"}
    visitor.save()
    return visitor


def attendance_summary(organization_id, date_from=None, date_to=None) -> dict:
    qs = AttendanceRecord.objects.filter(organization_id=organization_id)
    if date_from:
        qs = qs.filter(recorded_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(recorded_at__date__lte=date_to)
    return {
        "total_records": qs.count(),
        "clock_ins": qs.filter(direction=AttendanceDirection.IN).count(),
        "clock_outs": qs.filter(direction=AttendanceDirection.OUT).count(),
        "late_count": qs.filter(is_late=True).count(),
        "overtime_count": qs.filter(is_overtime=True).count(),
        "unique_persons": qs.values("person_id").distinct().count(),
    }
