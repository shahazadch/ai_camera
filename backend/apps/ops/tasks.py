"""Celery tasks for notifications, recordings, and SIEM forwarding."""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="apps.ops.tasks.send_notification_job", max_retries=0)
def send_notification_job(self, job_id: str):
    from apps.ops.models import NotificationDeliveryLog, NotificationJob, NotificationStatus
    from apps.ops.notifications.channels import deliver_channel
    from apps.ops.observability import observe_notification

    try:
        job = NotificationJob.objects.select_related("organization", "event").get(id=job_id)
    except NotificationJob.DoesNotExist:
        return {"ok": False, "error": "not_found"}

    if job.status in {NotificationStatus.SENT, NotificationStatus.DEAD}:
        return {"ok": True, "skipped": True, "status": job.status}

    if job.next_attempt_at and job.next_attempt_at > timezone.now():
        return {"ok": True, "deferred": True}

    if _rate_limited(job):
        job.next_attempt_at = timezone.now() + timedelta(seconds=30)
        job.last_error = "rate_limited"
        job.save(update_fields=["next_attempt_at", "last_error", "updated_at"])
        return {"ok": True, "rate_limited": True}

    # Claim atomically: the drain task can dispatch the same job twice while a
    # backlog is still queued, and only one worker may deliver it.
    claimed = NotificationJob.objects.filter(
        id=job.id, status__in=[NotificationStatus.QUEUED, NotificationStatus.FAILED]
    ).update(status=NotificationStatus.SENDING, updated_at=timezone.now())
    if not claimed:
        return {"ok": True, "skipped": True, "status": "claimed_elsewhere"}
    job.status = NotificationStatus.SENDING
    job.attempts += 1
    job.celery_task_id = getattr(self.request, "id", "") or ""
    job.save(update_fields=["status", "attempts", "celery_task_id", "updated_at"])

    result = deliver_channel(job.channel, job.destination, job.subject, job.body, job.payload)
    NotificationDeliveryLog.objects.create(
        job=job,
        attempt=job.attempts,
        success=result.success,
        provider_response=result.response,
        latency_ms=result.latency_ms,
        error=result.error,
    )
    observe_notification(result.success, job.channel)

    if result.success:
        job.status = NotificationStatus.SENT
        job.last_error = ""
        job.save(update_fields=["status", "last_error", "updated_at"])
        return {"ok": True, "status": "sent"}

    job.last_error = result.error or result.response or "delivery_failed"
    if job.attempts >= job.max_attempts:
        job.status = NotificationStatus.DEAD
        job.save(update_fields=["status", "last_error", "updated_at"])
        return {"ok": False, "status": "dead"}

    # Exponential backoff: 30s, 60s, 120s, ...
    delay = min(30 * (2 ** (job.attempts - 1)), 900)
    job.status = NotificationStatus.FAILED
    job.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    job.save(update_fields=["status", "last_error", "next_attempt_at", "updated_at"])
    return {"ok": False, "status": "failed", "retry_in": delay}


def _rate_limited(job) -> bool:
    from apps.ops.models import NotificationJob, NotificationStatus

    limit = settings.NOTIFICATION_RATE_LIMIT_PER_MINUTE
    key = job.rate_limit_key or f"{job.organization_id}:{job.channel}:{job.destination}"
    window_start = timezone.now() - timedelta(minutes=1)
    sent = NotificationJob.objects.filter(
        organization_id=job.organization_id,
        channel=job.channel,
        destination=job.destination,
        status=NotificationStatus.SENT,
        updated_at__gte=window_start,
    ).count()
    # Also count recent sends sharing the same rate_limit_key when set
    if job.rate_limit_key:
        sent = max(
            sent,
            NotificationJob.objects.filter(
                rate_limit_key=key,
                status=NotificationStatus.SENT,
                updated_at__gte=window_start,
            ).count(),
        )
    return sent >= limit


@shared_task(name="apps.ops.tasks.drain_notification_queue")
def drain_notification_queue(limit: int = 50):
    from apps.ops.models import NotificationJob, NotificationStatus

    now = timezone.now()
    qs = NotificationJob.objects.filter(
        status__in=[NotificationStatus.QUEUED, NotificationStatus.FAILED],
        next_attempt_at__lte=now,
    ).order_by("next_attempt_at")[:limit]
    dispatched = 0
    for job in qs:
        send_notification_job.delay(str(job.id))
        dispatched += 1
    return {"ok": True, "dispatched": dispatched}


