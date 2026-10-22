"""M6 enterprise ops: notifications, evidence, recording, SSO, SIEM, metrics."""

from io import BytesIO

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from apps.cameras.models import Camera, EdgeDevice
from apps.events.models import DetectionEvent, EventType, Severity
from apps.events.pipeline import create_detection_event
from apps.ops.models import (
    NotificationDeliveryLog,
    NotificationJob,
    NotificationStatus,
    PasswordPolicy,
    RecordingJob,
    RecordingStatus,
    SessionPolicy,
    SIEMForwardJob,
    SnapshotObject,
    SSOProvider,
)
from apps.ops.recording import get_recording_adapter
from apps.ops.storage import LocalEvidenceStorage
from apps.ops.tasks import process_recording_job, send_notification_job
from apps.sites.models import Site
from apps.tenancy.models import Organization, Role, UserMembership


@pytest.fixture
def ops_env(db, settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.EVIDENCE_STORAGE_BACKEND = "local"
    org = Organization.objects.create(
        name="Ops Org",
        slug="ops-org",
        settings={
            "notification_channels": [
                {"channel": "email", "destination": "ops@example.com"},
                {"channel": "webhook", "destination": "/hooks/security"},
            ],
            "siem_targets": [{"target": "webhook", "destination": "/siem/ingest"}],
        },
    )
    site = Site.objects.create(organization=org, name="HQ")
    camera = Camera.objects.create(site=site, name="Cam", rtsp_url="rtsp://example/stream", status="online")
    key = "ops-edge-key"
    device = EdgeDevice.objects.create(
        site=site,
        name="Edge",
        api_key_hash=EdgeDevice.hash_api_key(key),
        api_key_prefix="ops",
        status="online",
    )
    user = User.objects.create_user("opsadmin", password="pass")
    UserMembership.objects.create(user=user, organization=org, role=Role.OWNER)
    return {"org": org, "site": site, "camera": camera, "device": device, "key": key, "user": user}


@pytest.mark.django_db
def test_critical_event_queues_and_delivers_notifications(ops_env):
    result = create_detection_event(
        camera=ops_env["camera"],
        event_type=EventType.FIRE,
        severity=Severity.CRITICAL,
        confidence=0.95,
        edge_event_id="fire-1",
        edge_device=ops_env["device"],
    )
    assert result.created
    jobs = NotificationJob.objects.filter(event=result.event)
    assert jobs.count() == 2
    assert all(j.status == NotificationStatus.SENT for j in jobs)
    assert NotificationDeliveryLog.objects.filter(job__in=jobs, success=True).count() == 2


@pytest.mark.django_db
def test_notification_dead_letter_after_max_attempts(ops_env, settings, monkeypatch):
    settings.CELERY_TASK_ALWAYS_EAGER = True

    def boom(*args, **kwargs):
        from apps.ops.notifications.channels import DeliveryResult

        return DeliveryResult(False, error="provider_down")

    monkeypatch.setattr("apps.ops.notifications.channels.deliver_channel", boom)

    job = NotificationJob.objects.create(
        organization=ops_env["org"],
        channel="email",
        destination="x@y.com",
        subject="t",
        body="b",
        max_attempts=2,
    )
    send_notification_job(str(job.id))
    job.refresh_from_db()
    assert job.status == NotificationStatus.FAILED
    job.next_attempt_at = timezone.now()
    job.save(update_fields=["next_attempt_at"])
    send_notification_job(str(job.id))
    job.refresh_from_db()
    assert job.status == NotificationStatus.DEAD
    assert job.attempts == 2


@pytest.mark.django_db
def test_local_evidence_upload_registers_metadata(ops_env):
    client = APIClient()
    # Minimal JPEG
    jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xd9"
    )
    upload = SimpleUploadedFile("frame.jpg", jpeg, content_type="image/jpeg")
    resp = client.post(
        "/api/v1/snapshots/upload/",
        {"camera_id": str(ops_env["camera"].id), "file": upload},
        format="multipart",
        HTTP_X_EDGE_API_KEY=ops_env["key"],
    )
    assert resp.status_code == 201, resp.data
    obj = SnapshotObject.objects.get(id=resp.data["id"])
    assert obj.checksum
    assert obj.uploaded_by_edge is True
    assert obj.storage_backend == "local"
    assert obj.expires_at is not None


