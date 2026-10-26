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
    scale = detection.object_detector.person_sensitivity_scale  # depends on the active model
    assert detection.person_min_confidence(farm) == scale["strict"]
    assert detection.person_min_confidence(office) == scale["high"]
    assert scale["high"] < scale["normal"] < scale["strict"]
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
        ("none", 0.0, 0.9, "pending"),  # no AI face (e.g. cow fur)
        ("insightface", 0.4, 0.9, "pending"),  # weak face
        ("insightface", 0.9, 0.25, "pending"),  # weak person box
        ("insightface", 0.9, 0.8, "unknown"),  # clear face, confident person
        ("sface", 0.9, 0.8, "unknown"),  # commercial-safe engine too
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


def test_strict_drops_person_with_animal_evidence():
    strict = detection.object_detector.person_sensitivity_scale["strict"]
    cow_like = ObjectDetection(x=0, y=0, width=50, height=80, confidence=0.6, class_name="person", animal_score=0.2)
    sure_person = ObjectDetection(x=0, y=0, width=50, height=80, confidence=0.8, class_name="person", animal_score=0.2)
    clean_person = ObjectDetection(x=0, y=0, width=50, height=80, confidence=0.6, class_name="person")
    kept = detection.drop_weak_persons([cow_like, sure_person, clean_person], strict)
    assert kept == [sure_person, clean_person]
    # High sensitivity (office) never applies the animal rule.
    high = detection.object_detector.person_sensitivity_scale["high"]
    assert cow_like in detection.drop_weak_persons([cow_like], high)


def test_strict_needs_two_sightings():
    cam = "persist-test"
    first = _det("person", 0.7)
    assert detection.require_two_sightings(cam, [first]) == []  # one frame: no box yet
    again = _det("person", 0.7, x=105)
    assert detection.require_two_sightings(cam, [again]) == [again]  # same place next check
    elsewhere = _det("person", 0.7, x=600)
    assert detection.require_two_sightings(cam, [elsewhere, _det("cow", 0.8)]) == [_det("cow", 0.8)]


def test_ignore_zone_drops_person_standing_inside():
    pen = [[0.4, 0.3], [0.7, 0.3], [0.7, 0.6], [0.4, 0.6]]
    calf = _det("person", 0.7, x=400, y=150, w=40, h=80)  # feet at (420, 230) of 800x500 → inside
    walker = _det("person", 0.7, x=100, y=300, w=40, h=120)  # feet at (120, 420) → outside
    cow = _det("cow", 0.8, x=400, y=150, w=80, h=60)
    kept = detection.drop_in_ignore_zones([calf, walker, cow], [pen], 800, 500)
    assert kept == [walker, cow]
    assert detection.drop_in_ignore_zones([calf], [], 800, 500) == [calf]


@pytest.mark.django_db
def test_camera_api_ignore_zones(api_client, admin_user, camera):
    api_client.force_authenticate(user=admin_user)
    zone = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]]
    ok = api_client.patch(f"/api/v1/cameras/{camera.id}/", {"ignore_zones": [zone]}, format="json")
    assert ok.status_code == 200 and ok.data["ignore_zones"] == [zone]
    camera.refresh_from_db()
    assert detection.ignore_zones(camera) == [zone]
    for bad in ([[[0.1, 0.1], [0.2, 0.2]]], [[[0.1, 0.1], [2, 0.2], [0.3, 0.3]]], "nope"):
        assert api_client.patch(f"/api/v1/cameras/{camera.id}/", {"ignore_zones": bad}, format="json").status_code == 400


def test_weak_person_with_animal_dropped_on_every_camera():
    high = detection.object_detector.person_sensitivity_scale["high"]
    weak_animalish = ObjectDetection(x=0, y=0, width=40, height=80, confidence=high + 0.05, class_name="person", animal_score=0.2)
    weak_clean = ObjectDetection(x=0, y=0, width=40, height=80, confidence=high + 0.05, class_name="person", animal_score=0.05)
    assert detection.drop_weak_persons([weak_animalish, weak_clean], high) == [weak_clean]
