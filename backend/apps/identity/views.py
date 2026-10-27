from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.cameras.authentication import EdgeAPIKeyAuthentication, EdgeServiceUser
from apps.cameras.models import Camera
from apps.common.permissions import TenantQuerysetMixin, require_role
from apps.events.models import EventType, Severity
from apps.events.pipeline import create_detection_event
from apps.tenancy.models import Role

from .embeddings import (
    APPEARANCE_MODEL,
    APPEARANCE_VERSION,
    DEFAULT_MATCH_THRESHOLD,
    appearance_embedding_from_bytes,
)
from .models import (
    AttendanceRecord,
    BiometricConsent,
    FaceEmbedding,
    FaceImage,
    Person,
    PersonType,
    UnknownSighting,
    VisitorRegistration,
    VisitorStatus,
)
from .serializers import (
    AttendanceRecordSerializer,
    BiometricConsentSerializer,
    FaceEnrollmentSerializer,
    FaceMatchRequestSerializer,
    GalleryEmbeddingSerializer,
    PersonSerializer,
    PersonSightingSerializer,
    UnknownSightingSerializer,
    VisitorRegistrationSerializer,
)
from .services import (
    REQUIRED_ENROLL_ANGLES,
    attendance_summary,
    create_unknown_sighting,
    enroll_face_bytes,
    handle_visitor_face_match,
    invalidate_gallery_cache,
    mature_person_identity,
    recognize_embedding,
    record_attendance,
)


def _clamp_threshold(value) -> float:
    """SFace cosine for the same person sits around 0.36-0.6; keep client values sane."""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return DEFAULT_MATCH_THRESHOLD
    return min(0.6, max(0.25, threshold))


class PersonListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = PersonSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("person_type", "is_active")
    search_fields = ("full_name", "external_id", "email")

    def get_queryset(self):
        return Person.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        org = serializer.validated_data["organization"]
        require_role(self.request.user, org, [Role.OWNER, Role.ADMIN])
        serializer.save()


