from django.utils import timezone
from rest_framework import serializers

from .embeddings import simulate_embedding_from_bytes
from .models import (
    AttendanceRecord,
    BiometricConsent,
    FaceEmbedding,
    FaceImage,
    Person,
    PersonSighting,
    UnknownSighting,
    VisitorRegistration,
)


class PersonSerializer(serializers.ModelSerializer):
    embedding_count = serializers.SerializerMethodField()
    image_count = serializers.IntegerField(source="face_images.count", read_only=True)
    primary_image_url = serializers.SerializerMethodField()

    class Meta:
        model = Person
        fields = (
            "id",
            "organization",
            "external_id",
            "full_name",
            "person_type",
            "email",
            "phone",
            "department",
            "shift_start",
            "shift_end",
            "is_active",
            "watch_mobile_activity",
            "metadata",
            "embedding_count",
            "image_count",
            "primary_image_url",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "embedding_count", "image_count")

    def get_embedding_count(self, obj):
        return obj.embeddings.filter(is_active=True).count()

    def get_primary_image_url(self, obj):
        # Prefer the original uploaded photo for profile display.
        face_image = (
            obj.face_images.filter(is_primary=True, source="upload").order_by("-created_at").first()
            or obj.face_images.filter(is_primary=True).exclude(source="enrollment_scan").order_by("-created_at").first()
            or obj.face_images.filter(is_primary=True).order_by("-created_at").first()
            or obj.face_images.exclude(source="enrollment_scan").order_by("-created_at").first()
            or obj.face_images.order_by("-created_at").first()
        )
        if not face_image:
            return None
        url = None
        if getattr(face_image, "image", None):
            try:
                url = face_image.image.url
            except ValueError:
                url = None
        if not url and face_image.image_path:
            from django.conf import settings

            media_url = settings.MEDIA_URL.rstrip("/") + "/"
            path = face_image.image_path.lstrip("/")
            url = f"{media_url}{path}"
        if not url:
            return None
        # Relative: absolute URLs built behind the frontend proxy lose the port.
        return url


class FaceImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = FaceImage
        fields = (
            "id",
            "person",
            "image",
            "image_path",
            "quality_score",
            "blur_score",
            "is_primary",
            "source",
            "metadata",
            "created_at",
        )
        read_only_fields = ("id", "created_at", "image_path")


class FaceEnrollmentSerializer(serializers.Serializer):
    person_id = serializers.UUIDField()
    consent_id = serializers.UUIDField(required=False, allow_null=True)
    quality_score = serializers.FloatField(required=False, default=0.8)
    blur_score = serializers.FloatField(required=False, default=100.0)
    is_primary = serializers.BooleanField(required=False, default=False)
    # Either upload file or provide raw bytes via base64 later; file via multipart
    image = serializers.ImageField(required=False)
    embedding = serializers.ListField(child=serializers.FloatField(), required=False)


class BiometricConsentSerializer(serializers.ModelSerializer):
    class Meta:
        model = BiometricConsent
        fields = (
            "id",
            "organization",
            "person",
            "consent_type",
            "granted_at",
            "revoked_at",
            "evidence_url",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class AttendanceRecordSerializer(serializers.ModelSerializer):
    person_name = serializers.CharField(source="person.full_name", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = (
            "id",
            "organization",
            "person",
            "person_name",
            "site",
            "camera",
            "event",
            "direction",
            "recorded_at",
            "confidence",
            "is_late",
            "is_overtime",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class VisitorRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = VisitorRegistration
        fields = (
            "id",
            "organization",
            "site",
            "person",
            "full_name",
            "email",
            "phone",
            "host_name",
            "host_email",
            "purpose",
            "valid_from",
            "valid_until",
            "status",
            "qr_code",
            "approved_by",
            "checked_in_at",
            "checked_out_at",
            "match_confidence",
            "metadata",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "qr_code",
            "approved_by",
            "checked_in_at",
            "checked_out_at",
            "match_confidence",
            "created_at",
            "updated_at",
        )


class UnknownSightingSerializer(serializers.ModelSerializer):
    camera_name = serializers.CharField(source="camera.name", read_only=True, allow_null=True)
    snapshot_url = serializers.SerializerMethodField()

    class Meta:
        model = UnknownSighting
        fields = (
            "id",
            "organization",
            "camera",
            "camera_name",
            "event",
            "snapshot_path",
            "snapshot_url",
            "confidence",
            "track_id",
            "gallery_person",
            "reviewed",
            "notes",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def get_snapshot_url(self, obj):
        if not obj.snapshot_path:
            return None
        if obj.snapshot_path.startswith(("http://", "https://")):
            return obj.snapshot_path

        from django.conf import settings

        media_url = settings.MEDIA_URL.rstrip("/") + "/"
        path = obj.snapshot_path.lstrip("/")
        if path.startswith(media_url.lstrip("/")):
            path = path[len(media_url.lstrip("/")) :]
        url = f"{media_url}{path}"
        # Relative: absolute URLs built behind the frontend proxy lose the port.
        return url


class PersonSightingSerializer(serializers.ModelSerializer):
    camera_name = serializers.CharField(source="camera.name", read_only=True, allow_null=True)
    person_name = serializers.CharField(source="person.full_name", read_only=True)
    snapshot_url = serializers.SerializerMethodField()

    class Meta:
        model = PersonSighting
        fields = (
            "id",
            "person",
            "person_name",
            "camera",
            "camera_name",
            "event",
            "snapshot_path",
            "snapshot_url",
            "confidence",
            "match_score",
            "notes",
            "created_at",
        )
        read_only_fields = fields

    def get_snapshot_url(self, obj):
        if not obj.snapshot_path:
            return None
        if obj.snapshot_path.startswith(("http://", "https://")):
            return obj.snapshot_path

        from django.conf import settings

        media_url = settings.MEDIA_URL.rstrip("/") + "/"
        path = obj.snapshot_path.lstrip("/")
        if path.startswith(media_url.lstrip("/")):
            path = path[len(media_url.lstrip("/")) :]
        url = f"{media_url}{path}"
        # Relative: absolute URLs built behind the frontend proxy lose the port.
        return url


class FaceMatchRequestSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField(required=False)
    camera_id = serializers.UUIDField(required=False)
    embedding = serializers.ListField(child=serializers.FloatField(), required=False)
    image = serializers.ImageField(required=False)
    threshold = serializers.FloatField(required=False, default=0.72)
    track_id = serializers.IntegerField(required=False, allow_null=True)
    confidence = serializers.FloatField(required=False, default=0.0)
    snapshot_path = serializers.CharField(required=False, allow_blank=True, default="")
    create_attendance = serializers.BooleanField(required=False, default=True)
    create_unknown_if_miss = serializers.BooleanField(required=False, default=True)


class GalleryEmbeddingSerializer(serializers.Serializer):
    person_id = serializers.UUIDField()
    full_name = serializers.CharField()
    person_type = serializers.CharField()
    embedding_id = serializers.UUIDField()
    vector = serializers.ListField(child=serializers.FloatField())
    model_name = serializers.CharField()
