"""Recording adapters for NVR / camera vendors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RecordingHandle:
    session_id: str
    vendor: str
    status: str
    details: dict[str, Any]


@dataclass
class ClipResult:
    clip_key: str
    evidence_url: str
    details: dict[str, Any]


class RecordingAdapter(ABC):
    vendor: str = "stub"

    @abstractmethod
    def start_recording(self, camera, *, reason: str = "", duration_sec: int = 30) -> RecordingHandle:
        raise NotImplementedError

    @abstractmethod
    def stop_recording(self, handle: RecordingHandle) -> RecordingHandle:
        raise NotImplementedError

    @abstractmethod
    def fetch_clip(self, handle: RecordingHandle, *, started_at: datetime | None = None) -> ClipResult:
        raise NotImplementedError


class StubRecordingAdapter(RecordingAdapter):
    vendor = "stub"

    def start_recording(self, camera, *, reason: str = "", duration_sec: int = 30) -> RecordingHandle:
        return RecordingHandle(
            session_id=f"stub-{camera.id}",
            vendor=self.vendor,
            status="recording",
            details={"reason": reason, "duration_sec": duration_sec},
        )

    def stop_recording(self, handle: RecordingHandle) -> RecordingHandle:
        handle.status = "stopped"
        return handle

    def fetch_clip(self, handle: RecordingHandle, *, started_at: datetime | None = None) -> ClipResult:
        key = f"recordings/{handle.session_id}.mp4"
        return ClipResult(clip_key=key, evidence_url=f"/media/{key}", details={"stub": True})


class RTSPRecordingAdapter(RecordingAdapter):
    """Captures a short clip from camera.rtsp_url when OpenCV is available."""

    vendor = "rtsp"

    def start_recording(self, camera, *, reason: str = "", duration_sec: int = 30) -> RecordingHandle:
        return RecordingHandle(
            session_id=f"rtsp-{camera.id}-{int(datetime.now(dt_timezone.utc).timestamp())}",
            vendor=self.vendor,
            status="recording",
            details={"rtsp_url": camera.rtsp_url, "reason": reason, "duration_sec": duration_sec},
        )

    def stop_recording(self, handle: RecordingHandle) -> RecordingHandle:
        handle.status = "stopped"
        return handle

    def fetch_clip(self, handle: RecordingHandle, *, started_at: datetime | None = None) -> ClipResult:
        rtsp = (handle.details or {}).get("rtsp_url") or ""
        duration = int((handle.details or {}).get("duration_sec") or 5)
        key = f"recordings/{handle.session_id}.mp4"
        try:
            import cv2
            from pathlib import Path
            from django.conf import settings

            out = Path(settings.MEDIA_ROOT) / key
            out.parent.mkdir(parents=True, exist_ok=True)
            if not rtsp:
                raise RuntimeError("No RTSP URL")
            cap = cv2.VideoCapture(rtsp)
            if not cap.isOpened():
                raise RuntimeError("Unable to open RTSP stream")
            fps = cap.get(cv2.CAP_PROP_FPS) or 5
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 640)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 480)
            writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
            frames = int(max(1, fps) * min(duration, 10))
            for _ in range(frames):
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
            writer.release()
            cap.release()
            return ClipResult(clip_key=key, evidence_url=f"/media/{key}", details={"frames": frames})
        except Exception as exc:
            # Fail the job instead of marking a clip READY that was never written.
            raise RuntimeError(f"RTSP clip capture failed: {exc}") from exc


class VendorStubAdapter(RecordingAdapter):
    """ONVIF / Hikvision / Dahua / Milestone / Axis — interface ready, vendor SDK pending."""

    def __init__(self, vendor: str):
        self.vendor = vendor

    def start_recording(self, camera, *, reason: str = "", duration_sec: int = 30) -> RecordingHandle:
        return RecordingHandle(
            session_id=f"{self.vendor}-{camera.id}",
            vendor=self.vendor,
            status="recording",
            details={
                "reason": reason,
                "duration_sec": duration_sec,
                "integration": "sdk_pending",
                "camera": camera.name,
            },
        )

    def stop_recording(self, handle: RecordingHandle) -> RecordingHandle:
        handle.status = "stopped"
        return handle

    def fetch_clip(self, handle: RecordingHandle, *, started_at: datetime | None = None) -> ClipResult:
        key = f"recordings/{handle.session_id}.mp4"
        return ClipResult(
            clip_key=key,
            evidence_url=f"/media/{key}",
            details={"vendor": self.vendor, "sdk_pending": True},
        )


def get_recording_adapter(vendor: str) -> RecordingAdapter:
    vendor = (vendor or "stub").lower()
    if vendor == "rtsp":
        return RTSPRecordingAdapter()
    if vendor in {"onvif", "hikvision", "dahua", "milestone", "axis"}:
        return VendorStubAdapter(vendor)
    return StubRecordingAdapter()
