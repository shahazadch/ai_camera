"""Continuous automatic video recording for home / RTSP / IP Webcam cameras.

Each camera with ``auto_record=True`` gets one ffmpeg process that copies the
camera's own video stream into rolling MP4 segments under
MEDIA_ROOT/recordings/continuous/<camera>/ — no decode/re-encode for RTSP, so it
costs almost no CPU and plays back in real time. Finished segments become
READY RecordingJob rows for the Recordings page.

The same process also writes a "live rewind" buffer: 6-second HLS pieces in
MEDIA_ROOT/live/<camera>/ covering the last ``live_rewind_minutes`` (default
120), so the still-recording half hour can be watched up to ~6 s behind live.

Retention (``purge_recordings``) deletes continuous clips older than the
organization's ``recording_retention_days`` (default 3) and event clips older
than ``recording_event_retention_days`` (default 30).
"""

from __future__ import annotations

import collections
import logging
import shutil
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SEGMENT_SECONDS = 30 * 60
DEFAULT_RETENTION_DAYS = 3
DEFAULT_EVENT_RETENTION_DAYS = 30
DEFAULT_LIVE_REWIND_MINUTES = 120
LIVE_PIECE_SECONDS = 6
DEFAULT_QUALITY = "standard"  # "standard" = camera sub-stream, "high" = main stream
RETENTION_INTERVAL_SEC = 30 * 60


def _days(raw: dict, key: str, default: int) -> int:
    try:
        days = int(raw.get(key) or default)
    except (TypeError, ValueError):
        days = default
    return max(1, min(days, 365))


def _minutes(raw: dict, key: str, default: int) -> int:
    try:
        minutes = int(raw.get(key) or default)
    except (TypeError, ValueError):
        minutes = default
    return max(10, min(minutes, 360))


def live_dir(camera_id) -> Path:
    return Path(settings.MEDIA_ROOT) / "live" / str(camera_id).replace("-", "")[:8]


def live_playlist_url(camera_id) -> str | None:
    """URL of the rewind playlist when this camera is writing one."""
    playlist = live_dir(camera_id) / "index.m3u8"
    if not playlist.is_file():
        return None
    return f"{settings.MEDIA_URL.rstrip('/')}/live/{playlist.parent.name}/index.m3u8"


def recording_settings(organization) -> dict[str, Any]:
    raw = (getattr(organization, "settings", None) or {}) if organization is not None else {}
    quality = raw.get("recording_quality") or DEFAULT_QUALITY
    return {
        "live_rewind_minutes": _minutes(raw, "recording_live_rewind_minutes", DEFAULT_LIVE_REWIND_MINUTES),
        "retention_days": _days(raw, "recording_retention_days", DEFAULT_RETENTION_DAYS),
        "event_retention_days": _days(raw, "recording_event_retention_days", DEFAULT_EVENT_RETENTION_DAYS),
        "quality": quality if quality in {"standard", "high"} else DEFAULT_QUALITY,
    }


def _source_url(camera, quality: str) -> str:
    url = (camera.rtsp_url or "").strip()
    scheme = url.split("://", 1)[0].lower()
    if scheme in {"rtsp", "rtsps"} and quality == "standard":
        from apps.cameras.live_stream import prefer_fast_url

        return prefer_fast_url(url)
    if scheme in {"http", "https"}:
        from urllib.parse import urlparse

        if urlparse(url).path in {"", "/"}:
            return url.rstrip("/") + "/video"  # IP Webcam MJPEG feed
    return url


def _video_codec(url: str) -> str:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "10000000",
                "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", url,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""
    except Exception:
        return ""


