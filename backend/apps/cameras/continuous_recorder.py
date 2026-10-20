"""Continuous automatic video recording for home / RTSP / IP Webcam cameras.

Each camera with ``auto_record=True`` gets a daemon worker that writes
rolling MP4 segments under MEDIA_ROOT/recordings/ and registers
RecordingJob rows so they show up in the Recordings UI.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SEGMENT_SECONDS = 60
TARGET_FPS = 8


class _CameraWorker:
    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.segments_written = 0
        self.recording = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"cam-recorder-{self.camera_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            camera = self._load_camera()
            if camera is None:
                logger.info("Continuous recorder stop for %s: camera deleted", self.camera_id)
                self._stop.set()
                break
            try:
                self._record_one_segment()
            except Exception as exc:
                self.last_error = str(exc)
                self.recording = False
                logger.info("Continuous recorder pause for %s: %s", self.camera_id, exc)
                # Back off so a dead stream does not spin the CPU.
                self._stop.wait(8)

    def _load_camera(self):
        from apps.cameras.models import Camera

        return (
            Camera.objects.filter(id=self.camera_id)
            .select_related("site", "site__organization")
            .first()
        )

    def _read_frame(self, source_url: str, capture):
        """Return (frame, width, height, capture) — keep RTSP capture open when possible."""
        from urllib.parse import urlparse

        from apps.cameras.views import _capture_frame

        scheme = urlparse(source_url).scheme.lower()
        if scheme in {"http", "https"}:
            frame, width, height, _ = _capture_frame(source_url)
            return frame, width, height, None

        from apps.cameras.rtsp_open import open_rtsp_capture

        if capture is None or not capture.isOpened():
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            capture = open_rtsp_capture(source_url)
            if not capture.isOpened():
                capture.release()
                raise RuntimeError("Could not open RTSP stream for recording.")
        ok, frame = capture.read()
        if not ok or frame is None:
            try:
                capture.release()
            except Exception:
                pass
            raise RuntimeError("RTSP stream opened but no frame received.")
        height, width = frame.shape[:2]
        return frame, width, height, capture

    def _record_one_segment(self) -> None:
        camera = self._load_camera()
        if camera is None:
            self._stop.set()
            return
        if not camera.auto_record or not camera.is_active or not camera.rtsp_url:
            # sync_camera starts a fresh worker if recording is re-enabled.
            self._stop.set()
            return

        from apps.ops.models import RecordingJob, RecordingStatus, RecordingVendor

        started_at = timezone.now()
        stamp = started_at.strftime("%Y/%m/%d")
        clip_name = f"{camera.id.hex[:8]}-{started_at.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.mp4"
        relative = Path("recordings") / "continuous" / stamp / clip_name
        destination = Path(settings.MEDIA_ROOT) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)

        job = RecordingJob.objects.create(
            organization=camera.site.organization,
            camera=camera,
            event=None,
            vendor=RecordingVendor.RTSP,
            status=RecordingStatus.RECORDING,
            started_at=started_at,
            details={
                "mode": "continuous",
                "segment_seconds": SEGMENT_SECONDS,
                "source": camera.rtsp_url,
            },
        )

        self.recording = True
        self.last_error = ""
        frames_written = 0
        writer = None
        capture = None
        size = None

        try:
            import cv2

            deadline = time.monotonic() + SEGMENT_SECONDS
            frame_interval = 1.0 / max(1, TARGET_FPS)
            next_frame_at = time.monotonic()

            while not self._stop.is_set() and time.monotonic() < deadline:
                now = time.monotonic()
                if now < next_frame_at:
                    time.sleep(min(0.05, next_frame_at - now))
                    continue
                next_frame_at = now + frame_interval

                try:
                    frame, width, height, capture = self._read_frame(camera.rtsp_url, capture)
                except RuntimeError as exc:
                    self.last_error = str(exc)
                    capture = None
                    time.sleep(1.0)
                    continue

                if writer is None:
                    size = (int(width), int(height))
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    writer = cv2.VideoWriter(str(destination), fourcc, float(TARGET_FPS), size)
                    if not writer.isOpened():
                        raise RuntimeError("Unable to open video writer for continuous recording.")

                if (frame.shape[1], frame.shape[0]) != size:
                    frame = cv2.resize(frame, size)
                writer.write(frame)
                frames_written += 1

            if writer is not None:
                writer.release()
                writer = None

            if frames_written <= 0:
                raise RuntimeError(self.last_error or "No frames captured for recording segment.")

            evidence_url = f"{settings.MEDIA_URL.rstrip('/')}/{relative.as_posix()}"
            job.status = RecordingStatus.READY
            job.ended_at = timezone.now()
            job.clip_key = relative.as_posix()
            job.evidence_url = evidence_url
            job.details = {
                **(job.details or {}),
                "frames": frames_written,
                "fps": TARGET_FPS,
                "size": list(size) if size else None,
            }
            job.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "clip_key",
                    "evidence_url",
                    "details",
                    "updated_at",
                ]
            )
            self.segments_written += 1
            camera.last_seen_at = timezone.now()
            camera.status = "online"
            camera.metadata = {
                **(camera.metadata or {}),
                "auto_record": True,
                "last_recording_segment": relative.as_posix(),
                "last_recording_at": timezone.now().isoformat(),
                "last_recording_error": "",
            }
            camera.save(update_fields=["last_seen_at", "status", "metadata", "updated_at"])
        except Exception as exc:
            if writer is not None:
                try:
                    writer.release()
                except Exception:
                    pass
            if destination.exists() and destination.stat().st_size < 1024:
                destination.unlink(missing_ok=True)
            job.status = RecordingStatus.FAILED
            job.ended_at = timezone.now()
            job.details = {**(job.details or {}), "error": str(exc), "frames": frames_written}
            job.save(update_fields=["status", "ended_at", "details", "updated_at"])
            camera.metadata = {
                **(camera.metadata or {}),
                "last_recording_error": str(exc),
            }
            camera.save(update_fields=["metadata", "updated_at"])
            raise
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            self.recording = False


class ContinuousRecorderManager:
    """Process-wide registry of per-camera recording workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, _CameraWorker] = {}
        self._started = False

    def ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        # Defer DB sync so migrations can finish during manage.py commands.
        threading.Thread(target=self._boot_sync, name="cam-recorder-boot", daemon=True).start()

    def _boot_sync(self) -> None:
        time.sleep(1.5)
        try:
            self.sync_from_db()
        except Exception as exc:
            logger.warning("Continuous recorder boot sync failed: %s", exc)

    def sync_camera(self, camera) -> None:
        camera_id = str(camera.id)
        should_run = bool(
            settings.CONTINUOUS_RECORDING
            and camera.auto_record
            and camera.is_active
            and camera.rtsp_url
        )
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        # Always restart so URL / flag changes apply; never block the API on RTSP.
        if worker is not None:
            worker.stop(wait=False)
        if should_run:
            worker = _CameraWorker(camera_id)
            with self._lock:
                self._workers[camera_id] = worker
            worker.start()

    def stop_camera(self, camera_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(str(camera_id), None)
        if worker is not None:
            worker.stop()

    def sync_from_db(self) -> dict[str, Any]:
        from apps.cameras.models import Camera

        cameras = list(
            Camera.objects.filter(auto_record=True, is_active=True)
            .exclude(rtsp_url="")
            .only("id", "auto_record", "is_active", "rtsp_url")
        )
        wanted = {str(c.id) for c in cameras}
        for camera in cameras:
            self.sync_camera(camera)
        with self._lock:
            stale = [cid for cid in self._workers if cid not in wanted]
        for cid in stale:
            self.stop_camera(cid)
        return {"active": len(wanted), "workers": len(self._workers)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            items = []
            for cid, worker in self._workers.items():
                items.append(
                    {
                        "camera_id": cid,
                        "recording": worker.recording,
                        "segments_written": worker.segments_written,
                        "last_error": worker.last_error,
                    }
                )
            return {"workers": items, "count": len(items)}


recorder_manager = ContinuousRecorderManager()
