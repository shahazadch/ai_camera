"""Camera rules: schedule, matching, cooldown, actions, API and camera linking."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from apps.cameras.detection import ObjectDetection, live_detection_tracker
from apps.events.models import DetectionEvent
from apps.rules import engine
from apps.rules.models import Rule
from apps.tenancy.models import Organization

PKT = ZoneInfo("Asia/Karachi")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-10-19 is a Monday.
    return datetime(2026, 10, 19 + day, hour, minute, tzinfo=PKT)


@pytest.fixture(autouse=True)
def _fresh_engine(settings):
    settings.ALERT_TIME_ZONE = "Asia/Karachi"
    engine.invalidate_rules()
    engine._last_fired.clear()
    engine._hits.clear()
    yield
    engine.invalidate_rules()


def _rule(org, **kwargs):
    defaults = {"name": "R", "detect": "any_person", "popup": True, "cooldown_sec": 0}
    defaults.update(kwargs)
    return Rule.objects.create(organization=org, **defaults)


@pytest.mark.django_db
def test_schedule_day_window_and_days(org):
    rule = _rule(org, start_time=time(9), end_time=time(17), days=[0, 1, 2, 3, 4])
    assert rule.active_at(_at(0, 10))
    assert not rule.active_at(_at(0, 17))
    assert not rule.active_at(_at(5, 10))  # Saturday


@pytest.mark.django_db
def test_schedule_overnight_counts_from_start_day(org):
    rule = _rule(org, start_time=time(22), end_time=time(6), days=[4])  # Friday night
    assert rule.active_at(_at(4, 23))
    assert rule.active_at(_at(5, 2))  # early Saturday = Friday's night
    assert not rule.active_at(_at(5, 23))


@pytest.mark.django_db
def test_disabled_rule_never_active(org):
    assert not _rule(org, enabled=False).active_at(_at(0, 12))


@pytest.mark.django_db
def test_trigger_needs_camera_link_or_apply_to_all(org, camera):
    rule = _rule(org)
    assert engine.trigger(str(camera.id), "any_person") == 0
    rule.cameras.add(camera)
    assert engine.trigger(str(camera.id), "any_person") == 1
    rule.cameras.clear()
    rule.apply_to_all = True
    rule.save()
    assert engine.trigger(str(camera.id), "any_person") == 1


@pytest.mark.django_db
def test_cooldown_blocks_repeat(org, camera):
    _rule(org, apply_to_all=True, cooldown_sec=60)
    assert engine.trigger(str(camera.id), "any_person") == 1
    assert engine.trigger(str(camera.id), "any_person") == 0


@pytest.mark.django_db
def test_saved_person_filters(org, camera):
    _rule(org, detect="saved_person", apply_to_all=True, params={"person_types": ["staff"]})
    staff = {"status": "verified", "person_id": "p1", "person_type": "staff", "name": "Ali"}
    visitor = {"status": "verified", "person_id": "p2", "person_type": "visitor", "name": "Bo"}
    assert engine.trigger(str(camera.id), "saved_person", result=visitor) == 0
    assert engine.trigger(str(camera.id), "saved_person", result=staff) == 1
    assert engine.trigger(str(camera.id), "saved_person", result={"status": "unknown"}) == 0


@pytest.mark.django_db
def test_actions_create_alert_and_hit(org, camera):
    rule = _rule(org, apply_to_all=True, sound="siren", alert_severity="critical")
    latest, _ = engine.hits_after(None, None)
    engine.trigger(str(camera.id), "any_person", confidence=0.8)
    _, hits = engine.hits_after(latest, {str(org.id)})
    assert [h["sound"] for h in hits] == ["siren"]
    event = DetectionEvent.objects.get(payload__rule_id=str(rule.id))
    assert event.severity == "critical"
    rule.refresh_from_db()
    assert rule.last_triggered_at is not None


@pytest.mark.django_db
def test_hits_scoped_to_org(org, camera):
    _rule(org, apply_to_all=True, sound="chime")
    engine.trigger(str(camera.id), "any_person")
    other = Organization.objects.create(name="Other", slug="other")
    assert engine.hits_after(0, {str(other.id)})[1] == []
    assert len(engine.hits_after(0, {str(org.id)})[1]) == 1


def test_tracker_queues_arrival_once():
    cam = "test-rules-cam"
    live_detection_tracker.forget(cam)
    person = ObjectDetection(x=100, y=100, width=80, height=200, confidence=0.9, class_name="person")
    for _ in range(3):
        live_detection_tracker.update(cam, [person], frame_h=720)
    assert [check for check, _ in live_detection_tracker.pop_events(cam)] == ["any_person"]
    assert live_detection_tracker.pop_events(cam) == []
    live_detection_tracker.forget(cam)


@pytest.mark.django_db
def test_rules_api_and_camera_linking(api_client, admin_user, org, camera):
    api_client.force_authenticate(user=admin_user)
    created = api_client.post(
        "/api/v1/camera-rules/",
        {
            "organization": str(org.id),
            "name": "Night",
            "detect": "any_person",
            "start_time": "22:00",
            "end_time": "06:00",
            "sound": "siren",
        },
        format="json",
    )
    assert created.status_code == 201, created.data
    rule_id = created.data["id"]

    bad = api_client.post(
        "/api/v1/camera-rules/",
        {"organization": str(org.id), "name": "Half", "detect": "any_person", "start_time": "22:00"},
        format="json",
    )
    assert bad.status_code == 400

    linked = api_client.patch(f"/api/v1/cameras/{camera.id}/", {"rule_ids": [rule_id]}, format="json")
    assert linked.status_code == 200, linked.data
    assert [str(r) for r in linked.data["rule_ids"]] == [rule_id]
    assert api_client.get(f"/api/v1/camera-rules/{rule_id}/").data["camera_names"] == [camera.name]

    assert api_client.post(f"/api/v1/camera-rules/{rule_id}/test/").status_code == 201
    hits = api_client.get("/api/v1/camera-rules/hits/?after=0").data["hits"]
    assert hits and hits[-1]["message"] == "Test: Night"


@pytest.mark.django_db
def test_hits_endpoint_auth(api_client, settings):
    settings.ALERT_PLAYER_KEY = "k"
    assert api_client.get("/api/v1/camera-rules/hits/").status_code == 401
    assert api_client.get("/api/v1/camera-rules/hits/", HTTP_X_ALERT_KEY="bad").status_code == 403
    assert api_client.get("/api/v1/camera-rules/hits/", HTTP_X_ALERT_KEY="k").status_code == 200


@pytest.mark.django_db
def test_speech_text(org):
    rule = _rule(org, name="Night", speak=True)
    assert rule.speech("Office") == "Night at Office"
    assert rule.speech("Office", "Ali") == "Ali at Office"
    rule.speak_text = "Alert on {camera} for {name}"
    assert rule.speech("Gate", "Ali") == "Alert on Gate for Ali"
    rule.speak_text = "Bad {placeholder"
    assert rule.speech("Gate") == "Bad {placeholder"
    rule.speak = False
    assert rule.speech("Gate") == ""


@pytest.mark.django_db
def test_hit_carries_speech(org, camera):
    _rule(org, apply_to_all=True, sound="bell", speak=True, name="Door")
    engine.trigger(str(camera.id), "any_person")
    hit = engine.hits_after(0, None)[1][-1]
    assert hit["sound"] == "bell"
    assert hit["speech"] == f"Door at {camera.name}"


@pytest.mark.django_db
def test_upload_tune_and_use_in_rule(api_client, admin_user, org, settings, tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.MEDIA_ROOT = tmp_path
    settings.ALERT_PLAYER_KEY = "k"
    api_client.force_authenticate(user=admin_user)

    bad = api_client.post(
        "/api/v1/camera-rules/sounds/",
        {"organization": str(org.id), "name": "Doc", "file": SimpleUploadedFile("x.txt", b"hi")},
        format="multipart",
    )
    assert bad.status_code == 400

    upload = api_client.post(
        "/api/v1/camera-rules/sounds/",
        {
            "organization": str(org.id),
            "name": "Gate bell",
            "file": SimpleUploadedFile("gate.wav", b"RIFF0000WAVE", content_type="audio/wav"),
        },
        format="multipart",
    )
    assert upload.status_code == 201, upload.data
    key = upload.data["sound_key"]
    sound_id = upload.data["id"]

    rule = api_client.post(
        "/api/v1/camera-rules/",
        {"organization": str(org.id), "name": "Gate", "detect": "any_person", "sound": key},
        format="json",
    )
    assert rule.status_code == 201, rule.data
    nonsense = api_client.post(
        "/api/v1/camera-rules/",
        {"organization": str(org.id), "name": "X", "detect": "any_person", "sound": "custom:nope"},
        format="json",
    )
    assert nonsense.status_code == 400

    api_client.force_authenticate(user=None)
    player = api_client.get(f"/api/v1/camera-rules/sounds/{sound_id}/file/", HTTP_X_ALERT_KEY="k")
    assert player.status_code == 200
    assert b"".join(player.streaming_content) == b"RIFF0000WAVE"
    assert api_client.get(f"/api/v1/camera-rules/sounds/{sound_id}/file/").status_code == 401

    api_client.force_authenticate(user=admin_user)
    assert api_client.delete(f"/api/v1/camera-rules/sounds/{sound_id}/").status_code == 204
    assert Rule.objects.get(id=rule.data["id"]).sound == "chime"
