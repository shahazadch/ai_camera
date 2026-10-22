"""Animals must not become 'unknown person' (farm cameras)."""

from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest
from django.core.management import call_command

from apps.cameras import detection
from apps.cameras.detection import ObjectDetection
from apps.events.models import DetectionEvent
from apps.identity.models import UnknownSighting


def _det(cls, conf, x=100, y=100, w=200, h=150):
    return ObjectDetection(x=x, y=y, width=w, height=h, confidence=conf, class_name=cls)


def test_weak_person_inside_cow_is_dropped():
    cow = _det("cow", 0.7)
    weak_person = _det("person", 0.3, x=120, y=110, w=90, h=120)
    kept = detection._suppress_animal_persons([cow, weak_person])
    assert [d.class_name for d in kept] == ["cow"]


def test_confident_person_next_to_cow_is_kept():
    cow = _det("cow", 0.7)
    farmer = _det("person", 0.82, x=150, y=90, w=60, h=170)
    kept = detection._suppress_animal_persons([cow, farmer])
    assert {d.class_name for d in kept} == {"cow", "person"}


def test_person_beside_cow_without_overlap_is_kept():
    cow = _det("cow", 0.7)
    person = _det("person", 0.3, x=600, y=100, w=60, h=170)
    assert len(detection._suppress_animal_persons([cow, person])) == 2


def test_camera_sensitivity_threshold():
    farm = SimpleNamespace(metadata={"person_sensitivity": "strict"})
    office = SimpleNamespace(metadata={})
    dets = [_det("person", 0.3), _det("cow", 0.6), _det("person", 0.7)]
    assert detection.person_min_confidence(farm) == 0.50
    assert detection.person_min_confidence(office) == 0.20
    kept = detection.drop_weak_persons(dets, detection.person_min_confidence(farm))
    assert [(d.class_name, d.confidence) for d in kept] == [("cow", 0.6), ("person", 0.7)]


def _fake_scan(engine, face_score):
    return SimpleNamespace(
        face_jpeg=b"jpeg",
        face_count=1,
        used_full_image=False,
        feature=[0.1] * 128,
        engine=engine,
        face_score=face_score,
        quality_score=0.8,
        blur_score=200.0,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "engine,face_score,person_conf,expected",
    [
        ("haar", 0.0, 0.9, "pending"),  # Haar-only "face" (e.g. cow fur)
        ("sface", 0.4, 0.9, "pending"),  # weak YuNet face
        ("sface", 0.9, 0.25, "pending"),  # weak person box
        ("sface", 0.9, 0.8, "unknown"),  # clear face, confident person
    ],
)
def test_unknown_needs_real_face(camera, engine, face_score, person_conf, expected):
    from apps.cameras import views

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with mock.patch.object(views, "_crop_person_jpeg", return_value=(b"jpg", "sightings/x.jpg")), mock.patch(
        "apps.identity.face_scan.scan_face_from_bytes", return_value=_fake_scan(engine, face_score)
    ), mock.patch("apps.identity.services.recognize_embedding", return_value=(None, 0.1, None)), mock.patch.object(
        views, "_face_blur_ok", return_value=True
    ), mock.patch.object(views, "_discard_snapshot"), mock.patch.object(
        views, "_emit_verify_event", return_value=None
    ), mock.patch(
        "apps.identity.services.create_unknown_sighting"
    ):
        result = views._process_person_box(frame, (10, 10, 100, 200, person_conf, "person"), camera, None, person_conf)
    assert result["status"] == expected


@pytest.mark.django_db
def test_cleanup_false_unknowns(org, camera):
    weak = DetectionEvent.objects.create(camera=camera, event_type="unknown_person", severity="warning", confidence=0.25)
    strong = DetectionEvent.objects.create(camera=camera, event_type="unknown_person", severity="warning", confidence=0.8)
    UnknownSighting.objects.create(organization=org, camera=camera, event=weak, confidence=0.25)
    UnknownSighting.objects.create(organization=org, camera=camera, event=strong, confidence=0.8)

    call_command("cleanup_false_unknowns", camera=camera.name, dry_run=True)
    assert DetectionEvent.objects.filter(event_type="unknown_person").count() == 2

    call_command("cleanup_false_unknowns", camera=camera.name)
    assert list(DetectionEvent.objects.filter(event_type="unknown_person").values_list("id", flat=True)) == [strong.id]
    assert UnknownSighting.objects.count() == 1


@pytest.mark.django_db
def test_camera_api_person_sensitivity(api_client, admin_user, camera):
    api_client.force_authenticate(user=admin_user)
    assert api_client.get(f"/api/v1/cameras/{camera.id}/").data["person_sensitivity"] == "high"
    response = api_client.patch(f"/api/v1/cameras/{camera.id}/", {"person_sensitivity": "strict"}, format="json")
    assert response.status_code == 200
    assert response.data["person_sensitivity"] == "strict"
    camera.refresh_from_db()
    assert camera.metadata["person_sensitivity"] == "strict"
    bad = api_client.patch(f"/api/v1/cameras/{camera.id}/", {"person_sensitivity": "max"}, format="json")
    assert bad.status_code == 400
