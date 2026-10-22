"""Continuous recording: segment registration, retention purge, settings API."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.cameras import continuous_recorder as rec
from apps.ops.models import RecordingJob, RecordingStatus


def _job(org, camera, clip_key, *, age_days=0.0, status=RecordingStatus.READY, mode="continuous"):
    job = RecordingJob.objects.create(
        organization=org,
        camera=camera,
        vendor="rtsp",
        status=status,
        clip_key=clip_key,
        details={"mode": mode} if mode else {},
    )
    RecordingJob.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(days=age_days))
    return job


@pytest.mark.django_db
def test_purge_respects_retention_days(org, camera, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org.settings = {"recording_retention_days": 3}
    org.save()
    folder = tmp_path / "recordings" / "continuous" / "cam"
    folder.mkdir(parents=True)
    for name in ("old.mp4", "new.mp4", "orphan.mp4"):
        (folder / name).write_bytes(b"x" * 2048)
    old = _job(org, camera, "recordings/continuous/cam/old.mp4", age_days=4)
    new = _job(org, camera, "recordings/continuous/cam/new.mp4", age_days=1)
    missing = _job(org, camera, "recordings/continuous/cam/gone.mp4", age_days=1)
    orphan = folder / "orphan.mp4"
    stamp = (timezone.now() - timedelta(days=5)).timestamp()
    import os

    os.utime(orphan, (stamp, stamp))

    result = rec.purge_recordings()

    assert not RecordingJob.objects.filter(id__in=[old.id, missing.id]).exists()
    assert RecordingJob.objects.filter(id=new.id).exists()
    assert not (folder / "old.mp4").exists()
    assert (folder / "new.mp4").exists()
    assert not orphan.exists()
    assert result["jobs"] == 2


def test_recording_settings_defaults_and_bounds():
    class Org:
        settings = {"recording_retention_days": "9999", "recording_quality": "ultra"}

    assert rec.recording_settings(None) == {
        "live_rewind_minutes": 120,
        "retention_days": 3,
        "event_retention_days": 30,
        "quality": "standard",
    }
    assert rec.recording_settings(Org()) == {
        "live_rewind_minutes": 120,
        "retention_days": 365,
        "event_retention_days": 30,
        "quality": "standard",
    }


@pytest.mark.django_db
def test_segments_from_ffmpeg_list_become_ready_jobs(org, camera, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    folder_rel = rec.Path("recordings/continuous/cam")
    folder = tmp_path / folder_rel
    folder.mkdir(parents=True)
    (folder / "20261022-100000.mp4").write_bytes(b"x" * 4096)
    (folder / "20261022-100100.mp4").write_bytes(b"x" * 10)  # broken, dropped
    list_path = folder / ".segments.csv"
    list_path.write_text("20261022-100000.mp4,0.000000,60.04\n20261022-100100.mp4,60.04,61.0\n2026")

    worker = rec._CameraWorker(str(camera.id), ("url", "standard"))
    started = timezone.now()
    offset = worker._register_segments(camera, list_path, folder_rel, started, 0)

    jobs = list(RecordingJob.objects.filter(camera=camera))
    assert len(jobs) == 1
    assert jobs[0].clip_key == "recordings/continuous/cam/20261022-100000.mp4"
    assert jobs[0].details["mode"] == "continuous"
    assert round((jobs[0].ended_at - jobs[0].started_at).total_seconds()) == 60
    # The half-written last line is left for the next pass.
    assert offset == len("20261022-100000.mp4,0.000000,60.04\n20261022-100100.mp4,60.04,61.0\n")


@pytest.mark.django_db
def test_sync_keeps_healthy_recorder_running(camera, settings):
    settings.CONTINUOUS_RECORDING = True
    manager = rec.ContinuousRecorderManager()
    with mock.patch.object(rec.shutil, "which", return_value="/usr/bin/ffmpeg"), mock.patch.object(
        rec._CameraWorker, "start"
    ) as start, mock.patch.object(rec._CameraWorker, "stop") as stop:
        manager.sync_camera(camera)
        worker = manager._workers[str(camera.id)]
        worker._thread = mock.Mock(is_alive=lambda: True)
        manager.sync_camera(camera)  # same settings → untouched
        assert start.call_count == 1
        assert stop.call_count == 0
        camera.rtsp_url = "rtsp://other/stream"
        manager.sync_camera(camera)  # URL changed → restarted
        assert start.call_count == 2
        assert stop.call_count == 1


@pytest.mark.django_db
def test_recording_settings_api(api_client, admin_user, org):
    api_client.force_authenticate(user=admin_user)
    data = api_client.get("/api/v1/recordings/settings/").data
    assert data["retention_days"] == 3
    with mock.patch("apps.cameras.continuous_recorder.purge_recordings"), mock.patch.object(
        rec.recorder_manager, "sync_from_db"
    ):
        bad = api_client.patch("/api/v1/recordings/settings/", {"retention_days": 0}, format="json")
        assert bad.status_code == 400
        assert (
            api_client.patch("/api/v1/recordings/settings/", {"event_retention_days": 400}, format="json").status_code
            == 400
        )
        assert (
            api_client.patch("/api/v1/recordings/settings/", {"live_rewind_minutes": 5}, format="json").status_code
            == 400
        )
        ok = api_client.patch(
            "/api/v1/recordings/settings/",
            {"retention_days": 7, "event_retention_days": 60, "quality": "high"},
            format="json",
        )
    assert ok.status_code == 200
    org.refresh_from_db()
    assert org.settings["recording_retention_days"] == 7
    assert org.settings["recording_quality"] == "high"
    assert org.settings["recording_event_retention_days"] == 60


@pytest.mark.django_db
def test_recording_camera_summary(api_client, admin_user, org, camera):
    _job(org, camera, "recordings/continuous/cam/a.mp4")
    _job(org, camera, "recordings/continuous/cam/b.mp4")
    _job(org, camera, "", status=RecordingStatus.FAILED)
    api_client.force_authenticate(user=admin_user)
    rows = api_client.get("/api/v1/recordings/cameras/").data
    assert [(r["camera_name"], r["clip_count"]) for r in rows] == [(camera.name, 2)]


@pytest.mark.django_db
def test_event_clips_kept_longer_than_continuous(org, camera, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org.settings = {"recording_retention_days": 3, "recording_event_retention_days": 30}
    org.save()
    old_continuous = _job(org, camera, "", age_days=5)
    old_event = _job(org, camera, "", age_days=5, mode=None)
    ancient_event = _job(org, camera, "", age_days=31, mode=None)
    rec.purge_recordings()
    remaining = set(RecordingJob.objects.values_list("id", flat=True))
    assert old_continuous.id not in remaining
    assert old_event.id in remaining
    assert ancient_event.id not in remaining


@pytest.mark.django_db
def test_bulk_delete_recordings(api_client, admin_user, org, camera, settings, tmp_path):
    from django.contrib.auth.models import User

    from apps.cameras.models import Camera
    from apps.sites.models import Site
    from apps.tenancy.models import Organization, Role, UserMembership

    settings.MEDIA_ROOT = tmp_path
    clip = tmp_path / "recordings" / "continuous" / "cam" / "a.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x" * 2048)
    mine = _job(org, camera, "recordings/continuous/cam/a.mp4")
    keep = _job(org, camera, "")
    other_org = Organization.objects.create(name="Other", slug="other")
    other_site = Site.objects.create(organization=other_org, name="Elsewhere")
    other_cam = Camera.objects.create(site=other_site, name="X", rtsp_url="rtsp://x")
    theirs = _job(other_org, other_cam, "")

    viewer = User.objects.create_user(username="viewer", password="pw-12345678")
    UserMembership.objects.create(user=viewer, organization=org, role=Role.VIEWER)
    api_client.force_authenticate(user=viewer)
    assert api_client.post("/api/v1/recordings/delete/", {"ids": [str(mine.id)]}, format="json").status_code == 403

    api_client.force_authenticate(user=admin_user)
    assert api_client.post("/api/v1/recordings/delete/", {"ids": ["nope"]}, format="json").status_code == 400
    result = api_client.post(
        "/api/v1/recordings/delete/", {"ids": [str(mine.id), str(theirs.id)]}, format="json"
    )
    assert result.data == {"deleted": 1}
    assert not clip.exists()
    remaining = set(RecordingJob.objects.values_list("id", flat=True))
    assert remaining == {keep.id, theirs.id}


@pytest.mark.django_db
def test_recorder_writes_files_and_rewind_buffer(camera, org, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    org.settings = {"recording_live_rewind_minutes": 60}
    org.save()
    camera.refresh_from_db()
    worker = rec._CameraWorker(str(camera.id), None)
    proc = mock.Mock(stderr=[], returncode=1)
    proc.poll.return_value = 1
    with mock.patch.object(rec, "_video_codec", return_value="hevc"), mock.patch.object(
        rec.subprocess, "Popen", return_value=proc
    ) as popen:
        with pytest.raises(RuntimeError):
            worker._record(camera)
    command = popen.call_args[0][0]
    assert command.count("-c:v") == 2  # one copy per output
    assert command[command.index("-segment_time") + 1] == "1800"
    assert command[command.index("-hls_list_size") + 1] == str(60 * 60 // 6)
    assert command[-1] == str(rec.live_dir(camera.id) / "index.m3u8")
    assert rec.live_dir(camera.id).is_dir()


def test_live_playlist_url(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cam_id = "ac0d68f2-6296-4c7f-a8d1-7e07cddb9ea4"
    assert rec.live_playlist_url(cam_id) is None
    rec.live_dir(cam_id).mkdir(parents=True)
    (rec.live_dir(cam_id) / "index.m3u8").write_text("#EXTM3U")
    assert rec.live_playlist_url(cam_id) == "/media/live/ac0d68f2/index.m3u8"