class PersonDetailView(TenantQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = PersonSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        return Person.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_update(self, serializer):
        require_role(
            self.request.user,
            serializer.instance.organization,
            [Role.OWNER, Role.ADMIN, Role.OPERATOR],
        )
        serializer.save()

    def perform_destroy(self, instance):
        require_role(self.request.user, instance.organization, [Role.OWNER, Role.ADMIN])
        instance.delete()


class ConsentListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = BiometricConsentSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        return BiometricConsent.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        org = serializer.validated_data["organization"]
        require_role(self.request.user, org, [Role.OWNER, Role.ADMIN])
        serializer.save()


class FaceEnrollView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        person_id = request.data.get("person_id")
        if not person_id:
            return Response({"detail": "person_id is required."}, status=400)

        person = Person.objects.filter(
            id=person_id,
            organization_id__in=request.user.memberships.values_list("organization_id", flat=True),
        ).first()
        if not person:
            return Response({"detail": "Person not found"}, status=404)
        require_role(request.user, person.organization, [Role.OWNER, Role.ADMIN])

        consent = None
        consent_id = request.data.get("consent_id")
        if consent_id:
            consent = BiometricConsent.objects.filter(
                id=consent_id, person=person, revoked_at__isnull=True
            ).first()
        else:
            consent = (
                BiometricConsent.objects.filter(person=person, revoked_at__isnull=True)
                .order_by("-granted_at")
                .first()
            )
        if consent is None:
            return Response(
                {"detail": "Valid biometric consent required before enrollment."},
                status=400,
            )

        # Prefer named angle fields: image_front, image_left, ...
        # Fallback: images[] ordered as front,left,right,up,down (all 5 required).
        angle_uploads: dict[str, object] = {}
        for angle in REQUIRED_ENROLL_ANGLES:
            upload = request.FILES.get(f"image_{angle}") or request.FILES.get(angle)
            if upload:
                angle_uploads[angle] = upload

        if not angle_uploads:
            uploads = list(request.FILES.getlist("images") or [])
            single = request.FILES.get("image")
            if single and not uploads:
                uploads = [single]
            for index, upload in enumerate(uploads[:5]):
                angle_uploads[REQUIRED_ENROLL_ANGLES[index]] = upload

        missing = [a for a in REQUIRED_ENROLL_ANGLES if a not in angle_uploads]
        if missing:
            return Response(
                {
                    "detail": (
                        "Full human view required. Upload ALL 5 angles before enrollment: "
                        "Front, Left, Right, Up, Down. "
                        f"Missing: {', '.join(missing)}."
                    ),
                    "missing_angles": missing,
                    "required_angles": list(REQUIRED_ENROLL_ANGLES),
                },
                status=400,
            )

        results = []
        errors = []
        created_embedding_ids: list[str] = []
        for index, angle in enumerate(REQUIRED_ENROLL_ANGLES):
            upload = angle_uploads[angle]
            raw = upload.read()
            try:
                result = enroll_face_bytes(
                    person=person,
                    raw_bytes=raw,
                    consent=consent,
                    filename=getattr(upload, "name", f"{angle}.jpg") or f"{angle}.jpg",
                    angle_label=angle,
                    make_primary=(angle == "front" or index == 0),
                )
                results.append(result)
                if result.get("embedding_id"):
                    created_embedding_ids.append(result["embedding_id"])
            except ValueError as exc:
                errors.append({"angle": angle, "detail": str(exc)})

        # Ultra-strong enrollment: every angle must succeed or we reject.
        if errors or len(results) < len(REQUIRED_ENROLL_ANGLES):
            # Roll back only embeddings from this failed attempt.
            if created_embedding_ids:
                FaceEmbedding.objects.filter(id__in=created_embedding_ids).update(is_active=False)
                invalidate_gallery_cache()
            detail = (
                "; ".join(f"{e['angle']}: {e['detail']}" for e in errors)
                if errors
                else "All 5 angles are required."
            )
            return Response(
                {
                    "detail": detail,
                    "issue": detail,
                    "errors": errors,
                    "required_angles": list(REQUIRED_ENROLL_ANGLES),
                },
                status=400,
            )

        mature = mature_person_identity(person)
        active_count = FaceEmbedding.objects.filter(person=person, is_active=True).count()
        primary_url = ""
        if results[0].get("primary_image_url"):
            primary_url = results[0]["primary_image_url"]

        message = (
            f"Ultra enrollment complete for {person.full_name}: "
            f"all 5 angles scanned and identity matured. "
            f"Live camera accuracy is now high."
        )
        if not mature.get("ok"):
            message = (
                f"Angles saved for {person.full_name}, but maturity incomplete. "
                f"Missing: {', '.join(mature.get('missing_angles') or [])}."
            )

        return Response(
            {
                "person_id": str(person.id),
                "full_name": person.full_name,
                "face_scanned": True,
                "angles_saved": len(results),
                "angles_failed": 0,
                "embedding_count": active_count,
                "face_particles": results[0].get("face_particles", 128),
                "face_engine": results[0].get("face_engine", "sface"),
                "identity_mature": bool(mature.get("ok")),
                "enrollment_complete": bool(mature.get("complete")),
                "mature_quality": mature.get("quality"),
                "scans": results,
                "errors": [],
                "issue": None,
                "primary_image_url": primary_url,
                "message": message,
            },
            status=201,
        )


class GalleryExportView(APIView):
    """Edge pulls active embeddings for on-device matching (optional)."""

    authentication_classes = [EdgeAPIKeyAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        device = getattr(request.user, "device", None)
        if device is None and not isinstance(request.user, EdgeServiceUser):
            return Response({"results": []})
        org_id = device.site.organization_id if device else None
        if org_id is None:
            # fallback edge key: export nothing unless org query param (dev)
            org_id = request.query_params.get("organization_id")
        if not org_id:
            return Response({"results": []})
        embeddings = FaceEmbedding.objects.filter(
            is_active=True, person__organization_id=org_id, person__is_active=True
        ).select_related("person")
        results = [
            {
                "person_id": str(e.person_id),
                "full_name": e.person.full_name,
                "person_type": e.person.person_type,
                "embedding_id": str(e.id),
                "vector": e.vector,
                "model_name": e.model_name,
            }
            for e in embeddings
            if e.vector
        ]
        return Response({"results": results, "count": len(results)})


class FaceMatchView(APIView):
    """Edge or dashboard posts embedding/image for recognition."""

    authentication_classes = [EdgeAPIKeyAuthentication]
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        serializer = FaceMatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        camera = None
        organization = None
        if data.get("camera_id"):
            camera = Camera.objects.select_related("site__organization").filter(id=data["camera_id"]).first()
            if camera:
                organization = camera.site.organization

        if isinstance(request.user, EdgeServiceUser) and request.user.device:
            if camera and camera.site_id != request.user.device.site_id:
                return Response({"detail": "Camera not on edge site"}, status=403)
            if organization is None:
                organization = request.user.device.site.organization

        if organization is None and data.get("organization_id"):
            from apps.tenancy.models import Organization

            organization = Organization.objects.filter(id=data["organization_id"]).first()

        if organization is None:
            return Response({"detail": "organization or camera required"}, status=400)

        embedding = data.get("embedding")
        if not embedding and data.get("image"):
            embedding = appearance_embedding_from_bytes(data["image"].read())
        if not embedding:
            return Response({"detail": "embedding or image required"}, status=400)

        person, score, _emb = recognize_embedding(
            organization.id, embedding, threshold=_clamp_threshold(data.get("threshold"))
        )

        if person and person.person_type == PersonType.BLACKLIST:
            event = None
            if camera:
                edge_device = getattr(request, "edge_device", None) or (
                    request.user.device if isinstance(request.user, EdgeServiceUser) else None
                )
                event = create_detection_event(
                    camera=camera,
                    event_type=EventType.UNKNOWN_PERSON,
                    severity=Severity.CRITICAL,
                    confidence=score,
                    model_name=APPEARANCE_MODEL,
                    model_version=APPEARANCE_VERSION,
                    payload={
                        "person_id": str(person.id),
                        "person_type": "blacklist",
                        "full_name": person.full_name,
                        "track_id": data.get("track_id"),
                        "match_score": score,
                    },
                    edge_device=edge_device,
                ).event
            return Response(
                {
                    "match": True,
                    "person_id": str(person.id),
                    "full_name": person.full_name,
                    "person_type": person.person_type,
                    "score": score,
                    "blacklist": True,
                    "event_id": str(event.id) if event else None,
                }
            )

        if person:
            event = None
            attendance = None
            visitor = None
            if camera:
                edge_device = getattr(request, "edge_device", None) or (
                    request.user.device if isinstance(request.user, EdgeServiceUser) else None
                )
                event = create_detection_event(
                    camera=camera,
                    event_type=EventType.FACE_RECOGNIZED,
                    severity=Severity.INFO,
                    confidence=score,
                    model_name=APPEARANCE_MODEL,
                    model_version=APPEARANCE_VERSION,
                    payload={
                        "person_id": str(person.id),
                        "full_name": person.full_name,
                        "person_type": person.person_type,
                        "track_id": data.get("track_id"),
                        "match_score": score,
                    },
                    edge_device=edge_device,
                ).event
            if data.get("create_attendance") and person.person_type in (
                PersonType.STUDENT,
                PersonType.INTERN,
                PersonType.JUNIOR,
                PersonType.SENIOR,
                PersonType.TEACHER,
                PersonType.MANAGER,
                PersonType.CEO,
                PersonType.STAFF,
                PersonType.EMPLOYEE,
                PersonType.CONTRACTOR,
                PersonType.VIP,
            ):
                attendance = record_attendance(person, camera, score, event=event)
            if person.person_type == PersonType.VISITOR:
                visitor = handle_visitor_face_match(person, score)
            return Response(
                {
                    "match": True,
                    "person_id": str(person.id),
                    "full_name": person.full_name,
                    "person_type": person.person_type,
                    "score": score,
                    "attendance_id": str(attendance.id) if attendance else None,
                    "visitor_id": str(visitor.id) if visitor else None,
                    "event_id": str(event.id) if event else None,
                }
            )

        # Unknown
        sighting = None
        event = None
        if data.get("create_unknown_if_miss"):
            if camera:
                edge_device = getattr(request, "edge_device", None) or (
                    request.user.device if isinstance(request.user, EdgeServiceUser) else None
                )
                event = create_detection_event(
                    camera=camera,
                    event_type=EventType.UNKNOWN_PERSON,
                    severity=Severity.WARNING,
                    confidence=data.get("confidence") or score,
                    model_name=APPEARANCE_MODEL,
                    model_version=APPEARANCE_VERSION,
                    payload={"track_id": data.get("track_id"), "match_score": score, "unknown": True},
                    edge_device=edge_device,
                ).event
            sighting = create_unknown_sighting(
                organization=organization,
                camera=camera,
                embedding=embedding,
                confidence=data.get("confidence") or score,
                track_id=data.get("track_id"),
                snapshot_path=data.get("snapshot_path", ""),
                event=event,
            )
        return Response(
            {
                "match": False,
                "score": score,
                "unknown_sighting_id": str(sighting.id) if sighting else None,
                "event_id": str(event.id) if event else None,
            }
        )


class AttendanceListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = AttendanceRecordSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("person", "direction", "is_late")

    def get_queryset(self):
        return AttendanceRecord.objects.filter(
            organization_id__in=self.get_user_organizations()
        ).select_related("person")


class AttendanceReportView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        org_ids = list(request.user.memberships.values_list("organization_id", flat=True))
        org_id = request.query_params.get("organization") or (str(org_ids[0]) if org_ids else None)
        if not org_id or org_id not in {str(o) for o in org_ids}:
            return Response({"detail": "organization required"}, status=400)
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        return Response(attendance_summary(org_id, date_from=date_from, date_to=date_to))


class VisitorListCreateView(TenantQuerysetMixin, generics.ListCreateAPIView):
    serializer_class = VisitorRegistrationSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("status", "site")

    def get_queryset(self):
        return VisitorRegistration.objects.filter(organization_id__in=self.get_user_organizations())

    def perform_create(self, serializer):
        org = serializer.validated_data["organization"]
        require_role(self.request.user, org, [Role.OWNER, Role.ADMIN, Role.OPERATOR])
        serializer.save()


class VisitorActionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk, action):
        visitor = VisitorRegistration.objects.filter(
            id=pk, organization_id__in=request.user.memberships.values_list("organization_id", flat=True)
        ).first()
        if not visitor:
            return Response({"detail": "Not found"}, status=404)
        require_role(request.user, visitor.organization, [Role.OWNER, Role.ADMIN, Role.OPERATOR])
        now = timezone.now()
        if action == "approve":
            visitor.status = VisitorStatus.APPROVED
            visitor.approved_by = request.user
        elif action == "deny":
            visitor.status = VisitorStatus.DENIED
            visitor.approved_by = request.user
        elif action == "checkin":
            visitor.status = VisitorStatus.CHECKED_IN
            visitor.checked_in_at = now
        elif action == "checkout":
            visitor.status = VisitorStatus.CHECKED_OUT
            visitor.checked_out_at = now
        else:
            return Response({"detail": "Unknown action"}, status=400)
        visitor.save()
        return Response(VisitorRegistrationSerializer(visitor).data)


class UnknownSightingListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = UnknownSightingSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("reviewed",)

    def get_queryset(self):
        return UnknownSighting.objects.filter(
            organization_id__in=self.get_user_organizations()
        ).select_related("camera")

    def delete(self, request):
        from pathlib import Path

        from django.conf import settings

        queryset = self.get_queryset()
        organizations = {sighting.organization for sighting in queryset.select_related("organization")}
        for organization in organizations:
            require_role(request.user, organization, [Role.OWNER, Role.ADMIN])

        snapshot_paths = set(queryset.exclude(snapshot_path="").values_list("snapshot_path", flat=True))
        deleted_count = queryset.count()
        queryset.delete()

        media_root = Path(settings.MEDIA_ROOT).resolve()
        for snapshot_path in snapshot_paths:
            if snapshot_path.startswith(("http://", "https://")):
                continue
            relative_path = snapshot_path.lstrip("/\\")
            media_prefix = settings.MEDIA_URL.strip("/\\")
            if media_prefix and relative_path.startswith(f"{media_prefix}/"):
                relative_path = relative_path[len(media_prefix) + 1 :]
            file_path = (media_root / relative_path).resolve()
            if media_root in file_path.parents and file_path.is_file():
                file_path.unlink(missing_ok=True)

        return Response({"deleted": deleted_count}, status=status.HTTP_200_OK)


class PersonSightingListView(TenantQuerysetMixin, generics.ListAPIView):
    serializer_class = PersonSightingSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"
    filterset_fields = ("person", "camera")

    def get_queryset(self):
        from .models import PersonSighting

        return PersonSighting.objects.filter(
            organization_id__in=self.get_user_organizations()
        ).select_related("person", "camera")


class PersonSightingDetailView(TenantQuerysetMixin, generics.DestroyAPIView):
    """Delete one profile log image / recognition sighting."""

    serializer_class = PersonSightingSerializer
    permission_classes = [permissions.IsAuthenticated]
    organization_field = "organization"

    def get_queryset(self):
        from .models import PersonSighting

        return PersonSighting.objects.filter(
            organization_id__in=self.get_user_organizations()
        ).select_related("organization")

    def perform_destroy(self, instance):
        from pathlib import Path

        from django.conf import settings

        require_role(
            self.request.user,
            instance.organization,
            [Role.OWNER, Role.ADMIN, Role.OPERATOR],
        )
        snapshot_path = (instance.snapshot_path or "").strip()
        instance.delete()
        if not snapshot_path or snapshot_path.startswith(("http://", "https://")):
            return
        media_root = Path(settings.MEDIA_ROOT).resolve()
        relative_path = snapshot_path.lstrip("/\\")
        media_prefix = settings.MEDIA_URL.strip("/\\")
        if media_prefix and relative_path.startswith(f"{media_prefix}/"):
            relative_path = relative_path[len(media_prefix) + 1 :]
        file_path = (media_root / relative_path).resolve()
        if media_root in file_path.parents and file_path.is_file():
            file_path.unlink(missing_ok=True)


class UnknownSightingReviewView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, pk):
        sighting = UnknownSighting.objects.filter(
            id=pk, organization_id__in=request.user.memberships.values_list("organization_id", flat=True)
        ).first()
        if not sighting:
            return Response({"detail": "Not found"}, status=404)
        require_role(request.user, sighting.organization, [Role.OWNER, Role.ADMIN, Role.OPERATOR])
        sighting.reviewed = bool(request.data.get("reviewed", True))
        sighting.notes = request.data.get("notes", sighting.notes)
        sighting.save()
        return Response(UnknownSightingSerializer(sighting).data)