@shared_task(name="apps.ops.tasks.process_recording_job")
def process_recording_job(job_id: str):
    from apps.ops.models import RecordingJob, RecordingStatus
    from apps.ops.recording import get_recording_adapter

    try:
        job = RecordingJob.objects.select_related("camera", "event").get(id=job_id)
    except RecordingJob.DoesNotExist:
        return {"ok": False, "error": "not_found"}

    if job.status == RecordingStatus.READY:
        return {"ok": True, "skipped": True}
    claimed = RecordingJob.objects.filter(
        id=job.id, status__in=[RecordingStatus.REQUESTED, RecordingStatus.FAILED]
    ).update(status=RecordingStatus.RECORDING, updated_at=timezone.now())
    if not claimed:
        return {"ok": True, "skipped": True, "status": "claimed_elsewhere"}

    adapter = get_recording_adapter(job.vendor)
    try:
        handle = adapter.start_recording(
            job.camera,
            reason=(job.event.event_type if job.event else "manual"),
            duration_sec=int((job.details or {}).get("duration_sec") or 10),
        )
        job.status = RecordingStatus.RECORDING
        job.started_at = timezone.now()
        job.details = {**(job.details or {}), "handle": handle.details, "session_id": handle.session_id}
        job.save(update_fields=["status", "started_at", "details", "updated_at"])

        handle = adapter.stop_recording(handle)
        clip = adapter.fetch_clip(handle, started_at=job.started_at)
        job.status = RecordingStatus.READY
        job.ended_at = timezone.now()
        job.evidence_url = clip.evidence_url
        job.clip_key = clip.clip_key
        job.details = {**(job.details or {}), "clip": clip.details}
        job.save(update_fields=["status", "ended_at", "evidence_url", "clip_key", "details", "updated_at"])
        return {"ok": True, "status": "ready", "clip_key": clip.clip_key}
    except Exception as exc:
        logger.exception("Recording job %s failed", job_id)
        job.status = RecordingStatus.FAILED
        job.details = {**(job.details or {}), "error": str(exc)}
        job.save(update_fields=["status", "details", "updated_at"])
        return {"ok": False, "error": str(exc)}


@shared_task(name="apps.ops.tasks.drain_recording_queue")
def drain_recording_queue(limit: int = 20):
    from apps.ops.models import RecordingJob, RecordingStatus

    qs = RecordingJob.objects.filter(status=RecordingStatus.REQUESTED).order_by("created_at")[:limit]
    dispatched = 0
    for job in qs:
        process_recording_job.delay(str(job.id))
        dispatched += 1
    return {"ok": True, "dispatched": dispatched}


@shared_task(name="apps.ops.tasks.forward_siem_event")
def forward_siem_event(job_id: str):
    from apps.ops.models import NotificationStatus, SIEMForwardJob
    from apps.ops.siem import forward_to_siem

    try:
        job = SIEMForwardJob.objects.select_related("event").get(id=job_id)
    except SIEMForwardJob.DoesNotExist:
        return {"ok": False, "error": "not_found"}

    if job.status == NotificationStatus.SENT:
        return {"ok": True, "skipped": True}

    job.attempts += 1
    job.status = NotificationStatus.SENDING
    job.save(update_fields=["attempts", "status", "updated_at"])

    ok, response = forward_to_siem(job.target, job.destination, job.payload)
    if ok:
        job.status = NotificationStatus.SENT
        job.last_error = ""
        job.payload = {**(job.payload or {}), "provider_response": response}
        job.save(update_fields=["status", "last_error", "payload", "updated_at"])
        return {"ok": True}
    job.last_error = response
    if job.attempts >= job.max_attempts:
        job.status = NotificationStatus.DEAD
    else:
        job.status = NotificationStatus.FAILED
    job.save(update_fields=["status", "last_error", "updated_at"])
    return {"ok": False, "error": response}


@shared_task(name="apps.ops.tasks.purge_expired_evidence")
def purge_expired_evidence(limit: int = 500):
    """Delete snapshot files + rows past expires_at; nothing else enforced the TTL."""
    from apps.ops.models import SnapshotObject
    from apps.ops.storage.evidence import get_evidence_storage

    storage = get_evidence_storage()
    expired = SnapshotObject.objects.filter(expires_at__lte=timezone.now()).order_by("expires_at")[:limit]
    purged = 0
    for snap in expired:
        try:
            storage.delete(snap.snapshot_key)
        except Exception as exc:
            logger.warning("Could not delete evidence %s: %s", snap.snapshot_key, exc)
            continue
        snap.delete()
        purged += 1
    return {"ok": True, "purged": purged}
