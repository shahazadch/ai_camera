from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.utils import timezone

from apps.cameras.authentication import EdgeAPIKeyAuthentication, EdgeServiceUser
from apps.cameras.models import Camera
from apps.common.permissions import TenantQuerysetMixin, require_role
from apps.events.models import DetectionEvent
from apps.tenancy.models import Role

from .models import (
    NotificationDeliveryLog,
    NotificationJob,
    PasswordPolicy,
    RecordingJob,
    RecordingStatus,
    SessionPolicy,
    SIEMForwardJob,
    SnapshotObject,
    SSOProvider,
)
from .serializers import (
    NotificationDeliveryLogSerializer,
    NotificationJobSerializer,
    PasswordPolicySerializer,
    RecordingJobSerializer,
    SessionPolicySerializer,
    SIEMForwardJobSerializer,
    SnapshotObjectSerializer,
    SSOProviderSerializer,
)
from .services import refresh_signed_url, register_snapshot, store_and_register_upload


class NotificationJobListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = NotificationJobSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("status", "channel")

    def get_queryset(self):
        return NotificationJob.objects.filter(organization_id__in=self.get_user_organizations())


class NotificationDeliveryLogListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = NotificationDeliveryLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return NotificationDeliveryLog.objects.filter(
            job__organization_id__in=self.get_user_organizations()
        ).select_related("job")


class RecordingJobListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = RecordingJobSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("status", "vendor", "camera")

    def get_queryset(self):
        return RecordingJob.objects.filter(organization_id__in=self.get_user_organizations()).select_related(
            "camera"
        )


class RecordingCameraSummaryView(APIView):
    """One row per camera for the Recordings page: clip count, newest clip, recording now."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Max, Q

        from apps.cameras.continuous_recorder import live_playlist_url, recorder_manager

        org_ids = list(request.user.memberships.values_list("organization_id", flat=True))
        ready = Q(recording_jobs__status=RecordingStatus.READY)
        cameras = (
            Camera.objects.filter(site__organization_id__in=org_ids)
            .annotate(
                clip_count=Count("recording_jobs", filter=ready),
                last_clip_at=Max("recording_jobs__started_at", filter=ready),
            )
            .order_by("name")
        )
        live = {w["camera_id"]: w for w in recorder_manager.status()["workers"]}
        return Response(
            [
                {
                    "camera_id": str(camera.id),
                    "camera_name": camera.name,
                    "status": camera.status,
                    "auto_record": camera.auto_record,
                    "recording_now": bool(live.get(str(camera.id), {}).get("recording")),
                    "live_url": live_playlist_url(camera.id)
                    if live.get(str(camera.id), {}).get("recording")
                    else None,
                    "last_error": live.get(str(camera.id), {}).get("last_error", "")
                    or (camera.metadata or {}).get("last_recording_error", ""),
                    "clip_count": camera.clip_count,
                    "last_clip_at": camera.last_clip_at,
                }
                for camera in cameras
            ]
        )


class RecordingBulkDeleteView(APIView):
    """Delete chosen recordings (rows + video files). Owners/admins only."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from pathlib import Path

        from apps.cameras.continuous_recorder import delete_clip
        from apps.tenancy.models import UserMembership

        ids = request.data.get("ids")
        if not isinstance(ids, list) or not ids:
            return Response({"ids": "Send a list of recording ids."}, status=status.HTTP_400_BAD_REQUEST)
        manage_orgs = list(
            UserMembership.objects.filter(user=request.user, role__in=[Role.OWNER, Role.ADMIN]).values_list(
                "organization_id", flat=True
            )
        )
        if not manage_orgs:
            return Response(
                {"detail": "Only owners and admins can delete recordings."}, status=status.HTTP_403_FORBIDDEN
            )
        try:
            jobs = RecordingJob.objects.filter(id__in=ids, organization_id__in=manage_orgs)
            media = Path(settings.MEDIA_ROOT)
            deleted = 0
            for job in jobs:
                delete_clip(media, job.clip_key)
                job.delete()
                deleted += 1
        except (ValueError, DjangoValidationError):
            return Response({"ids": "Invalid recording id."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"deleted": deleted})


