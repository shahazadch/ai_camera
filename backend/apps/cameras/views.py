from datetime import timedelta

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.permissions import TenantQuerysetMixin, require_role
from apps.events.models import DetectionEvent
from apps.tenancy.models import Role

from .authentication import EdgeAPIKeyAuthentication, EdgeServiceUser
from .models import Camera, EdgeDevice, VisionCapture
from .serializers import (
    CameraSerializer,
    EdgeCameraSerializer,
    EdgeDeviceCreateResponseSerializer,
    EdgeDeviceSerializer,
    EdgeHeartbeatSerializer,
    VisionCaptureSerializer,
)


def _sync_camera_recorder(camera) -> None:
    """Kick recorder sync off the request thread so Save stays fast."""
    import threading

    camera_id = str(camera.id)

    def _run() -> None:
        try:
            from apps.cameras.continuous_recorder import recorder_manager
            from apps.cameras.models import Camera

            fresh = Camera.objects.filter(id=camera_id).first()
            if fresh is not None:
                recorder_manager.sync_camera(fresh)
        except Exception:
            pass

    threading.Thread(target=_run, name=f"cam-sync-{camera_id[:8]}", daemon=True).start()


def _stop_camera_recorder(camera_id, rtsp_url: str = "") -> None:
    import threading

    cid = str(camera_id)
    url = rtsp_url or ""

    def _run() -> None:
        try:
            from apps.cameras.continuous_recorder import recorder_manager

            recorder_manager.stop_camera(cid)
        except Exception:
            pass
        if url:
            try:
                from apps.cameras.live_stream import live_stream_pool

                live_stream_pool.stop_url(url)
            except Exception:
                pass

    threading.Thread(target=_run, name=f"cam-stop-{cid[:8]}", daemon=True).start()


class CameraListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = CameraSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "site__organization"
    filterset_fields = ("site", "status", "is_active", "auto_record")
    search_fields = ("name",)

    def get_queryset(self):
        return Camera.objects.filter(site__organization_id__in=self.get_user_organizations()).select_related(
            "site", "zone"
        )

    def perform_create(self, serializer):
        site = serializer.validated_data["site"]
        require_role(self.request.user, site.organization, [Role.OWNER, Role.ADMIN])
        camera = serializer.save()
        _sync_camera_recorder(camera)