class _CameraWorker:
    def __init__(self, camera_id: str, config: tuple):
        self.camera_id = camera_id
        self.config = config  # (url, quality, rewind minutes) — a change restarts the worker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        self.last_error = ""
        self.segments_written = 0
        self.recording = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"cam-recorder-{self.camera_id[:8]}", daemon=True
        )
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()  # ffmpeg finalizes the current segment on SIGTERM
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _load_camera(self):
        from apps.cameras.models import Camera

        return Camera.objects.filter(id=self.camera_id).select_related("site__organization").first()

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            camera = self._load_camera()
            if camera is None or not (camera.auto_record and camera.is_active and camera.rtsp_url):
                break
            started = time.monotonic()
            try:
                self._record(camera)
            except Exception as exc:
                self.last_error = str(exc)
                logger.info("Continuous recorder for %s stopped: %s", camera.name, exc)
            self.recording = False
            if self._stop.is_set():
                break
            self._set_camera_error(self.last_error)
            # A stream that ran for a while gets a quick retry; a dead one backs off.
            backoff = 5.0 if time.monotonic() - started > 120 else min(backoff * 2, 120.0)
            self._stop.wait(backoff)
        # No recorder, no rewind: drop the buffer so stale video isn't offered as live.
        shutil.rmtree(live_dir(self.camera_id), ignore_errors=True)

    def _record(self, camera) -> None:
        org_settings = recording_settings(camera.site.organization)
        url = _source_url(camera, org_settings["quality"])
        folder_rel = Path("recordings") / "continuous" / camera.id.hex[:8]
        folder = Path(settings.MEDIA_ROOT) / folder_rel
        folder.mkdir(parents=True, exist_ok=True)
        list_path = folder / f".segments-{int(time.time())}.csv"
        live = live_dir(camera.id)
        # A restart begins a new rewind buffer; mixing old pieces in breaks the playlist.
        shutil.rmtree(live, ignore_errors=True)
        live.mkdir(parents=True, exist_ok=True)

        scheme = url.split("://", 1)[0].lower()
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if scheme in {"rtsp", "rtsps"}:
            command += ["-rtsp_transport", "tcp", "-timeout", "10000000", "-i", url]
            video = ["-map", "0:v:0", "-c:v", "copy"]
            if _video_codec(url) == "hevc":
                video += ["-tag:v", "hvc1"]  # Safari / QuickTime need this tag for H.265
        else:
            # MJPEG / HTTP phone cams can't be copied into MP4: light H.264 encode.
            command += ["-i", url]
            video = [
                "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-r", "10",
                "-g", "20", "-vf", "scale=-2:'min(720,ih)'", "-pix_fmt", "yuv420p",
            ]
        # Output 1: the 30-minute files shown in Recordings.
        command += video + [
            "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-reset_timestamps", "1",
            "-segment_format_options", "movflags=+faststart",
            "-segment_list", str(list_path), "-segment_list_type", "csv",
            "-strftime", "1", str(folder / "%Y%m%d-%H%M%S.mp4"),
        ]
        # Output 2: the rolling live-rewind buffer (HLS, timestamped pieces).
        pieces = max(1, org_settings["live_rewind_minutes"] * 60 // LIVE_PIECE_SECONDS)
        command += video + [
            "-f", "hls", "-hls_time", str(LIVE_PIECE_SECONDS), "-hls_list_size", str(pieces),
            "-hls_delete_threshold", "2", "-hls_segment_type", "fmp4",
            "-hls_flags", "delete_segments+program_date_time+independent_segments+temp_file",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(live / "seg-%06d.m4s"), str(live / "index.m3u8"),
        ]

        self._stderr.clear()
        proc_started = timezone.now()
        self._proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        threading.Thread(target=self._drain_stderr, args=(self._proc,), daemon=True).start()
        self.recording = True
        self.last_error = ""
        self._set_camera_error("")
        offset = 0
        try:
            while self._proc.poll() is None and not self._stop.is_set():
                offset = self._register_segments(camera, list_path, folder_rel, proc_started, offset)
                self._stop.wait(2.0)
            if self._proc.poll() is None:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._register_segments(camera, list_path, folder_rel, proc_started, offset)
        finally:
            list_path.unlink(missing_ok=True)
        if not self._stop.is_set():
            detail = " ".join(self._stderr).strip()
            raise RuntimeError(detail[-300:] or f"ffmpeg exited ({self._proc.returncode})")

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr or []:
            if line.strip():
                self._stderr.append(line.strip())

    def _register_segments(self, camera, list_path: Path, folder_rel: Path, proc_started, offset: int) -> int:
        """Turn newly finished segments (listed by ffmpeg) into READY RecordingJob rows."""
        from apps.ops.models import RecordingJob, RecordingStatus, RecordingVendor

        if not list_path.is_file():
            return offset
        with list_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            lines = handle.readlines()
            # Only complete lines; a half-written one is picked up next time.
            if lines and not lines[-1].endswith("\n"):
                lines = lines[:-1]
            offset += sum(len(line.encode("utf-8")) for line in lines)
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) < 3:
                continue
            name, start, end = parts[0], float(parts[1]), float(parts[2])
            relative = (folder_rel / Path(name).name).as_posix()
            path = Path(settings.MEDIA_ROOT) / relative
            if not path.is_file() or path.stat().st_size < 1024:
                path.unlink(missing_ok=True)
                continue
            RecordingJob.objects.create(
                organization=camera.site.organization,
                camera=camera,
                vendor=RecordingVendor.RTSP,
                status=RecordingStatus.READY,
                started_at=proc_started + timedelta(seconds=start),
                ended_at=proc_started + timedelta(seconds=end),
                clip_key=relative,
                evidence_url=f"{settings.MEDIA_URL.rstrip('/')}/{relative}",
                details={
                    "mode": "continuous",
                    "seconds": round(end - start, 1),
                    "size_bytes": path.stat().st_size,
                },
            )
            self.segments_written += 1
        return offset

    def _set_camera_error(self, message: str) -> None:
        from apps.cameras.models import Camera

        camera = Camera.objects.filter(id=self.camera_id).first()
        if camera is None or (camera.metadata or {}).get("last_recording_error", "") == message:
            return
        camera.metadata = {**(camera.metadata or {}), "last_recording_error": message}
        camera.save(update_fields=["metadata", "updated_at"])


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
        if shutil.which("ffmpeg") is None:
            logger.warning("ffmpeg not found; continuous recording is disabled")
            return
        try:
            # Rewind buffers from before this start are stale; recorders rebuild them.
            shutil.rmtree(Path(settings.MEDIA_ROOT) / "live", ignore_errors=True)
            _close_interrupted_jobs()
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
            and shutil.which("ffmpeg")
        )
        config = None
        if should_run:
            org = getattr(camera.site, "organization", None) if getattr(camera, "site", None) else None
            org_settings = recording_settings(org)
            config = (camera.rtsp_url, org_settings["quality"], org_settings["live_rewind_minutes"])
        with self._lock:
            current = self._workers.get(camera_id)
            # Keep a healthy recorder running; restart only when its settings changed.
            if current is not None and current.config == config and current._thread and current._thread.is_alive():
                return
            worker = self._workers.pop(camera_id, None)
        if worker is not None:
            # Wait so the old worker's buffer cleanup can't hit the new one's files.
            worker.stop(wait=True)
        if should_run:
            worker = _CameraWorker(camera_id, config)
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
            .select_related("site__organization")
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
            items = [
                {
                    "camera_id": cid,
                    "recording": worker.recording,
                    "segments_written": worker.segments_written,
                    "last_error": worker.last_error,
                }
                for cid, worker in self._workers.items()
            ]
        return {"workers": items, "count": sum(1 for item in items if item["recording"])}


