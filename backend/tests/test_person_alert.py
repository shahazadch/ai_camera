"""Per-camera person-alert window + arrivals endpoint."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from apps.cameras.detection import ObjectDetection, live_detection_tracker

PKT = ZoneInfo("Asia/Karachi")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 20, hour, minute, tzinfo=PKT)


@pytest.mark.django_db
def test_alert_off_never_active(camera):
    camera.person_alert_enabled = False
    assert not camera.person_alert_active(_at(12))


@pytest.mark.django_db
def test_alert_all_day_when_no_window(camera):
    camera.person_alert_enabled = True
    assert camera.person_alert_active(_at(3))
    assert camera.person_alert_active(_at(15))


@pytest.mark.django_db
def test_alert_daytime_window(camera, settings):
    settings.ALERT_TIME_ZONE = "Asia/Karachi"
    camera.person_alert_enabled = True
    camera.person_alert_start = time(9, 0)
    camera.person_alert_end = time(17, 0)
    assert camera.person_alert_active(_at(9, 0))
    assert camera.person_alert_active(_at(16, 59))
    assert not camera.person_alert_active(_at(17, 0))
    assert not camera.person_alert_active(_at(8, 59))


@pytest.mark.django_db
def test_alert_overnight_window(camera, settings):
    settings.ALERT_TIME_ZONE = "Asia/Karachi"
    camera.person_alert_enabled = True
    camera.person_alert_start = time(22, 0)
    camera.person_alert_end = time(6, 0)
    assert camera.person_alert_active(_at(23, 30))
    assert camera.person_alert_active(_at(2, 0))
    assert not camera.person_alert_active(_at(6, 0))
    assert not camera.person_alert_active(_at(12, 0))


@pytest.mark.django_db
def test_window_uses_alert_time_zone(camera, settings):
    settings.ALERT_TIME_ZONE = "Asia/Karachi"
    camera.person_alert_enabled = True
    camera.person_alert_start = time(22, 0)
    camera.person_alert_end = time(23, 0)
    # 17:30 UTC is 22:30 in Pakistan.
    assert camera.person_alert_active(datetime(2026, 10, 20, 17, 30, tzinfo=ZoneInfo("UTC")))


@pytest.mark.django_db
def test_serializer_requires_both_times(api_client, admin_user, camera):
    api_client.force_authenticate(user=admin_user)
    response = api_client.patch(
        f"/api/v1/cameras/{camera.id}/",
        {"person_alert_enabled": True, "person_alert_start": "22:00"},
        format="json",
    )
    assert response.status_code == 400
    response = api_client.patch(
        f"/api/v1/cameras/{camera.id}/",
        {"person_alert_enabled": True, "person_alert_start": "22:00", "person_alert_end": "06:00"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["person_alert_start"] == "22:00:00"


def test_tracker_counts_each_person_once():
    cam = "test-arrivals-cam"
    live_detection_tracker.forget(cam)
    person = ObjectDetection(x=100, y=100, width=80, height=200, confidence=0.9, class_name="person")
    live_detection_tracker.update(cam, [person], frame_h=720)
    assert live_detection_tracker.arrival_seq(cam) == 0  # one frame isn't enough
    live_detection_tracker.update(cam, [person], frame_h=720)
    assert live_detection_tracker.arrival_seq(cam) == 1
    live_detection_tracker.update(cam, [person], frame_h=720)
    assert live_detection_tracker.arrival_seq(cam) == 1  # standing still: no repeat
    live_detection_tracker.forget(cam)


@pytest.mark.django_db
def test_endpoint_key_and_user_auth(api_client, admin_user, camera, settings):
    settings.ALERT_PLAYER_KEY = "secret-key"
    camera.person_alert_enabled = True
    camera.save()

    assert api_client.get("/api/v1/cameras/person-alerts/").status_code == 401
    bad = api_client.get("/api/v1/cameras/person-alerts/", HTTP_X_ALERT_KEY="wrong")
    assert bad.status_code == 403

    ok = api_client.get("/api/v1/cameras/person-alerts/", HTTP_X_ALERT_KEY="secret-key")
    assert ok.status_code == 200
    assert [c["camera_id"] for c in ok.data["cameras"]] == [str(camera.id)]
    assert ok.data["cameras"][0]["active"] is True

    api_client.force_authenticate(user=admin_user)
    mine = api_client.get("/api/v1/cameras/person-alerts/")
    assert mine.status_code == 200
    assert len(mine.data["cameras"]) == 1