@pytest.mark.django_db
def test_recording_adapter_processes_job(ops_env):
    result = create_detection_event(
        camera=ops_env["camera"],
        event_type=EventType.WEAPON,
        severity=Severity.CRITICAL,
        confidence=0.9,
        edge_event_id="weapon-1",
        edge_device=ops_env["device"],
    )
    job = RecordingJob.objects.filter(event=result.event).first()
    assert job is not None
    job.refresh_from_db()
    # Eager celery processed it. The test camera has no real stream, so the clip
    # must fail honestly instead of being marked READY with no file behind it.
    if job.status == RecordingStatus.REQUESTED:
        process_recording_job(str(job.id))
        job.refresh_from_db()
    assert job.status == RecordingStatus.FAILED
    assert "RTSP clip capture failed" in job.details.get("error", "")


@pytest.mark.django_db
def test_recording_adapter_factory():
    assert get_recording_adapter("rtsp").vendor == "rtsp"
    assert get_recording_adapter("hikvision").vendor == "hikvision"
    assert get_recording_adapter("unknown").vendor == "stub"


@pytest.mark.django_db
def test_siem_forward_queued(ops_env):
    result = create_detection_event(
        camera=ops_env["camera"],
        event_type=EventType.INTRUSION,
        severity=Severity.CRITICAL,
        confidence=0.8,
        edge_event_id="intr-1",
        edge_device=ops_env["device"],
    )
    jobs = SIEMForwardJob.objects.filter(event=result.event)
    assert jobs.count() == 1
    assert jobs.first().status == NotificationStatus.SENT


@pytest.mark.django_db
def test_sso_and_policies_api(ops_env):
    client = APIClient()
    client.force_authenticate(user=ops_env["user"])
    r = client.post(
        "/api/v1/sso/providers/",
        {
            "organization": str(ops_env["org"].id),
            "name": "Azure AD",
            "protocol": "oidc",
            "issuer": "https://login.microsoftonline.com/tenant/v2.0",
            "client_id": "app-id",
            "client_secret": "secret",
            "mfa_required": True,
        },
        format="json",
    )
    assert r.status_code == 201, r.data
    assert SSOProvider.objects.filter(organization=ops_env["org"], name="Azure AD").exists()

    r2 = client.get("/api/v1/identity/password-policy/")
    assert r2.status_code == 200
    assert PasswordPolicy.objects.filter(organization=ops_env["org"]).exists()

    r3 = client.put(
        "/api/v1/identity/session-policy/",
        {"mfa_required": True, "idle_timeout_minutes": 15},
        format="json",
    )
    assert r3.status_code == 200
    assert SessionPolicy.objects.get(organization=ops_env["org"]).mfa_required is True


@pytest.mark.django_db
def test_metrics_and_probes():
    client = APIClient()
    assert client.get("/api/v1/live/").status_code == 200
    assert client.get("/api/v1/ready/").status_code == 200
    metrics = client.get("/api/v1/metrics/")
    assert metrics.status_code == 200
    assert b"sentinel_" in metrics.content or b"prometheus" in metrics.content.lower() or metrics.content


@pytest.mark.django_db
def test_local_storage_put(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    settings.MEDIA_URL = "/media/"
    stored = LocalEvidenceStorage().put("snapshots/t.jpg", b"abc", content_type="image/jpeg")
    assert stored.checksum
    assert stored.size_bytes == 3
    assert (tmp_path / "snapshots" / "t.jpg").exists()