def _close_interrupted_jobs() -> None:
    """Rows left in RECORDING/REQUESTED by a restart will never finish."""
    from apps.ops.models import RecordingJob, RecordingStatus

    cutoff = timezone.now() - timedelta(minutes=5)
    RecordingJob.objects.filter(
        status__in=[RecordingStatus.RECORDING], updated_at__lt=cutoff
    ).update(status=RecordingStatus.FAILED, details={"error": "interrupted by restart"})


def purge_recordings(now=None) -> dict[str, int]:
    """
    Enforce each organization's retention and drop broken entries:
    - continuous clips older than ``retention_days``, event clips older than
      ``event_retention_days`` (rows + files)
    - READY rows whose local clip file is missing (old placeholder clips)
    - clip files nobody references that are older than the longest retention
    """
    from apps.ops.models import RecordingJob, RecordingStatus
    from apps.tenancy.models import Organization

    now = now or timezone.now()
    media = Path(settings.MEDIA_ROOT)
    deleted_jobs = deleted_files = 0
    max_days = DEFAULT_RETENTION_DAYS
    max_event_days = DEFAULT_EVENT_RETENTION_DAYS

    for org in Organization.objects.all():
        org_settings = recording_settings(org)
        days, event_days = org_settings["retention_days"], org_settings["event_retention_days"]
        max_days = max(max_days, days)
        max_event_days = max(max_event_days, event_days)
        candidates = RecordingJob.objects.filter(
            organization=org, created_at__lt=now - timedelta(days=min(days, event_days))
        )
        for job in candidates.iterator():
            continuous = (job.details or {}).get("mode") == "continuous"
            if job.created_at >= now - timedelta(days=days if continuous else event_days):
                continue
            if delete_clip(media, job.clip_key):
                deleted_files += 1
            job.delete()
            deleted_jobs += 1

    settled = now - timedelta(minutes=5)
    for job in RecordingJob.objects.filter(status=RecordingStatus.READY, created_at__lt=settled).only(
        "id", "clip_key"
    ).iterator():
        key = job.clip_key or ""
        if key and not key.startswith(("http://", "https://")) and not (media / key).is_file():
            job.delete()
            deleted_jobs += 1

    root = media / "recordings"
    if root.is_dir():
        referenced = set(
            RecordingJob.objects.exclude(clip_key="").values_list("clip_key", flat=True)
        )
        continuous_cutoff = (now - timedelta(days=max_days)).timestamp()
        event_cutoff = (now - timedelta(days=max_event_days)).timestamp()
        for path in root.rglob("*.mp4"):
            relative = path.relative_to(media).as_posix()
            cutoff = continuous_cutoff if relative.startswith("recordings/continuous/") else event_cutoff
            if relative not in referenced and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                deleted_files += 1
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                pass
    if deleted_jobs or deleted_files:
        logger.info("Recording retention: removed %s entries, %s files", deleted_jobs, deleted_files)
    return {"jobs": deleted_jobs, "files": deleted_files}


def delete_clip(media: Path, clip_key: str) -> bool:
    if not clip_key or clip_key.startswith(("http://", "https://")):
        return False
    path = (media / clip_key).resolve()
    if media.resolve() not in path.parents or not path.is_file():
        return False
    path.unlink(missing_ok=True)
    return True


def recordings_disk_usage() -> int:
    root = Path(settings.MEDIA_ROOT) / "recordings"
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*.mp4") if p.is_file())


def start_retention_loop() -> None:
    def _loop() -> None:
        from django.db import close_old_connections

        time.sleep(30)
        while True:
            close_old_connections()
            try:
                purge_recordings()
            except Exception:
                logger.warning("Recording retention pass failed", exc_info=True)
            time.sleep(RETENTION_INTERVAL_SEC)

    threading.Thread(target=_loop, name="recording-retention", daemon=True).start()


recorder_manager = ContinuousRecorderManager()