class CameraDetailView(TenantQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CameraSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "site__organization"

    def get_queryset(self):
        return Camera.objects.filter(site__organization_id__in=self.get_user_organizations())

    def perform_update(self, serializer):
        require_role(self.request.user, serializer.instance.site.organization, [Role.OWNER, Role.ADMIN])
        camera = serializer.save()
        _sync_camera_recorder(camera)

    def perform_destroy(self, instance):
        require_role(self.request.user, instance.site.organization, [Role.OWNER, Role.ADMIN])
        camera_id = instance.id
        _stop_camera_recorder(camera_id, instance.rtsp_url or "")
        instance.delete()


def _user_camera(request, pk):
    organization_ids = request.user.memberships.values_list("organization_id", flat=True)
    return (
        Camera.objects.filter(id=pk, site__organization_id__in=organization_ids)
        .select_related("site", "zone")
        .first()
    )


class CameraConnectionTestView(APIView):
    """Open the configured RTSP / HTTP camera source and read one frame."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        camera = _user_camera(request, pk)
        if camera is None:
            return Response({"detail": "Camera not found."}, status=status.HTTP_404_NOT_FOUND)
        require_role(request.user, camera.site.organization, [Role.OWNER, Role.ADMIN])
        if not camera.rtsp_url:
            return Response(
                {"ok": False, "detail": "Camera stream URL is not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            frame, width, height, resolved_url = _capture_frame(camera.rtsp_url)
        except RuntimeError as exc:
            camera.status = "error"
            camera.metadata = {**camera.metadata, "last_connection_error": str(exc)}
            camera.save(update_fields=["status", "metadata", "updated_at"])
            return Response({"ok": False, "detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        camera.status = "online"
        camera.last_seen_at = timezone.now()
        camera.metadata = {
            **camera.metadata,
            "frame_width": width,
            "frame_height": height,
            "resolved_stream_url": resolved_url,
            "last_connection_error": "",
        }
        camera.save(update_fields=["status", "last_seen_at", "metadata", "updated_at"])
        return Response({"ok": True, "width": width, "height": height, "resolved_url": resolved_url})


class CameraFrameView(APIView):
    """Return a current JPEG frame with tracking overlays and optional live detection."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        camera = _user_camera(request, pk)
        if camera is None:
            return Response({"detail": "Camera not found."}, status=status.HTTP_404_NOT_FOUND)
        if not camera.rtsp_url:
            return Response(
                {"detail": "Camera stream URL is not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        detect = request.query_params.get("detect") in {"1", "true", "yes"}
        verify = {"verified": 0, "unknown": 0, "blacklist": 0, "labels": [], "issues": [], "dangers": 0}
        # Keep the fast person loop alive while this camera is on screen.
        try:
            from .detection import live_person_watcher

            if camera.rtsp_url:
                live_person_watcher.touch(str(camera.id), camera.rtsp_url)
        except Exception:
            pass

        # Fast paint path: latest frame + sticky boxes/names (no YOLO). Detect loop fills sticky.
        if not detect:
            try:
                import cv2

                from .detection import live_detection_tracker, opencv_lock
                from .live_stream import live_stream_pool

                sticky, sticky_labels, class_names = live_detection_tracker.peek(str(camera.id))
                # Live overlay: humans + mobile phones in use only.
                sticky = [
                    d for d in sticky if d.class_name in {"person", "cell phone"}
                ]
                if sticky:
                    frame = live_stream_pool.get_frame(camera.rtsp_url, prefer_fast=True)
                    _draw_detection_boxes(frame, sticky, sticky_labels)
                    with opencv_lock:
                        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 68])
                    if not ok:
                        raise RuntimeError("Unable to encode camera frame.")
                    jpeg = encoded.tobytes()
                else:
                    jpeg = live_stream_pool.get_jpeg(camera.rtsp_url, prefer_fast=True)

                count = sum(1 for d in sticky if d.class_name == "person")
                labels = []
                if any(d.class_name == "person" for d in sticky):
                    labels.append("person")
                if any(d.class_name == "cell phone" for d in sticky):
                    labels.append("MOBILE IN USE")
                # Expose last known identities so UI keeps showing who is on camera.
                v_ok = v_unknown = v_threat = 0
                v_labels: list[str] = []
                v_announce = ""
                v_issue = ""
                for info in (sticky_labels or {}).values():
                    if not isinstance(info, dict):
                        if info and info != "Unknown":
                            v_labels.append(str(info))
                            v_ok += 1
                        continue
                    status = info.get("status") or "unknown"
                    text = str(info.get("name") or info.get("label") or "").strip()
                    if text:
                        v_labels.append(text)
                    if status == "verified":
                        v_ok += 1
                        if not v_announce and info.get("name"):
                            v_announce = str(info["name"])
                    elif status == "blacklist":
                        v_threat += 1
                        if not v_announce:
                            v_announce = str(info.get("announce") or text or "threat")
                    elif status == "unknown":
                        v_unknown += 1
                        if not v_announce:
                            v_announce = "UNKNOWN person detected"
                    elif status == "pending" and info.get("issue") and not v_issue:
                        v_issue = str(info["issue"])
                for name in v_labels:
                    if name and name not in labels:
                        labels.insert(0, name)
            except RuntimeError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

            response = HttpResponse(jpeg, content_type="image/jpeg")
            response["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response["X-Detections"] = str(count)
            response["X-Objects"] = ", ".join(labels[:20])
            response["X-Verify-Ok"] = str(v_ok)
            response["X-Verify-Unknown"] = str(v_unknown)
            response["X-Verify-Threat"] = str(v_threat)
            response["X-Verify-Danger"] = "0"
            response["X-Verify-Labels"] = ", ".join(v_labels[:16])
            response["X-Verify-Issue"] = v_issue[:200]
            response["X-Verify-Announce"] = v_announce[:120]
            response["X-Stairs-Alert"] = (
                "1" if live_detection_tracker.stairs_hot(str(camera.id)) else "0"
            )
            response["Access-Control-Expose-Headers"] = (
                "X-Detections, X-Objects, X-Verify-Ok, X-Verify-Unknown, "
                "X-Verify-Threat, X-Verify-Danger, X-Verify-Labels, X-Verify-Issue, "
                "X-Verify-Announce, X-Stairs-Alert"
            )
            return response

        try:
            frame, _, _, _ = _capture_frame(camera.rtsp_url, prefer_fast=True)
            count, labels, verify = _detect_and_record(frame, camera)
            _draw_tracking_overlays(frame, camera)
            import cv2

            from .detection import live_detection_tracker, opencv_lock

            with opencv_lock:
                ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 68])
            if not ok:
                raise RuntimeError("Unable to encode camera frame.")
            jpeg = encoded.tobytes()
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        response = HttpResponse(jpeg, content_type="image/jpeg")
        response["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response["X-Detections"] = str(count)
        response["X-Objects"] = ", ".join(labels[:20])
        response["X-Verify-Ok"] = str(verify.get("verified", 0))
        response["X-Verify-Unknown"] = str(verify.get("unknown", 0))
        response["X-Verify-Threat"] = str(verify.get("blacklist", 0))
        response["X-Verify-Danger"] = str(verify.get("dangers", 0))
        response["X-Verify-Labels"] = ", ".join((verify.get("labels") or [])[:16])
        response["X-Verify-Issue"] = " | ".join((verify.get("issues") or [])[:3])
        response["X-Verify-Announce"] = str(verify.get("announce") or "")[:120]
        response["X-Stairs-Alert"] = (
            "1" if live_detection_tracker.stairs_hot(str(camera.id)) else "0"
        )
        response["Access-Control-Expose-Headers"] = (
            "X-Detections, X-Objects, X-Verify-Ok, X-Verify-Unknown, "
            "X-Verify-Threat, X-Verify-Danger, X-Verify-Labels, X-Verify-Issue, "
            "X-Verify-Announce, X-Stairs-Alert"
        )
        return response


class CameraMonitorTickView(APIView):
    """
    Background monitor tick — detect + capture for all active cameras.

    Used by the dashboard shell so monitoring continues on any page
    (Events, Unknown, Alerts, etc.), not only /cameras.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.cameras.continuous_recorder import recorder_manager
        from apps.cameras.monitor import monitor_active_cameras

        org_ids = list(request.user.memberships.values_list("organization_id", flat=True))
        run_vision = request.query_params.get("vision", "1") not in {"0", "false", "no"}
        # Keep continuous recording workers in sync with DB flags.
        try:
            recorder_manager.sync_from_db()
        except Exception:
            pass
        result = monitor_active_cameras(organization_ids=org_ids, run_vision=run_vision)
        result["recorder"] = recorder_manager.status()
        return Response(result)


class CameraRecorderStatusView(APIView):
    """Return which cameras are currently writing continuous video."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.cameras.continuous_recorder import recorder_manager

        org_ids = list(request.user.memberships.values_list("organization_id", flat=True))
        allowed = set(
            str(cid)
            for cid in Camera.objects.filter(site__organization_id__in=org_ids).values_list("id", flat=True)
        )
        status = recorder_manager.status()
        status["workers"] = [w for w in status.get("workers", []) if w.get("camera_id") in allowed]
        status["count"] = len(status["workers"])
        return Response(status)


class CameraVisionAnalyzeView(APIView):
    """Capture a live frame, analyze with a vision LLM, and persist the capture."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        camera = _user_camera(request, pk)
        if camera is None:
            return Response({"detail": "Camera not found."}, status=status.HTTP_404_NOT_FOUND)
        if not camera.rtsp_url:
            return Response(
                {"detail": "Camera stream URL is not configured."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            frame, _, _, _ = _capture_frame(camera.rtsp_url)

            # Face-verify every human; capture unknown/threat + known-on-mobile.
            persons, _, verify = _detect_and_record(frame.copy(), camera)
            unknown_count = int(verify.get("unknown") or 0)
            threat_count = int(verify.get("blacklist") or 0)
            verified_count = int(verify.get("verified") or 0)
            if not _has_actionable_activity(verify):
                return Response(
                    {
                        "detail": "No people activity to capture.",
                        "skipped": True,
                        "detected": persons,
                        "verified": verified_count,
                    },
                    status=status.HTTP_200_OK,
                )

            # Multi-human: crop covering ALL people; unknown: wide clear person (no face zoom).
            focus_box = _focus_box_for_people(verify)
            if not focus_box:
                return Response(
                    {
                        "detail": "No clear person activity to capture.",
                        "skipped": True,
                        "detected": persons,
                    },
                    status=status.HTTP_200_OK,
                )
            multi = len(verify.get("person_boxes") or []) >= 2
            has_unknown = unknown_count > 0
            image_bytes, relative_path = _crop_person_jpeg(
                frame,
                focus_box,
                subdir="vision",
                pad_ratio=0.40 if has_unknown else (0.18 if multi else 0.12),
                min_long_side=800 if has_unknown or multi else 640,
            )
            if not image_bytes or not relative_path:
                return Response(
                    {"detail": "Could not crop active person.", "skipped": True},
                    status=status.HTTP_200_OK,
                )
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from .vision_llm import analyze_frame_jpeg

        try:
            analysis = analyze_frame_jpeg(image_bytes)
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if not _analysis_has_real_activity(analysis, require_people=True):
            return Response(
                {
                    "detail": "No meaningful activity in frame — not saved.",
                    "skipped": True,
                },
                status=status.HTTP_200_OK,
            )

        if unknown_count:
            person_status = "unknown"
        elif threat_count:
            person_status = "blacklist"
        else:
            person_status = "verified"

        # Prefer per-person detect results so every human (known+unknown) is listed.
        detected_people = [
            p for p in (verify.get("people") or []) if isinstance(p, dict)
        ]
        people = detected_people or list(analysis.people or [])
        if not people:
            labels = [
                str(label)
                for label in (verify.get("labels") or [])
                if label and not str(label).lower().startswith("issue")
            ]
            if (
                not labels
                and threat_count <= 0
                and unknown_count <= 0
                and verified_count <= 0
                and not verify.get("activity")
            ):
                return Response(
                    {"detail": "No person activity to save.", "skipped": True},
                    status=status.HTTP_200_OK,
                )
            people = [
                {
                    "description": label,
                    "activity": "present on camera",
                    "status": person_status,
                }
                for label in labels[:12]
            ] or [
                {
                    "description": "Person on camera",
                    "activity": "present on camera",
                    "status": person_status,
                }
            ]
        else:
            for person in people:
                if isinstance(person, dict) and "status" not in person:
                    person["status"] = person_status

        human_total = unknown_count + threat_count + verified_count
        summary = analysis.summary.strip() or (
            f"{human_total} person(s) on {camera.name} "
            f"({verified_count} known, {unknown_count} unknown"
            + (f", {threat_count} threat" if threat_count else "")
            + ")"
        )

        # Analyze frequently for real-time UI, but do not fill disk with an
        # identical scene every few seconds. Reuse the latest capture for 45s.
        latest = VisionCapture.objects.filter(camera=camera).first()
        if (
            latest
            and latest.summary.strip().lower() == summary.strip().lower()
            and latest.created_at >= timezone.now() - timedelta(seconds=45)
        ):
            return Response(VisionCaptureSerializer(latest, context={"request": request}).data)

        if threat_count:
            severity = "critical"
        elif unknown_count:
            severity = "warning"
        else:
            severity = "info"

        event = None
        try:
            from apps.events.pipeline import create_detection_event

            event = create_detection_event(
                camera=camera,
                event_type="unknown_person" if (unknown_count or threat_count) else "person_detected",
                severity=severity,
                confidence=analysis.confidence or 0.7,
                model_name=f"vision-llm:{analysis.provider}",
                model_version=analysis.model_name,
                payload={
                    "source": "vision_llm",
                    "capture_kind": "multi_human_activity" if human_total >= 2 else "human_activity",
                    "summary": summary,
                    "scene": analysis.scene,
                    "people": people,
                    "objects": [],
                    "risks": analysis.risks,
                    "text_visible": analysis.text_visible,
                    "image_path": relative_path,
                    "bbox": focus_box,
                    "person_count": human_total,
                    "verified_count": verified_count,
                    "unknown_count": unknown_count,
                    "blacklist_count": threat_count,
                },
                run_hooks=True,
            ).event
        except Exception:
            event = None

        capture = VisionCapture.objects.create(
            organization=camera.site.organization,
            camera=camera,
            event=event,
            image_path=relative_path,
            summary=summary,
            scene=analysis.scene or "people activity",
            people=people,
            detected_objects=[],
            risks=analysis.risks,
            text_visible=analysis.text_visible,
            confidence=analysis.confidence,
            model_name=analysis.model_name,
            provider=analysis.provider,
            raw_response=analysis.raw_response[:8000],
        )
        return Response(
            VisionCaptureSerializer(capture, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class VisionCaptureListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = VisionCaptureSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("camera",)

    def get_queryset(self):
        # AI Vision gallery: real activity only — skip empty / object clutter.
        from django.db.models import Q

        return (
            VisionCapture.objects.filter(organization_id__in=self.get_user_organizations())
            .exclude(people=[])
            .exclude(
                Q(summary__icontains="empty scene")
                | Q(summary__icontains="nothing clearly")
                | Q(summary__icontains="no security activity")
            )
            .select_related("camera", "camera__site")
        )

    def delete(self, request):
        from pathlib import Path

        from django.conf import settings

        queryset = self.get_queryset()
        organizations = {capture.organization for capture in queryset.select_related("organization")}
        for organization in organizations:
            require_role(request.user, organization, [Role.OWNER, Role.ADMIN])

        image_paths = set(queryset.exclude(image_path="").values_list("image_path", flat=True))
        deleted_count = queryset.count()
        queryset.delete()

        media_root = Path(settings.MEDIA_ROOT).resolve()
        for image_path in image_paths:
            if image_path.startswith(("http://", "https://")):
                continue
            relative_path = image_path.lstrip("/\\")
            media_prefix = settings.MEDIA_URL.strip("/\\")
            if media_prefix and relative_path.startswith(f"{media_prefix}/"):
                relative_path = relative_path[len(media_prefix) + 1 :]
            file_path = (media_root / relative_path).resolve()
            if media_root in file_path.parents and file_path.is_file():
                file_path.unlink(missing_ok=True)

        return Response({"deleted": deleted_count}, status=status.HTTP_200_OK)


class VisionLLMStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .vision_llm import vision_llm_status

        return Response(vision_llm_status())


def _candidate_stream_urls(source_url: str) -> list[str]:
    """Expand a phone/IP base URL into common IP Webcam / MJPEG / Dahua RTSP paths."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    source_url = (source_url or "").strip()
    if not source_url:
        return []

    parsed = urlparse(source_url)
    candidates = [source_url]
    if parsed.scheme in {"http", "https"} and (not parsed.path or parsed.path == "/"):
        base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        for path in ("/video", "/videofeed", "/shot.jpg", "/"):
            candidates.append(f"{base}{path}")

    # Dahua / Hikvision-style RTSP: substream first (faster through Docker), then main.
    if parsed.scheme == "rtsp":
        path = (parsed.path or "").lower()
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if "realmonitor" in path or "subtype" in query or "channel" in query:
            alts = []
            for subtype in ("1", "0"):
                q = {**query, "channel": query.get("channel") or "1", "subtype": subtype}
                alts.append(
                    urlunparse(
                        (
                            parsed.scheme,
                            parsed.netloc,
                            parsed.path or "/cam/realmonitor",
                            "",
                            urlencode(q),
                            "",
                        )
                    )
                )
            host_only = urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
            candidates = alts + candidates + [host_only]

    # De-dupe while preserving order
    seen = set()
    ordered = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def _capture_frame(source_url: str, *, prefer_fast: bool = False):
    """Capture one frame from RTSP or HTTP/MJPEG (IP Webcam friendly).

    When ``prefer_fast`` is True (live preview), use the persistent RTSP pool and
    Dahua substream so each request does not re-open the camera (~1s savings).
    """
    from urllib.parse import urlparse

    scheme = urlparse((source_url or "").strip()).scheme.lower()
    if prefer_fast and scheme == "rtsp":
        try:
            from .live_stream import live_stream_pool, prefer_fast_url

            frame = live_stream_pool.get_frame(source_url, prefer_fast=True)
            height, width = frame.shape[:2]
            return frame, width, height, prefer_fast_url(source_url)
        except RuntimeError:
            # Fall through to one-shot open if the pool is still cold / stalled.
            pass

    errors: list[str] = []
    candidates = _candidate_stream_urls(source_url)
    if prefer_fast and scheme == "rtsp":
        from .live_stream import prefer_fast_url

        fast = prefer_fast_url(source_url)
        candidates = [fast] + [u for u in candidates if u != fast]

    for url in candidates:
        url_scheme = urlparse(url).scheme.lower()
        try:
            if url_scheme in {"http", "https"}:
                frame = _capture_http_jpeg(url)
                height, width = frame.shape[:2]
                return frame, width, height, url
            frame = _capture_opencv(url)
            height, width = frame.shape[:2]
            return frame, width, height, url
        except RuntimeError as exc:
            errors.append(f"{url}: {exc}")
            continue

    detail = "; ".join(errors[-3:]) if errors else "No reachable stream URL."
    if "401" in detail or "Unauthorized" in detail:
        raise RuntimeError(
            "Camera requires login. In IP Webcam turn off password, "
            "or use http://username:password@192.168.x.x:8080/video"
        )
    raise RuntimeError(f"Could not read camera frame. {detail}")


def _capture_http_jpeg(url: str):
    """Fetch a still JPEG (IP Webcam /shot.jpg or MJPEG first frame via requests)."""
    try:
        import cv2
        import numpy as np
        import requests
    except ImportError as exc:
        raise RuntimeError("Live preview requires opencv-python-headless and requests.") from exc

    from urllib.parse import urlparse

    path = (urlparse(url).path or "").lower()
    try:
        if path.endswith(".jpg") or path.endswith(".jpeg") or path.endswith("shot.jpg") or path in {"", "/"}:
            shot_url = url if path.endswith((".jpg", ".jpeg")) or path.endswith("shot.jpg") else url.rstrip("/") + "/shot.jpg"
            response = requests.get(shot_url, timeout=4)
            if response.status_code == 401:
                raise RuntimeError("401 Unauthorized")
            if response.status_code >= 400 and path not in {"", "/"} and not path.endswith("shot.jpg"):
                raise RuntimeError(f"HTTP {response.status_code}")
            if response.status_code < 400:
                arr = np.frombuffer(response.content, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    return frame

        # MJPEG: read a chunk and extract first JPEG
        with requests.get(url, stream=True, timeout=5) as response:
            if response.status_code == 401:
                raise RuntimeError("401 Unauthorized")
            response.raise_for_status()
            buffer = b""
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                buffer += chunk
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9")
                if start != -1 and end != -1 and end > start:
                    jpeg = buffer[start : end + 2]
                    arr = np.frombuffer(jpeg, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        return frame
                if len(buffer) > 2_000_000:
                    break
        raise RuntimeError("No JPEG frame found in HTTP stream.")
    except requests.RequestException as exc:
        raise RuntimeError(str(exc)) from exc


_RTSP_CAPTURE_SLOTS = __import__("threading").Semaphore(2)
_RTSP_OPEN_TIMEOUT_SEC = 12.0


def _capture_opencv(url: str):
    """Open one RTSP/HTTP frame via OpenCV with a hard wall-clock timeout."""
    try:
        import threading

        from apps.cameras.rtsp_open import open_rtsp_capture
    except ImportError as exc:
        raise RuntimeError("Live preview requires opencv-python-headless on the API server.") from exc

    box: dict = {}
    released = False

    def _release_slot() -> None:
        nonlocal released
        if released:
            return
        released = True
        _RTSP_CAPTURE_SLOTS.release()

    def _open_and_read() -> None:
        capture = None
        try:
            capture = open_rtsp_capture(url)
            if not capture.isOpened():
                raise RuntimeError("Could not open stream.")
            ok, frame = capture.read()
            if (not ok or frame is None) and capture.isOpened():
                for _ in range(2):
                    ok, frame = capture.read()
                    if ok and frame is not None:
                        break
            if not ok or frame is None:
                raise RuntimeError("Stream opened but no frame received.")
            box["frame"] = frame
        except Exception as exc:  # noqa: BLE001 — surface to caller thread
            box["error"] = exc
        finally:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            _release_slot()

    if not _RTSP_CAPTURE_SLOTS.acquire(timeout=1.0):
        raise RuntimeError("Camera capture busy, retry shortly.")
    worker = threading.Thread(target=_open_and_read, name="rtsp-frame", daemon=True)
    worker.start()
    worker.join(timeout=_RTSP_OPEN_TIMEOUT_SEC)
    if worker.is_alive():
        # Slot stays held until the native open returns, so retries cannot pile up.
        raise RuntimeError("Camera stream timed out.")
    if "error" in box:
        err = box["error"]
        raise err if isinstance(err, RuntimeError) else RuntimeError(str(err))
    frame = box.get("frame")
    if frame is None:
        raise RuntimeError("Could not open stream.")
    return frame


def _draw_tracking_overlays(frame, camera):
    try:
        import cv2

        events = DetectionEvent.objects.filter(
            camera=camera,
            created_at__gte=timezone.now() - timedelta(seconds=20),
        ).order_by("-created_at")[:20]
        for event in events:
            bbox = (event.payload or {}).get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                continue
            x, y, width, height = [int(float(value)) for value in bbox]
            if width <= 0 or height <= 0:
                continue
            color = (189, 127, 78)
            cv2.rectangle(frame, (x, y), (x + width, y + height), color, 2)
            label = f"ID {(event.payload or {}).get('track_id', '-')}  {event.confidence:.0%}"
            cv2.putText(
                frame,
                label,
                (x, max(20, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
            )
    except Exception:
        # A malformed event must never break the live camera preview.
        return


_LAST_DETECT_EVENT: dict[str, float] = {}
_LAST_UNKNOWN_SNAPSHOT: dict[str, float] = {}
_LAST_KNOWN_SIGHTING: dict[str, float] = {}
_LAST_VERIFY_EVENT: dict[str, float] = {}
_LAST_THIEF_CAPTURE: dict[str, float] = {}
_LAST_WEAPON_CAPTURE: dict[str, float] = {}
# Brief hold so saved members stay "on mobile" while phone box flickers.
_LAST_MOBILE_USE: dict[str, float] = {}
_MOBILE_USE_HOLD_SEC = 4.0
# Keep a verified name on a body slot so one bad frame doesn't flip to UNKNOWN.
_IDENTITY_HOLD: dict[str, dict] = {}
_IDENTITY_HOLD_SEC = 8.0
# Require 2 agreeing face matches before locking a saved name (stops wrong IDs).
_IDENTITY_CONFIRM: dict[str, dict] = {}
# Temporarily off — re-enable when thief / blacklist alerts are needed again.
THIEF_DETECTION_ENABLED = False


_STATE_MAX_AGE_SEC = 120.0  # longest cooldown above is 30 s
_last_state_prune = 0.0


def _prune_detection_state(now: float) -> None:
    """Drop stale per-slot timestamps so these maps don't grow per grid cell forever."""
    global _last_state_prune
    if now - _last_state_prune < 60.0:
        return
    _last_state_prune = now
    cutoff = now - _STATE_MAX_AGE_SEC
    for table in (
        _LAST_DETECT_EVENT,
        _LAST_UNKNOWN_SNAPSHOT,
        _LAST_KNOWN_SIGHTING,
        _LAST_VERIFY_EVENT,
        _LAST_THIEF_CAPTURE,
        _LAST_WEAPON_CAPTURE,
        _LAST_MOBILE_USE,
    ):
        for key in [k for k, ts in list(table.items()) if ts < cutoff]:
            table.pop(key, None)
    for table in (_IDENTITY_HOLD, _IDENTITY_CONFIRM):
        for key in [k for k, v in list(table.items()) if float(v.get("seen_at") or 0.0) < cutoff]:
            table.pop(key, None)


def _identity_slot_key(camera_id, x: int, y: int) -> str:
    return f"{camera_id}:{int(x / 70)}:{int(y / 70)}"


def _remember_identity(slot_key: str, result: dict, now: float) -> None:
    if result.get("status") not in {"verified", "blacklist"}:
        return
    # Only stick strong matches — weak ones caused repeated wrong names.
    if float(result.get("match_score") or 0.0) < 0.32:
        return
    _IDENTITY_HOLD[slot_key] = {
        "result": {
            "known": result.get("known", True),
            "match_score": result.get("match_score", 0.0),
            "person_id": result.get("person_id"),
            "name": result.get("name"),
            "label": result.get("label"),
            "status": result.get("status"),
            "using_phone": result.get("using_phone", False),
            "activity": result.get("activity"),
            "save_worthy": result.get("save_worthy", False),
            "announce": result.get("announce"),
            "issue": result.get("issue"),
            "watch_mobile_activity": bool(result.get("watch_mobile_activity")),
        },
        "seen_at": now,
    }


def _held_identity(slot_key: str, now: float) -> dict | None:
    held = _IDENTITY_HOLD.get(slot_key)
    if not held:
        return None
    # Do NOT refresh TTL — a wrong name must expire instead of sticking forever.
    if now - float(held.get("seen_at") or 0.0) > _IDENTITY_HOLD_SEC:
        _IDENTITY_HOLD.pop(slot_key, None)
        return None
    payload = dict(held.get("result") or {})
    payload["from_hold"] = True
    return payload


def _confirm_identity(slot_key: str, person_id: str, score: float, now: float) -> bool:
    """
    True only after the same saved person matches twice in a short window,
    or once with a very strong SFace score.
    """
    if score >= 0.40:
        _IDENTITY_CONFIRM.pop(slot_key, None)
        return True
    prev = _IDENTITY_CONFIRM.get(slot_key) or {}
    if now - float(prev.get("seen_at") or 0.0) > 6.0:
        prev = {}
    if prev.get("person_id") == person_id:
        _IDENTITY_CONFIRM.pop(slot_key, None)
        return True
    _IDENTITY_CONFIRM[slot_key] = {"person_id": person_id, "score": score, "seen_at": now}
    return False


def _enhance_clarity(image):
    """Mild denoise + unsharp so unknown crops look clearer without plastic look."""
    import cv2

    if image is None or getattr(image, "size", 0) == 0:
        return image
    # Light bilateral keeps edges while cutting phone-cam noise.
    cleaned = cv2.bilateralFilter(image, d=5, sigmaColor=35, sigmaSpace=35)
    blur = cv2.GaussianBlur(cleaned, (0, 0), sigmaX=1.1)
    sharp = cv2.addWeighted(cleaned, 1.45, blur, -0.45, 0)
    return sharp


def _write_clear_jpeg(
    image_or_bytes,
    *,
    subdir: str = "sightings",
    min_long_side: int = 560,
    quality: int = 95,
) -> tuple[bytes | None, str | None]:
    """Encode an enhanced clear JPEG under media; return (bytes, relative_path)."""
    import uuid
    from pathlib import Path

    import cv2
    import numpy as np
    from django.conf import settings

    if isinstance(image_or_bytes, (bytes, bytearray)):
        array = np.frombuffer(image_or_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    else:
        image = image_or_bytes
    if image is None or getattr(image, "size", 0) == 0:
        return None, None

    image = _enhance_clarity(image)
    height, width = image.shape[:2]
    long_side = max(height, width)
    if long_side > 0 and long_side < min_long_side:
        scale = min_long_side / float(long_side)
        image = cv2.resize(
            image,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_LANCZOS4,
        )
    # Cap absurd upsizing artifacts.
    height, width = image.shape[:2]
    max_side = 1280
    if max(height, width) > max_side:
        scale = max_side / float(max(height, width))
        image = cv2.resize(
            image,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )

    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None, None

    relative_path = (
        Path(subdir)
        / timezone.now().strftime("%Y/%m/%d")
        / f"{uuid.uuid4().hex[:14]}.jpg"
    )
    destination = Path(settings.MEDIA_ROOT) / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    image_bytes = encoded.tobytes()
    destination.write_bytes(image_bytes)
    return image_bytes, relative_path.as_posix()


def _crop_person_jpeg(
    frame,
    box,
    *,
    subdir: str = "sightings",
    pad_ratio: float = 0.06,
    min_long_side: int = 560,
    quality: int = 95,
) -> tuple[bytes | None, str | None]:
    """Crop one person (optional padding), enhance clarity, optionally upscale."""
    x, y, width, height = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    if width < 8 or height < 8:
        return None, None

    frame_height, frame_width = frame.shape[:2]
    padding_x = max(6, int(width * pad_ratio))
    padding_y = max(10, int(height * pad_ratio * 1.5))
    x1 = max(0, x - padding_x)
    y1 = max(0, y - padding_y)
    x2 = min(frame_width, x + width + padding_x)
    y2 = min(frame_height, y + height + padding_y)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return None, None
    return _write_clear_jpeg(
        crop, subdir=subdir, min_long_side=min_long_side, quality=quality
    )


def _largest_box(boxes: list) -> list | None:
    if not boxes:
        return None
    return max(boxes, key=lambda item: max(1, int(item[2])) * max(1, int(item[3])))


def _union_boxes(boxes: list) -> list | None:
    """Bounding box covering every person so multi-human scenes stay together."""
    if not boxes:
        return None
    if len(boxes) == 1:
        return [int(boxes[0][0]), int(boxes[0][1]), int(boxes[0][2]), int(boxes[0][3])]
    x1 = min(int(b[0]) for b in boxes)
    y1 = min(int(b[1]) for b in boxes)
    x2 = max(int(b[0]) + int(b[2]) for b in boxes)
    y2 = max(int(b[1]) + int(b[3]) for b in boxes)
    return [x1, y1, max(1, x2 - x1), max(1, y2 - y1)]


def _focus_box_for_people(verify: dict | None) -> list | None:
    """Crop covering capture-worthy people (unknown/threat + known-on-mobile)."""
    verify = verify or {}
    boxes = list(verify.get("person_boxes") or []) or list(verify.get("unknown_boxes") or [])
    if not boxes:
        return None
    if len(boxes) >= 2:
        return _union_boxes(boxes)
    return _largest_box(boxes)


def _discard_snapshot(relative_path: str | None) -> None:
    """Remove a just-written crop when it must not be kept."""
    if not relative_path or str(relative_path).startswith(("http://", "https://")):
        return
    try:
        from pathlib import Path

        from django.conf import settings

        path = Path(settings.MEDIA_ROOT) / str(relative_path).lstrip("/\\")
        if path.is_file():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _has_actionable_activity(verify: dict | None) -> bool:
    """True for unknown/threat/danger, or known-on-mobile (person_boxes only then)."""
    verify = verify or {}
    if verify.get("activity"):
        return True
    if int(verify.get("blacklist") or 0) > 0:
        return True
    if int(verify.get("unknown") or 0) > 0 and (verify.get("unknown_boxes") or verify.get("person_boxes")):
        return True
    # Known people only count when they earned a person_box (mobile in use).
    if int(verify.get("verified") or 0) > 0 and (verify.get("person_boxes") or []):
        return True
    if int(verify.get("dangers") or 0) > 0:
        return True
    return False


_IDLE_SUMMARY_MARKERS = (
    "nothing clearly",
    "empty scene",
    "no person",
    "no people",
    "idle",
    "static scene",
    "camera sees nothing",
)


def _analysis_has_real_activity(analysis, *, require_people: bool = True) -> bool:
    """Drop LLM/YOLO results that are furniture/empty-room noise."""
    summary = (getattr(analysis, "summary", "") or "").strip().lower()
    if any(marker in summary for marker in _IDLE_SUMMARY_MARKERS):
        return False
    people = getattr(analysis, "people", None) or []
    risks = getattr(analysis, "risks", None) or []
    if require_people and not people and not risks:
        return False
    # Object-only scenes (chair, bottle, tv) without people/risks are junk.
    if require_people and not people:
        critical_risks = [
            r for r in risks if isinstance(r, dict) and str(r.get("level", "")).lower() in {"warning", "critical"}
        ]
        if not critical_risks:
            return False
    return True


def _face_blur_ok(scanned, minimum: float = 35.0) -> bool:
    """Reject soft / motion-blur faces so gallery only keeps clear shots."""
    if scanned is None:
        return False
    blur = float(getattr(scanned, "blur_score", 0) or 0)
    quality = float(getattr(scanned, "quality_score", 0) or 0)
    return blur >= minimum or quality >= 0.72


def _emit_verify_event(
    camera,
    *,
    event_type: str,
    severity: str,
    confidence: float,
    payload: dict,
    cooldown_key: str,
    cooldown_sec: float = 15.0,
):
    """Create a verification event (with alert hooks) under a short cooldown."""
    import time

    from apps.events.pipeline import create_detection_event

    now = time.time()
    if now - _LAST_VERIFY_EVENT.get(cooldown_key, 0.0) < cooldown_sec:
        return None
    _LAST_VERIFY_EVENT[cooldown_key] = now
    try:
        return create_detection_event(
            camera=camera,
            event_type=event_type,
            severity=severity,
            confidence=confidence,
            model_name="face-verify",
            model_version="realtime-1",
            payload=payload,
            run_hooks=True,
        ).event
    except Exception:
        return None


def _process_person_box(
    frame,
    box,
    camera,
    event,
    confidence: float,
    *,
    using_phone: bool = False,
) -> dict | None:
    """
    Realtime face verification + multi-human capture.

    - Enrolled member → show name; snapshot ONLY when using mobile (profile)
    - Blacklist match → THIEF {name} + auto capture
    - Face hidden/covered → THIEF FACE HIDDEN + auto capture
    - Unknown → auto capture + UNKNOWN label
    """
    import time

    from apps.identity.embeddings import appearance_embedding_from_bytes, l2_normalize
    from apps.identity.face_scan import scan_face_from_bytes
    from apps.identity.models import PersonType
    from apps.identity.services import (
        create_person_sighting,
        create_unknown_sighting,
        handle_visitor_face_match,
        recognize_blacklist,
        recognize_embedding,
        record_attendance,
        save_profile_image_from_snapshot,
    )

    # Extra top pad — YOLO person boxes often clip the forehead/hairline.
    image_bytes, relative_path = _crop_person_jpeg(
        frame, box, pad_ratio=0.18, min_long_side=640, quality=95
    )
    if not image_bytes or not relative_path:
        return None

    now = time.time()
    x, y, width, height = box[0], box[1], box[2], box[3]
    frame_h = max(1, frame.shape[0])
    person_large = height >= 90 or (height / frame_h) >= 0.22

    face_found = False
    scanned = None
    match_bytes = image_bytes
    # Prefer a solid face crop; fall back to smaller faces for distant CCTV.
    for min_face in (36, 24, 18):
        try:
            scanned = scan_face_from_bytes(image_bytes, require_face=True, min_size=min_face)
            match_bytes = scanned.face_jpeg
            face_found = scanned.face_count > 0 and not scanned.used_full_image
            if face_found:
                break
        except Exception:
            face_found = False
            scanned = None

    # Clear face crop is for unknown / thief capture only (not enrolled members).
    # We decide after recognition; keep scanned.face for matching bytes.
    if face_found and scanned is not None and getattr(scanned, "face_jpeg", None):
        match_bytes = scanned.face_jpeg

    # Face covered / hidden → thief path (disabled for now).
    if THIEF_DETECTION_ENABLED and (not face_found) and person_large:
        return _handle_face_hidden_thief(
            camera=camera,
            event=event,
            confidence=confidence,
            image_bytes=image_bytes,
            relative_path=relative_path,
            now=now,
        )

    if not face_found:
        # Keep sticky name if this slot was just verified; else show person seen.
        held = _held_identity(_identity_slot_key(camera.id, x, y), now)
        if held and held.get("status") == "verified":
            _discard_snapshot(relative_path)
            return held
        _discard_snapshot(relative_path)
        return {
            "known": False,
            "match_score": 0.0,
            "name": "Person",
            "label": "PERSON",
            "status": "pending",
            "issue": "Face not clear — move closer or face the camera",
            "save_worthy": False,
            "announce": "person detected",
        }

    # Only match with a real face feature — body/LBP crops caused wrong names.
    feature = list(getattr(scanned, "feature", None) or [])
    if not feature:
        try:
            feature = appearance_embedding_from_bytes(match_bytes)
        except Exception:
            feature = []
    if not feature:
        _discard_snapshot(relative_path)
        return {
            "known": False,
            "match_score": 0.0,
            "name": "Person",
            "label": "PERSON",
            "status": "pending",
            "issue": "Face particles unavailable — retrying",
            "save_worthy": False,
        }
    embedding = l2_normalize(feature)

    slot_key = _identity_slot_key(camera.id, x, y)

    # Strict face match — correct saved name over forced match.
    person, match_score, _emb = recognize_embedding(
        camera.site.organization_id,
        embedding,
        threshold=0.32,
        min_margin=0.05,
    )
    # Optional second pass on face crop only (never full-body — that mislabels).
    if person is None and match_bytes is not image_bytes and scanned is not None:
        try:
            alt_embedding = appearance_embedding_from_bytes(match_bytes)
            alt_person, alt_score, alt_emb = recognize_embedding(
                camera.site.organization_id,
                alt_embedding,
                threshold=0.32,
                min_margin=0.05,
            )
            if alt_person is not None and alt_score >= float(match_score or 0.0):
                person, match_score, _emb = alt_person, alt_score, alt_emb
        except Exception:
            pass

    # Confirm same person twice (or one strong hit) before locking the name.
    if person is not None:
        if not _confirm_identity(slot_key, str(person.id), float(match_score or 0.0), now):
            held = _held_identity(slot_key, now)
            if held and held.get("person_id") == str(person.id):
                return held
            _discard_snapshot(relative_path)
            return {
                "known": False,
                "match_score": match_score,
                "name": "Checking",
                "label": "CONFIRMING",
                "status": "pending",
                "issue": "Confirming saved identity…",
                "save_worthy": False,
            }

    # Hold only the same strong identity briefly — never invent a new name.
    if person is None:
        held = _held_identity(slot_key, now)
        if held and held.get("status") == "verified":
            can_watch = bool(held.get("watch_mobile_activity"))
            phone_now = bool((using_phone or held.get("using_phone")) and can_watch)
            held = {**held, "using_phone": phone_now}
            if phone_now and held.get("name") and "MOBILE" not in str(held.get("label") or "").upper():
                held["label"] = f"{held['name']} · MOBILE"
                held["activity"] = "using mobile"
                held["save_worthy"] = True
            return held

    if person is not None:
        is_threat = bool(THIEF_DETECTION_ENABLED and person.person_type == PersonType.BLACKLIST)
        status = "blacklist" if is_threat else "verified"
        # Live frame: saved people show name only (no "KNOWN" word). Thief keeps THIEF.
        label = f"THIEF {person.full_name}" if is_threat else person.full_name
        # Strict mobile watch is per-person — only those flagged get mobile activity.
        strict_mobile = bool(getattr(person, "watch_mobile_activity", False))
        if using_phone and not strict_mobile and not is_threat:
            using_phone = False

        known_key = f"{camera.id}:{person.id}"
        if is_threat:
            # Thief (saved blacklist) → auto-capture image + alert.
            if now - _LAST_KNOWN_SIGHTING.get(known_key, 0.0) >= 4.0:
                create_person_sighting(
                    person=person,
                    camera=camera,
                    snapshot_path=relative_path,
                    confidence=confidence,
                    match_score=match_score,
                    event=event,
                )
                _LAST_KNOWN_SIGHTING[known_key] = now
                _LAST_THIEF_CAPTURE[f"{camera.id}:named"] = now
                _emit_verify_event(
                    camera,
                    event_type="unknown_person",
                    severity="critical",
                    confidence=match_score,
                    payload={
                        "verification": "blacklist",
                        "threat": "thief",
                        "auto_capture": True,
                        "person_id": str(person.id),
                        "full_name": person.full_name,
                        "match_score": round(match_score, 3),
                        "snapshot_path": relative_path,
                        "source": "realtime_thief_capture",
                    },
                    cooldown_key=f"threat:{camera.id}:{person.id}",
                    cooldown_sec=8.0,
                )
        else:
            # Known / enrolled: snapshot ONLY while using a mobile phone.
            # Sticky hold covers brief phone-box dropouts from YOLO.
            mobile_key = f"{camera.id}:person:{person.id}"
            if using_phone:
                _LAST_MOBILE_USE[mobile_key] = now
            elif now - _LAST_MOBILE_USE.get(mobile_key, 0.0) <= _MOBILE_USE_HOLD_SEC:
                using_phone = True
            if using_phone:
                label = f"{person.full_name} · MOBILE"
                if now - _LAST_KNOWN_SIGHTING.get(known_key, 0.0) >= 5.0:
                    if scanned is not None and _face_blur_ok(scanned, minimum=28.0):
                        clear_bytes, clear_path = _write_clear_jpeg(
                            scanned.face_jpeg,
                            subdir="sightings",
                            min_long_side=640,
                            quality=96,
                        )
                        if clear_bytes and clear_path:
                            relative_path = clear_path
                    create_person_sighting(
                        person=person,
                        camera=camera,
                        snapshot_path=relative_path,
                        confidence=confidence,
                        match_score=match_score,
                        event=event,
                        notes="mobile_in_use",
                    )
                    try:
                        save_profile_image_from_snapshot(
                            person=person,
                            snapshot_path=relative_path,
                            camera=camera,
                            match_score=match_score,
                            reason="mobile_in_use",
                        )
                    except Exception:
                        pass
                    _LAST_KNOWN_SIGHTING[known_key] = now
                    try:
                        record_attendance(person, camera, match_score, event=event)
                    except Exception:
                        pass
                    try:
                        handle_visitor_face_match(person, match_score)
                    except Exception:
                        pass
                    _emit_verify_event(
                        camera,
                        event_type="face_recognized",
                        severity="warning",
                        confidence=match_score,
                        payload={
                            "verification": "verified",
                            "mobile_in_use": True,
                            "profile_image_saved": True,
                            "auto_capture": True,
                            "person_id": str(person.id),
                            "full_name": person.full_name,
                            "person_type": person.person_type,
                            "match_score": round(match_score, 3),
                            "snapshot_path": relative_path,
                            "source": "realtime_known_mobile_profile",
                        },
                        cooldown_key=f"verified-mobile:{camera.id}:{person.id}",
                        cooldown_sec=10.0,
                    )
            else:
                _discard_snapshot(relative_path)
                if now - _LAST_KNOWN_SIGHTING.get(known_key, 0.0) >= 20.0:
                    _LAST_KNOWN_SIGHTING[known_key] = now
                    try:
                        record_attendance(person, camera, match_score, event=event)
                    except Exception:
                        pass
                    try:
                        handle_visitor_face_match(person, match_score)
                    except Exception:
                        pass
                    _emit_verify_event(
                        camera,
                        event_type="face_recognized",
                        severity="info",
                        confidence=match_score,
                        payload={
                            "verification": "verified",
                            "person_id": str(person.id),
                            "full_name": person.full_name,
                            "person_type": person.person_type,
                            "match_score": round(match_score, 3),
                            "skip_image_capture": True,
                            "source": "realtime_face_verify",
                        },
                        cooldown_key=f"verified:{camera.id}:{person.id}",
                        cooldown_sec=30.0,
                    )

        verified = {
            "person_id": str(person.id),
            "name": person.full_name,
            "label": label,
            "status": status,
            "match_score": match_score,
            "known": True,
            "using_phone": bool(using_phone and not is_threat),
            "activity": (
                "using mobile"
                if using_phone and not is_threat
                else ("threat on camera" if is_threat else "present on camera")
            ),
            "issue": (
                "THIEF auto-captured"
                if is_threat
                else (
                    "Known · mobile — saved to profile"
                    if using_phone
                    else "Known person — image not saved"
                )
            ),
            # Known snap only on mobile; thief always; unknown handled below.
            "save_worthy": bool(is_threat or using_phone),
            "profile_capture": bool(using_phone and not is_threat),
            "announce": (
                f"THIEF detected: {person.full_name}"
                if is_threat
                else (
                    f"{person.full_name} · mobile — profile saved"
                    if using_phone
                    else person.full_name
                )
            ),
            "watch_mobile_activity": strict_mobile,
        }
        _remember_identity(slot_key, verified, now)
        return verified

    # Unknown face — soft blacklist match (disabled while thief detection is off).
    if THIEF_DETECTION_ENABLED:
        bl_person, bl_score, _ = recognize_blacklist(
            camera.site.organization_id, embedding, threshold=0.30
        )
        if bl_person is not None:
            create_person_sighting(
                person=bl_person,
                camera=camera,
                snapshot_path=relative_path,
                confidence=confidence,
                match_score=bl_score,
                event=event,
            )
            _emit_verify_event(
                camera,
                event_type="unknown_person",
                severity="critical",
                confidence=bl_score,
                payload={
                    "verification": "blacklist",
                    "threat": "thief",
                    "auto_capture": True,
                    "person_id": str(bl_person.id),
                    "full_name": bl_person.full_name,
                    "match_score": round(bl_score, 3),
                    "snapshot_path": relative_path,
                    "source": "realtime_thief_soft_match",
                },
                cooldown_key=f"threat-soft:{camera.id}:{bl_person.id}",
                cooldown_sec=8.0,
            )
            return {
                "person_id": str(bl_person.id),
                "name": bl_person.full_name,
                "label": f"THIEF {bl_person.full_name}",
                "status": "blacklist",
                "match_score": bl_score,
                "known": True,
                "issue": "Auto-captured thief (soft match)",
                "save_worthy": True,
            }

    pct = int(round(float(match_score or 0.0) * 100))
    issue = (
        f"Not matched to enrolled people (best {pct}%). "
        "Add more angle photos in People & Enrollment."
    )
    # Skip long CHECKING holds — resolve to known (above) or unknown below.
    # Blurry / soft faces must NOT spam the unknown gallery.
    if not _face_blur_ok(scanned, minimum=18.0):
        held = _held_identity(slot_key, now)
        if held and held.get("status") == "verified":
            return held
        # Still label on overlay; just don't auto-capture junk frames.
        return {
            "known": False,
            "match_score": match_score,
            "name": "Unknown",
            "label": f"UNKNOWN PERSON {pct}%",
            "status": "unknown",
            "activity": "present on camera",
            "issue": "Face soft — waiting for a sharper capture",
            "save_worthy": False,
            "announce": "UNKNOWN person detected",
        }

    # Unknown gallery: wide clear person shot — do NOT zoom into the face.
    wide_bytes, wide_path = _crop_person_jpeg(
        frame,
        box,
        pad_ratio=0.45,
        min_long_side=720,
        quality=96,
    )
    if wide_bytes and wide_path:
        if relative_path and relative_path != wide_path:
            _discard_snapshot(relative_path)
        image_bytes, relative_path = wide_bytes, wide_path

    # Unknown → capture every distinct person slot (multi-human friendly).
    slot_x = int(x / 80)
    slot_y = int(y / 80)
    unknown_key = f"{camera.id}:{slot_x}:{slot_y}"
    if now - _LAST_UNKNOWN_SNAPSHOT.get(unknown_key, 0.0) >= 6.0:
        verify_event = _emit_verify_event(
            camera,
            event_type="unknown_person",
            severity="warning",
            confidence=confidence,
            payload={
                "verification": "unknown",
                "threat": "unknown_person",
                "announce": "UNKNOWN person detected",
                "match_score": round(float(match_score or 0.0), 3),
                "snapshot_path": relative_path,
                "source": "realtime_face_verify",
                "full_person": True,
                "auto_capture": True,
                "issue": issue,
                "bbox": [int(x), int(y), int(width), int(height)],
            },
            cooldown_key=f"unknown:{unknown_key}",
            cooldown_sec=8.0,
        )
        create_unknown_sighting(
            organization=camera.site.organization,
            camera=camera,
            embedding=embedding,
            confidence=confidence,
            track_id=None,
            snapshot_path=relative_path,
            event=verify_event or event,
        )
        _LAST_UNKNOWN_SNAPSHOT[unknown_key] = now

    return {
        "known": False,
        "match_score": match_score,
        "name": "Unknown",
        "label": f"UNKNOWN PERSON {pct}%",
        "status": "unknown",
        "activity": "present on camera",
        "issue": "UNKNOWN detected — auto-capturing",
        "save_worthy": True,
        "announce": "unknown",
    }


def _handle_face_hidden_thief(
    *,
    camera,
    event,
    confidence: float,
    image_bytes: bytes,
    relative_path: str,
    now: float,
) -> dict:
    """Auto-capture people who hide their face and label as thief."""
    from apps.identity.embeddings import appearance_embedding_from_bytes
    from apps.identity.services import create_unknown_sighting, recognize_blacklist

    capture_key = f"hidden:{camera.id}"
    should_capture = now - _LAST_THIEF_CAPTURE.get(capture_key, 0.0) >= 6.0

    embedding = appearance_embedding_from_bytes(image_bytes)
    bl_person, bl_score, _ = recognize_blacklist(
        camera.site.organization_id, embedding, threshold=0.28
    )

    if bl_person is not None:
        from apps.identity.services import create_person_sighting

        if should_capture:
            create_person_sighting(
                person=bl_person,
                camera=camera,
                snapshot_path=relative_path,
                confidence=confidence,
                match_score=bl_score,
                event=event,
            )
            _emit_verify_event(
                camera,
                event_type="unknown_person",
                severity="critical",
                confidence=max(confidence, bl_score),
                payload={
                    "verification": "blacklist",
                    "threat": "thief",
                    "face_hidden": True,
                    "auto_capture": True,
                    "person_id": str(bl_person.id),
                    "full_name": bl_person.full_name,
                    "match_score": round(bl_score, 3),
                    "snapshot_path": relative_path,
                    "source": "thief_face_hidden_match",
                },
                cooldown_key=f"threat-hidden:{camera.id}:{bl_person.id}",
                cooldown_sec=6.0,
            )
            _LAST_THIEF_CAPTURE[capture_key] = now
        return {
            "person_id": str(bl_person.id),
            "name": bl_person.full_name,
            "label": f"THIEF {bl_person.full_name}",
            "status": "blacklist",
            "match_score": bl_score,
            "known": True,
            "issue": "Face hidden — auto-captured as enrolled thief",
            "save_worthy": True,
        }

    if should_capture:
        verify_event = _emit_verify_event(
            camera,
            event_type="unknown_person",
            severity="critical",
            confidence=confidence,
            payload={
                "verification": "face_hidden",
                "threat": "thief",
                "face_hidden": True,
                "auto_capture": True,
                "snapshot_path": relative_path,
                "source": "thief_face_hidden_capture",
            },
            cooldown_key=f"face-hidden:{camera.id}",
            cooldown_sec=6.0,
        )
        create_unknown_sighting(
            organization=camera.site.organization,
            camera=camera,
            embedding=embedding,
            confidence=confidence,
            track_id=None,
            snapshot_path=relative_path,
            event=verify_event or event,
        )
        _LAST_THIEF_CAPTURE[capture_key] = now

    return {
        "known": False,
        "match_score": 0.0,
        "name": "Thief",
        "label": "THIEF FACE HIDDEN",
        "status": "blacklist",
        "issue": "Face covered/hidden — auto-captured as thief",
        "save_worthy": True,
    }


def _draw_detection_boxes(frame, detections, person_labels: dict | None = None) -> None:
    """Draw boxes for humans + mobile-in-use phones only."""
    import cv2

    from .detection import PERSON_CLASSES, PHONE_CLASSES, lookup_label_by_iou

    person_labels = person_labels or {}
    for item in detections:
        is_person = item.class_name in PERSON_CLASSES
        is_phone = item.class_name in PHONE_CLASSES
        if not (is_person or is_phone):
            continue
        x, y, width, height = item.x, item.y, item.width, item.height
        confidence = item.confidence
        # Smoothed sticky boxes rarely match raw YOLO keys — use IoU.
        info = lookup_label_by_iou(item, person_labels, min_iou=0.22)

        if is_phone:
            # Dedicated mobile activity box around the phone.
            color = (220, 180, 40)
            if isinstance(info, dict):
                text = str(info.get("label") or info.get("name") or "MOBILE IN USE")
            elif info:
                text = str(info)
            else:
                text = "MOBILE IN USE"
            label = f"{text} {confidence:.0%}"
        elif isinstance(info, dict):
            status = info.get("status", "unknown")
            text = info.get("label") or info.get("name") or "Person"
            if status == "verified":
                # Cyan/amber when known person is on mobile; green otherwise.
                if info.get("using_phone"):
                    color = (220, 180, 40)
                    if "MOBILE" not in str(text).upper():
                        text = f"{str(info.get('name') or text).removeprefix('KNOWN ').strip()} · MOBILE"
                else:
                    color = (90, 210, 140)
                    text = str(info.get("name") or text).removeprefix("KNOWN ").strip() or text
            elif status == "blacklist":
                color = (70, 70, 240)
            elif status == "pending":
                color = (180, 180, 180)
            else:
                # Unknown person also using phone — show mobile activity.
                if info.get("using_phone"):
                    color = (220, 180, 40)
                    text = f"{text} · MOBILE" if "MOBILE" not in str(text).upper() else text
                else:
                    color = (60, 165, 250)
            label = f"{text} {confidence:.0%}"
        elif info and info != "Unknown":
            color = (90, 210, 140)
            label = f"{info} {confidence:.0%}"
        else:
            color = (60, 165, 250)
            label = f"UNKNOWN {confidence:.0%}"

        thickness = max(2, int(round(frame.shape[1] / 640)))
        cv2.rectangle(frame, (x, y), (x + width, y + height), color, thickness)

        font_scale = max(0.55, frame.shape[1] / 1500)
        font_thickness = max(1, thickness - 1)
        (text_w, text_h), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, font_thickness
        )
        label_y = y - 10 if y - 10 > text_h else y + height + text_h + 10
        top_left = (x, label_y - text_h - baseline)
        bottom_right = (x + text_w + 8, label_y + baseline)
        cv2.rectangle(frame, top_left, bottom_right, color, -1)
        cv2.putText(
            frame,
            label,
            (x + 4, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (15, 20, 25),
            font_thickness,
            cv2.LINE_AA,
        )


def _quick_detections(frame, camera) -> list:
    """Person boxes from the live watcher, without a second slow YOLO pass."""
    from .detection import live_person_watcher, object_detector

    camera_id = str(camera.id)
    source = getattr(camera, "rtsp_url", "") or ""
    if source:
        live_person_watcher.touch(camera_id, source)
    cached = live_person_watcher.latest(camera_id, max_age=0.9)
    if cached is not None:
        return cached
    # Watcher is already scanning. Don't queue another pass behind it.
    if live_person_watcher.running:
        stale = live_person_watcher.latest(camera_id, max_age=2.5)
        return stale or []
    return object_detector.detect_fast(frame, input_size=320)


def _detect_and_record(frame, camera) -> tuple[int, list[str], dict]:
    """Detect people + hand dangers + face verify. Returns (person_count, labels, verify)."""
    verify = {
        "verified": 0,
        "unknown": 0,
        "blacklist": 0,
        "labels": [],
        "issues": [],
        "dangers": 0,
        "danger_labels": [],
        "unknown_boxes": [],
        "person_boxes": [],
        "people": [],
        "activity": False,
    }
    try:
        import time

        from apps.events.pipeline import create_detection_event

        _prune_detection_state(time.time())

        from .detection import (
            find_illegal_activity,
            find_phones_in_use,
            object_detector,
            person_using_phone,
        )

        # Fast human lock. The background watcher already runs YOLO; reuse its
        # boxes so this request can spend time on face names, not a second model pass.
        detections = _quick_detections(frame, camera)
        person_labels: dict = {}
        person_dets = [d for d in detections if d.class_name == "person"]
        verify["objects"] = []
        # Mobile tracking only when this camera's AI module is enabled.
        modules = set(camera.enabled_modules or [])
        track_mobile = "mobile_use_tracking" in modules
        phone_findings = find_phones_in_use(detections) if track_mobile else []
        phone_dets = []
        if phone_findings:
            verify["phones"] = len(phone_findings)
            verify["labels"].insert(0, "MOBILE IN USE")
            verify["activity"] = True
            for finding in phone_findings:
                phone = finding.get("phone")
                if phone is None:
                    continue
                phone_dets.append(phone)
                person_labels[(phone.x, phone.y, phone.width, phone.height)] = {
                    "status": "mobile",
                    "label": "MOBILE IN USE",
                    "name": "MOBILE IN USE",
                    "using_phone": True,
                    "activity": "using mobile",
                }
        verify["mobile_tracking"] = track_mobile
        if track_mobile and not phone_findings:
            # Still surface that module is on but no phone locked yet.
            verify.setdefault("phones", 0)

        # Paint the human immediately. Face matching below can take seconds;
        # the preview must not wait for it or the person walks out first.
        # Skip empty updates — the watcher owns "nobody here" so a slow face
        # request cannot erase a box that just appeared.
        if person_dets or phone_dets:
            from .detection import live_detection_tracker

            live_detection_tracker.update(
                str(camera.id),
                list(person_dets) + list(phone_dets),
                None,
                frame_h=int(frame.shape[0]),
            )

        # Auto-detect any illegal / security-critical signal we can see.
        illegal_findings = find_illegal_activity(frame, detections)
        # Draw fire/smoke synthetic boxes too.
        for finding in illegal_findings:
            if finding.get("kind") in {"fire", "smoke"}:
                detections = list(detections) + [finding["danger"]]

        for finding in illegal_findings:
            verify["dangers"] += 1
            danger_label = f"ILLEGAL {finding['label']}"
            verify["danger_labels"].append(danger_label)
            verify["labels"].insert(0, danger_label)
            if finding.get("kind") == "weapon" and finding.get("associated"):
                verify["issues"].append(
                    f"Illegal weapon in hand: {finding['label']} ({finding['confidence']:.0%})"
                )
            elif finding.get("kind") == "abandoned":
                verify["issues"].append(
                    f"Illegal unattended item: {finding['label']} ({finding['confidence']:.0%})"
                )
            elif finding.get("kind") in {"fire", "smoke"}:
                verify["issues"].append(
                    f"Illegal hazard: {finding['label']} ({finding['confidence']:.0%})"
                )
            else:
                verify["issues"].append(
                    f"Illegal item detected: {finding['label']} ({finding['confidence']:.0%})"
                )
            _auto_capture_hand_danger(frame, camera, finding)

        if person_dets:
            key = str(camera.id)
            now = time.time()
            should_log = now - _LAST_DETECT_EVENT.get(key, 0.0) >= 6.0
            event = None
            if should_log:
                _LAST_DETECT_EVENT[key] = now
                top = max(person_dets, key=lambda item: item.confidence)
                try:
                    event = create_detection_event(
                        camera=camera,
                        event_type="person_detected",
                        severity="info",
                        confidence=top.confidence,
                        model_name=object_detector.model_name,
                        model_version=object_detector.model_version,
                        payload={
                            "bbox": [top.x, top.y, top.width, top.height],
                            "person_count": len(person_dets),
                            "source": "live_preview",
                        },
                        run_hooks=False,
                    ).event
                except Exception:
                    event = None

            # Sort largest-first so primary faces get verified first; process ALL humans.
            ranked_people = sorted(
                person_dets,
                key=lambda item: max(1, item.width) * max(1, item.height),
                reverse=True,
            )
            for det in ranked_people[:12]:
                box = (det.x, det.y, det.width, det.height, det.confidence, det.class_name)
                try:
                    on_phone = track_mobile and person_using_phone(det, phone_findings)
                    # Slot sticky: phone YOLO flicker shouldn't drop mobile tracking.
                    slot_key = f"{camera.id}:slot:{int(det.x / 60)}:{int(det.y / 60)}"
                    now_phone = time.time()
                    if on_phone:
                        _LAST_MOBILE_USE[slot_key] = now_phone
                    elif track_mobile and (
                        now_phone - _LAST_MOBILE_USE.get(slot_key, 0.0) <= _MOBILE_USE_HOLD_SEC
                    ):
                        on_phone = True
                    result = _process_person_box(
                        frame,
                        box,
                        camera,
                        event,
                        det.confidence,
                        using_phone=on_phone,
                    )
                    if not result:
                        continue
                    # After face match, hold mobile state per saved person id.
                    person_id = result.get("person_id")
                    if track_mobile and person_id and result.get("status") == "verified":
                        pid_key = f"{camera.id}:person:{person_id}"
                        if result.get("using_phone") or on_phone:
                            _LAST_MOBILE_USE[pid_key] = now_phone
                            result["using_phone"] = True
                            if result.get("label") and "MOBILE" not in str(result["label"]):
                                result["label"] = f"{result.get('name') or result['label']} · MOBILE"
                            result["save_worthy"] = True
                            result["activity"] = "using mobile"
                        elif now_phone - _LAST_MOBILE_USE.get(pid_key, 0.0) <= _MOBILE_USE_HOLD_SEC:
                            result["using_phone"] = True
                            result["save_worthy"] = True
                            result["activity"] = "using mobile"
                            if result.get("name"):
                                result["label"] = f"{result['name']} · MOBILE"
                    person_labels[(det.x, det.y, det.width, det.height)] = result
                    status = result.get("status", "unknown")
                    if status in verify:
                        verify[status] += 1
                    # Capture: unknown + thief always; known only when on mobile (save_worthy).
                    save_worthy = result.get("save_worthy")
                    if save_worthy is None:
                        save_worthy = status in {"unknown", "blacklist"}
                    person_box = [det.x, det.y, det.width, det.height]
                    if save_worthy and status in {"unknown", "blacklist", "verified"}:
                        verify["person_boxes"].append(person_box)
                        if status in {"unknown", "blacklist"}:
                            verify["unknown_boxes"].append(person_box)
                        verify["activity"] = True
                        verify["people"].append(
                            {
                                "description": result.get("name")
                                or result.get("label")
                                or ("Unknown person" if status == "unknown" else "Person"),
                                "activity": result.get("activity")
                                or (
                                    "using mobile"
                                    if result.get("using_phone")
                                    else "present on camera"
                                ),
                                "status": status,
                                "person_id": result.get("person_id"),
                                "match_score": round(float(result.get("match_score") or 0), 3),
                                "bbox": person_box,
                            }
                        )
                    label = result.get("label") or result.get("name")
                    # Expose live labels — include pending so UI shows CHECKING / face status.
                    if label and status in {"verified", "unknown", "blacklist", "pending"}:
                        verify["labels"].append(str(label))
                    issue = result.get("issue")
                    if issue and (save_worthy or status in {"verified", "pending"}):
                        verify["issues"].append(str(issue))
                    if status == "pending" and result.get("announce") is None and not verify.get("announce"):
                        verify["announce"] = str(label or "checking face")
                    if result.get("announce") and not (
                        not THIEF_DETECTION_ENABLED
                        and str(result.get("announce", "")).upper().startswith("THIEF")
                    ):
                        verify["announce"] = str(result["announce"])
                    elif status == "blacklist" and THIEF_DETECTION_ENABLED:
                        verify["announce"] = f"THIEF detected: {result.get('name') or 'unknown'}"
                    elif status == "unknown":
                        verify["announce"] = "UNKNOWN person detected"
                    elif status == "verified":
                        verify["announce"] = str(result.get("name") or result.get("label") or "member")
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).warning(
                        "Person check failed for camera %s: %s", camera.id, exc
                    )
                    continue

            total_humans = (
                int(verify["verified"]) + int(verify["unknown"]) + int(verify["blacklist"])
            )
            if total_humans >= 2:
                names = [
                    str(p.get("description") or "").removeprefix("KNOWN ").strip()
                    for p in (verify.get("people") or [])
                    if isinstance(p, dict) and p.get("status") == "verified" and p.get("description")
                ]
                if names:
                    verify["announce"] = ", ".join(names[:6])
                    if verify["unknown"]:
                        verify["announce"] += f" + {verify['unknown']} unknown"
                else:
                    verify["announce"] = (
                        f"{total_humans} people"
                        + (f" · {verify['unknown']} unknown" if verify["unknown"] else "")
                        + (f" · {verify['blacklist']} threat" if verify["blacklist"] else "")
                    )

        # Sticky tracks: humans + phones in use (no chairs/bottles/etc.).
        from .detection import live_detection_tracker

        overlay_dets = list(person_dets) + list(phone_dets)
        if overlay_dets:
            sticky_dets, sticky_labels = live_detection_tracker.update(
                str(camera.id),
                overlay_dets,
                person_labels,
                frame_h=int(frame.shape[0]),
            )
        else:
            sticky_dets, sticky_labels, _names = live_detection_tracker.peek(str(camera.id))
        draw_labels = {**person_labels, **sticky_labels}
        if sticky_dets:
            _draw_detection_boxes(frame, sticky_dets, draw_labels)
        elif overlay_dets:
            _draw_detection_boxes(frame, overlay_dets, person_labels)

        labels = []
        if person_dets or any(d.class_name == "person" for d in sticky_dets):
            labels.append("person")
        if phone_dets or any(d.class_name == "cell phone" for d in sticky_dets):
            labels.append("MOBILE IN USE")
        for name in verify["labels"]:
            if name and name not in labels:
                labels.insert(0, name)
        person_count = sum(1 for d in sticky_dets if d.class_name == "person") or len(person_dets)
        return person_count, labels, verify
    except Exception:
        return 0, [], verify


def _auto_capture_hand_danger(frame, camera, finding: dict) -> None:
    """Save evidence + fire critical alert for any illegal finding."""
    import time
    import uuid
    from pathlib import Path

    import cv2
    from django.conf import settings

    from apps.events.pipeline import create_detection_event

    danger = finding["danger"]
    person = finding.get("person")
    weapon_type = finding["weapon_type"]
    kind = finding.get("kind") or "weapon"
    key = f"{camera.id}:{kind}:{weapon_type}"
    now = time.time()
    if now - _LAST_WEAPON_CAPTURE.get(key, 0.0) < 8.0:
        return
    _LAST_WEAPON_CAPTURE[key] = now

    if person is not None:
        x1 = max(0, min(person.x, danger.x) - 12)
        y1 = max(0, min(person.y, danger.y) - 12)
        x2 = min(frame.shape[1], max(person.x + person.width, danger.x + danger.width) + 12)
        y2 = min(frame.shape[0], max(person.y + person.height, danger.y + danger.height) + 12)
    else:
        x1 = max(0, danger.x - 20)
        y1 = max(0, danger.y - 20)
        x2 = min(frame.shape[1], danger.x + danger.width + 20)
        y2 = min(frame.shape[0], danger.y + danger.height + 20)

    crop = frame[y1:y2, x1:x2]
    snapshot_path = ""
    if crop.size > 0:
        ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            relative = (
                Path("threats")
                / timezone.now().strftime("%Y/%m/%d")
                / f"illegal-{kind}-{weapon_type.replace(' ', '_')}-{uuid.uuid4().hex[:12]}.jpg"
            )
            destination = Path(settings.MEDIA_ROOT) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(encoded.tobytes())
            snapshot_path = relative.as_posix()

    event_type = finding.get("event_type") or ("weapon" if kind == "weapon" else "risk_incident")
    if kind == "abandoned":
        event_type = "object_left"
    try:
        create_detection_event(
            camera=camera,
            event_type=event_type,
            severity="critical",
            confidence=float(finding["confidence"]),
            model_name="illegal-activity-detector",
            model_version="1.0",
            payload={
                "illegal": True,
                "kind": kind,
                "weapon_type": weapon_type,
                "label": finding["label"],
                "associated": bool(finding.get("associated")),
                "auto_capture": True,
                "in_hand": bool(finding.get("associated") and kind == "weapon"),
                "snapshot": snapshot_path,
                "snapshot_path": snapshot_path,
                "bbox": [danger.x, danger.y, danger.width, danger.height],
                "source": "realtime_illegal_detect",
            },
            run_hooks=True,
        )
    except Exception:
        pass


class EdgeDeviceListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "site__organization"

    def get_queryset(self):
        return EdgeDevice.objects.filter(site__organization_id__in=self.get_user_organizations()).select_related("site")

    def get_serializer_class(self):
        if self.request.method == "POST":
            return EdgeDeviceCreateResponseSerializer
        return EdgeDeviceSerializer

    def perform_create(self, serializer):
        site = serializer.validated_data["site"]
        require_role(self.request.user, site.organization, [Role.OWNER, Role.ADMIN])
        raw_key = EdgeDevice.generate_api_key()
        device = serializer.save(
            api_key_hash=EdgeDevice.hash_api_key(raw_key),
            api_key_prefix=raw_key[:8],
        )
        device.plain_api_key = raw_key  # noqa: attach for response only

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        device = serializer.instance
        data = EdgeDeviceCreateResponseSerializer(device).data
        data["api_key"] = device.plain_api_key
        return Response(data, status=status.HTTP_201_CREATED)


class EdgeHeartbeatView(APIView):
    authentication_classes = [EdgeAPIKeyAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = EdgeHeartbeatSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        device = getattr(request.user, "device", None)
        if device is None and isinstance(request.user, EdgeServiceUser):
            device = request.user.device

        if device is None and data.get("device_id"):
            device = EdgeDevice.objects.filter(id=data["device_id"], is_active=True).first()

        if device is None:
            return Response({"status": "ok", "registered": False}, status=status.HTTP_200_OK)

        device.last_seen_at = timezone.now()
        device.status = data.get("status", "online")
        if data.get("version"):
            device.version = data["version"]
        if data.get("metadata"):
            device.metadata = {**device.metadata, **data["metadata"]}
        device.save(update_fields=["last_seen_at", "status", "version", "metadata", "updated_at"])

        Camera.objects.filter(site=device.site, is_active=True).update(
            last_seen_at=timezone.now(), status="online"
        )

        return Response({"status": "ok", "registered": True, "device_id": str(device.id)})


class EdgeCameraListView(APIView):
    authentication_classes = [EdgeAPIKeyAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        device = getattr(request.user, "device", None)
        if device is None:
            return Response({"results": []})
        cameras = Camera.objects.filter(site=device.site, is_active=True).select_related("site", "zone").prefetch_related("site__zones")
        return Response({"results": EdgeCameraSerializer(cameras, many=True).data})