class RecordingSettingsView(APIView):
    """How long recordings are kept and which camera stream is recorded."""

    permission_classes = [permissions.IsAuthenticated]

    def _organization(self, request):
        from apps.tenancy.models import Organization, UserMembership

        memberships = UserMembership.objects.filter(user=request.user).select_related("organization")
        wanted = request.query_params.get("organization") or request.data.get("organization")
        if wanted:
            memberships = memberships.filter(organization_id=wanted)
        membership = memberships.order_by("created_at").first()
        return membership.organization if membership else None

    def _payload(self, organization):
        from apps.cameras.continuous_recorder import recorder_manager, recording_settings, recordings_disk_usage

        return {
            "organization": str(organization.id),
            **recording_settings(organization),
            "enabled": bool(settings.CONTINUOUS_RECORDING),
            "disk_usage_bytes": recordings_disk_usage(),
            "cameras_recording": recorder_manager.status()["count"],
        }

    def get(self, request):
        organization = self._organization(request)
        if organization is None:
            return Response({"detail": "No organization."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(organization))

    def patch(self, request):
        import threading

        from apps.cameras.continuous_recorder import purge_recordings, recorder_manager

        organization = self._organization(request)
        if organization is None:
            return Response({"detail": "No organization."}, status=status.HTTP_404_NOT_FOUND)
        require_role(request.user, organization, [Role.OWNER, Role.ADMIN])
        current = dict(organization.settings or {})
        for field, key in (
            ("retention_days", "recording_retention_days"),
            ("event_retention_days", "recording_event_retention_days"),
        ):
            if field not in request.data:
                continue
            try:
                days = int(request.data[field])
            except (TypeError, ValueError):
                days = 0
            if not 1 <= days <= 365:
                return Response({field: "Choose 1 to 365 days."}, status=status.HTTP_400_BAD_REQUEST)
            current[key] = days
        restart = False
        if "live_rewind_minutes" in request.data:
            try:
                minutes = int(request.data["live_rewind_minutes"])
            except (TypeError, ValueError):
                minutes = 0
            if not 10 <= minutes <= 360:
                return Response(
                    {"live_rewind_minutes": "Choose 10 to 360 minutes."}, status=status.HTTP_400_BAD_REQUEST
                )
            restart = minutes != current.get("recording_live_rewind_minutes", 120)
            current["recording_live_rewind_minutes"] = minutes
        if "quality" in request.data:
            quality = request.data["quality"]
            if quality not in {"standard", "high"}:
                return Response({"quality": "Use standard or high."}, status=status.HTTP_400_BAD_REQUEST)
            restart = restart or quality != current.get("recording_quality", "standard")
            current["recording_quality"] = quality
        organization.settings = current
        organization.save(update_fields=["settings", "updated_at"])
        # Apply now: shorter retention deletes at once; new quality/rewind restarts recorders.
        threading.Thread(target=purge_recordings, daemon=True).start()
        if restart:
            threading.Thread(target=recorder_manager.sync_from_db, daemon=True).start()
        return Response(self._payload(organization))


class SnapshotListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = SnapshotObjectSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("camera",)

    def get_queryset(self):
        return SnapshotObject.objects.filter(organization_id__in=self.get_user_organizations()).select_related(
            "camera"
        )


class SnapshotDetailView(TenantQuerysetMixin, generics.RetrieveAPIView):
    serializer_class = SnapshotObjectSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        return SnapshotObject.objects.filter(organization_id__in=self.get_user_organizations())

    def retrieve(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.storage_backend in {"s3", "minio"} or request.query_params.get("resign") == "1":
            try:
                refresh_signed_url(obj)
            except Exception:
                pass
        return Response(self.get_serializer(obj).data)


class SnapshotUploadView(APIView):
    """Edge uploads snapshot file/metadata → evidence storage + signed URL."""

    authentication_classes = [EdgeAPIKeyAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        camera_id = request.data.get("camera_id")
        event_id = request.data.get("event_id")
        camera = Camera.objects.select_related("site__organization").filter(id=camera_id, is_active=True).first()
        if not camera:
            return Response({"error": {"code": "NOT_FOUND", "message": "Camera not found."}}, status=404)
        if isinstance(request.user, EdgeServiceUser) and request.user.device:
            if camera.site_id != request.user.device.site_id:
                return Response({"error": {"code": "FORBIDDEN", "message": "Camera not on edge site."}}, status=403)

        event = None
        if event_id:
            event = DetectionEvent.objects.filter(id=event_id, camera=camera).first()

        upload = request.FILES.get("file")
        if upload:
            obj = store_and_register_upload(
                organization=camera.site.organization,
                camera=camera,
                file_obj=upload,
                event=event,
                content_type=upload.content_type or "image/jpeg",
                uploaded_by_edge=True,
            )
            return Response(SnapshotObjectSerializer(obj).data, status=status.HTTP_201_CREATED)

        snapshot_key = request.data.get("snapshot_key") or ""
        if not snapshot_key:
            return Response(
                {"error": {"code": "BAD_REQUEST", "message": "file or snapshot_key required."}},
                status=400,
            )
        obj = register_snapshot(
            organization=camera.site.organization,
            camera=camera,
            snapshot_key=snapshot_key,
            snapshot_url=request.data.get("snapshot_url", ""),
            thumbnail_url=request.data.get("thumbnail_url", ""),
            event=event,
            content_type=request.data.get("content_type") or "image/jpeg",
            mime_type=request.data.get("mime_type") or "",
            checksum=request.data.get("checksum") or "",
            width=int(request.data["width"]) if request.data.get("width") else None,
            height=int(request.data["height"]) if request.data.get("height") else None,
            size_bytes=int(request.data.get("size_bytes") or 0),
            uploaded_by_edge=True,
            storage_backend=request.data.get("storage_backend") or "local",
        )
        return Response(SnapshotObjectSerializer(obj).data, status=status.HTTP_201_CREATED)


class SSOProviderListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = SSOProviderSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        return SSOProvider.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        from apps.tenancy.models import Organization

        org_id = self.request.data.get("organization") or self.get_user_organizations()[0]
        org = Organization.objects.get(id=org_id)
        require_role(self.request.user, org, [Role.OWNER, Role.ADMIN])
        serializer.save(organization=org)


class PasswordPolicyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.tenancy.models import Organization

        org_id = request.query_params.get("organization") or request.user.memberships.values_list(
            "organization_id", flat=True
        ).first()
        org = Organization.objects.get(id=org_id)
        policy, _ = PasswordPolicy.objects.get_or_create(organization=org)
        return Response(PasswordPolicySerializer(policy).data)

    def put(self, request):
        from apps.tenancy.models import Organization

        org_id = request.data.get("organization") or request.user.memberships.values_list(
            "organization_id", flat=True
        ).first()
        org = Organization.objects.get(id=org_id)
        require_role(request.user, org, [Role.OWNER, Role.ADMIN])
        policy, _ = PasswordPolicy.objects.get_or_create(organization=org)
        ser = PasswordPolicySerializer(policy, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


class SessionPolicyView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from apps.tenancy.models import Organization

        org_id = request.query_params.get("organization") or request.user.memberships.values_list(
            "organization_id", flat=True
        ).first()
        org = Organization.objects.get(id=org_id)
        policy, _ = SessionPolicy.objects.get_or_create(organization=org)
        return Response(SessionPolicySerializer(policy).data)

    def put(self, request):
        from apps.tenancy.models import Organization

        org_id = request.data.get("organization") or request.user.memberships.values_list(
            "organization_id", flat=True
        ).first()
        org = Organization.objects.get(id=org_id)
        require_role(request.user, org, [Role.OWNER, Role.ADMIN])
        policy, _ = SessionPolicy.objects.get_or_create(organization=org)
        ser = SessionPolicySerializer(policy, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(ser.data)


class SIEMJobListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = SIEMForwardJobSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("status", "target")

    def get_queryset(self):
        return SIEMForwardJob.objects.filter(organization_id__in=self.get_user_organizations())


class MetricsView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        if not settings.PROMETHEUS_ENABLED:
            return Response({"error": "metrics disabled"}, status=404)
        from apps.ops.observability import render_metrics

        body, content_type = render_metrics()
        from django.http import HttpResponse

        return HttpResponse(body, content_type=content_type)


class ReadyView(APIView):
    """Kubernetes readiness: DB reachable."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        try:
            connection.ensure_connection()
            return Response({"status": "ready", "timestamp": timezone.now().isoformat()})
        except Exception as exc:
            return Response({"status": "not_ready", "error": str(exc)}, status=503)


class LiveView(APIView):
    """Kubernetes liveness probe."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response({"status": "alive", "timestamp": timezone.now().isoformat()})
