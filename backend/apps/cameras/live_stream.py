"""Persistent RTSP readers that keep the latest JPEG in memory for fast live preview.

Opening RTSP on every HTTP /frame/ request costs ~1s. Each active camera URL gets
one background worker that grabs + JPEG-encodes frames; the API returns the
newest cached bytes (typically a few ms).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

IDLE_STOP_SEC = 45.0
RECONNECT_WAIT_SEC = 1.5
TARGET_FPS = 15
JPEG_QUALITY = 62


def prefer_fast_url(source_url: str) -> str:
    """Prefer Dahua/Hik substream (subtype=1) for live preview — lower res, lower latency."""
    parsed = urlparse((source_url or "").strip())
    if parsed.scheme != "rtsp":
        return source_url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    path = (parsed.path or "").lower()
    if "realmonitor" in path or "subtype" in query or "channel" in query:
        query["channel"] = query.get("channel") or "1"
        query["subtype"] = "1"
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/cam/realmonitor",
                "",
                urlencode(query),
                "",
            )
        )
    return source_url


class _StreamWorker:
    def __init__(self, url: str):
        self.url = url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame = None
        self._jpeg: bytes | None = None
        self._frame_at = 0.0
        self._last_access = time.monotonic()
        self._error = ""
        self._opened = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"live-rtsp-{abs(hash(self.url)) % 10_000:04d}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def touch(self) -> None:
        self._last_access = time.monotonic()

    @property
    def idle_for(self) -> float:
        return time.monotonic() - self._last_access

    def get_frame(self, max_age_sec: float = 2.5):
        self.touch()
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
            age = time.monotonic() - self._frame_at if self._frame is not None else 1e9
            err = self._error
        if frame is None:
            if err:
                raise RuntimeError(err)
            raise RuntimeError("Camera stream starting…")
        if age > max_age_sec:
            raise RuntimeError(err or "Camera stream stalled.")
        return frame

    def get_jpeg(self, max_age_sec: float = 2.5) -> bytes:
        """Encode lazily: only previews need JPEG, detection reads raw frames."""
        import cv2

        self.touch()
        with self._lock:
            if self._jpeg is None and self._frame is not None:
                ok_jpg, encoded = cv2.imencode(
                    ".jpg", self._frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                self._jpeg = encoded.tobytes() if ok_jpg else None
            jpeg = self._jpeg
            age = time.monotonic() - self._frame_at if jpeg is not None else 1e9
            err = self._error
        if jpeg is None:
            if err:
                raise RuntimeError(err)
            raise RuntimeError("Camera stream starting…")
        if age > max_age_sec:
            raise RuntimeError(err or "Camera stream stalled.")
        return jpeg

    def _run(self) -> None:
        from apps.cameras.rtsp_open import open_rtsp_capture

        capture = None
        interval = 1.0 / max(1, TARGET_FPS)
        while not self._stop.is_set():
            if self.idle_for > IDLE_STOP_SEC:
                logger.info("Live stream idle-stop %s", self.url.split("@")[-1])
                break
            try:
                if capture is None or not capture.isOpened():
                    if capture is not None:
                        try:
                            capture.release()
                        except Exception:
                            pass
                    capture = open_rtsp_capture(self.url)
                    if not capture.isOpened():
                        raise RuntimeError("Could not open stream.")
                    self._opened = True
                    with self._lock:
                        self._error = ""

                # Drop buffered frames so detection sees who just walked in,
                # not a frame still sitting in the decoder queue.
                grabbed = False
                for _ in range(6):
                    grabbed = bool(capture.grab())
                    if not grabbed:
                        break
                if grabbed:
                    ok, frame = capture.retrieve()
                else:
                    ok, frame = capture.read()
                if not ok or frame is None:
                    raise RuntimeError("Stream opened but no frame received.")

                with self._lock:
                    self._frame = frame
                    self._jpeg = None
                    self._frame_at = time.monotonic()
                    self._error = ""
                self._stop.wait(interval)
            except Exception as exc:
                self._opened = False
                with self._lock:
                    self._error = str(exc)
                    self._frame = None
                    self._jpeg = None
                    self._frame_at = 0.0
                if capture is not None:
                    try:
                        capture.release()
                    except Exception:
                        pass
                    capture = None
                self._stop.wait(RECONNECT_WAIT_SEC)

        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


class LiveStreamPool:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: dict[str, _StreamWorker] = {}

    def get_frame(self, source_url: str, *, prefer_fast: bool = True):
        worker = self._ensure_worker(self._url(source_url, prefer_fast))
        return self._wait(worker.get_frame)

    def get_jpeg(self, source_url: str, *, prefer_fast: bool = True) -> bytes:
        worker = self._ensure_worker(self._url(source_url, prefer_fast))
        return self._wait(worker.get_jpeg)

    def _url(self, source_url: str, prefer_fast: bool) -> str:
        url = prefer_fast_url(source_url) if prefer_fast else (source_url or "").strip()
        if not url:
            raise RuntimeError("Camera stream URL is not configured.")
        return url

    def _wait(self, getter):
        deadline = time.monotonic() + 12.0
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return getter()
            except RuntimeError as exc:
                last_err = exc
                msg = str(exc).lower()
                if "starting" in msg or "stalled" in msg:
                    time.sleep(0.04)
                    continue
                raise
        raise RuntimeError(str(last_err) if last_err else "Camera stream timed out.")

    def _ensure_worker(self, url: str) -> _StreamWorker:
        with self._lock:
            worker = self._workers.get(url)
            if worker is None or not (worker._thread and worker._thread.is_alive()):
                worker = _StreamWorker(url)
                self._workers[url] = worker
                worker.start()
            else:
                worker.touch()
            self._prune_locked()
            return worker

    def stop_url(self, source_url: str) -> None:
        fast = prefer_fast_url(source_url)
        with self._lock:
            for key in {source_url, fast}:
                worker = self._workers.pop(key, None)
                if worker is not None:
                    worker.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            items = []
            for url, worker in self._workers.items():
                with worker._lock:
                    age = time.monotonic() - worker._frame_at if worker._frame is not None else None
                    err = worker._error
                items.append(
                    {
                        "url": url.split("@")[-1],
                        "alive": bool(worker._thread and worker._thread.is_alive()),
                        "opened": worker._opened,
                        "frame_age_ms": None if age is None else int(age * 1000),
                        "error": err,
                    }
                )
            return {"workers": items, "count": len(items)}

    def _prune_locked(self) -> None:
        dead = [
            key
            for key, worker in self._workers.items()
            if not (worker._thread and worker._thread.is_alive())
        ]
        for key in dead:
            self._workers.pop(key, None)


live_stream_pool = LiveStreamPool()
