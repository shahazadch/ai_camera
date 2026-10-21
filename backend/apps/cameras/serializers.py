from urllib.parse import urlparse

from rest_framework import serializers

from .models import Camera, EdgeDevice, VisionCapture


class CameraSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    organization = serializers.UUIDField(source="site.organization_id", read_only=True)
    zone_name = serializers.CharField(source="zone.name", read_only=True, allow_null=True)
    zone_polygon = serializers.JSONField(source="zone.polygon_json", read_only=True, allow_null=True)
    zone_is_restricted = serializers.BooleanField(source="zone.is_restricted", read_only=True, allow_null=True)

    class Meta:
        model = Camera
        fields = (
            "id",
            "site",
            "site_name",
            "organization",
            "zone",
            "zone_name",
            "zone_polygon",
            "zone_is_restricted",
            "name",
            "rtsp_url",
            "status",
            "enabled_modules",
            "last_seen_at",
            "metadata",
            "is_active",
            "auto_record",
            "person_alert_enabled",
            "person_alert_start",
            "person_alert_end",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "status", "last_seen_at", "created_at", "updated_at")

    def validate_rtsp_url(self, value):
        if not value:
            return value
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"rtsp", "rtsps", "http", "https"} or not parsed.hostname:
            raise serializers.ValidationError(
                "Enter a valid camera URL (rtsp://, rtsps://, http:// or https://)."
            )
        return value.strip()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        start = attrs.get("person_alert_start", getattr(instance, "person_alert_start", None))
        end = attrs.get("person_alert_end", getattr(instance, "person_alert_end", None))
        if (start is None) != (end is None):
            raise serializers.ValidationError(
                {"person_alert_end": "Set both From and To, or leave both empty for all day."}
            )
        return attrs


class EdgeCameraSerializer(CameraSerializer):
    """Edge-facing camera payload including site zones for the zone engine."""

    site_zones = serializers.SerializerMethodField()
    safety_config = serializers.SerializerMethodField()

    class Meta(CameraSerializer.Meta):
        fields = CameraSerializer.Meta.fields + ("site_zones", "safety_config")

    def get_safety_config(self, obj):
        try:
            profile = obj.safety_profile
        except Exception:
            return {}
        return {
            "enabled": profile.is_active,
            "environment": profile.environment,
            "fire_threshold": profile.fire_threshold,
            "fire_confirmation_frames": profile.fire_confirmation_frames,
            "smoke_threshold": profile.smoke_threshold,
            "smoke_persistence_seconds": profile.smoke_persistence_seconds,
            "smoke_sensitivity": profile.smoke_sensitivity,
            "weapon_threshold": profile.weapon_threshold,
            "detect_knives": profile.detect_knives,
            "fall_confirmation_seconds": profile.fall_confirmation_seconds,
            "fall_immobility_seconds": profile.fall_immobility_seconds,
            "ppe_required": profile.ppe_required,
            "ppe_model_enabled": profile.ppe_model_enabled,
            "ppe_contractor_only": profile.ppe_contractor_only,
            "ppe_shift_start": profile.ppe_shift_start.isoformat() if profile.ppe_shift_start else None,
            "ppe_shift_end": profile.ppe_shift_end.isoformat() if profile.ppe_shift_end else None,
            "ppe_zone_ids": [str(zone_id) for zone_id in profile.ppe_zones.values_list("id", flat=True)],
        }

    def get_site_zones(self, obj):
        # Prefer camera-linked analytics zones; fall back to all site zones
        linked = list(obj.analytics_zones.filter(is_active=True))
        zones = linked or list(obj.site.zones.filter(is_active=True))
        # Always include primary camera.zone if set
        if obj.zone_id and obj.zone and obj.zone.is_active and obj.zone not in zones:
            zones.append(obj.zone)
        result = []
        seen = set()
        for z in zones:
            sid = str(z.id)
            if sid in seen:
                continue
            seen.add(sid)
            result.append(
                {
                    "id": sid,
                    "name": z.name,
                    "polygon_json": z.polygon_json,
                    "is_restricted": z.is_restricted,
                    "is_active": z.is_active,
                    "priority": z.priority,
                    "loitering_seconds": z.loitering_seconds,
                    "visitor_loitering_seconds": z.visitor_loitering_seconds,
                    "ignore_employees": z.ignore_employees,
                    "max_occupancy": z.max_occupancy,
                    "access_start": z.access_start.isoformat() if z.access_start else None,
                    "access_end": z.access_end.isoformat() if z.access_end else None,
                    "after_hours_restricted": z.after_hours_restricted,
                    "entry_line_json": z.entry_line_json or [],
                    "config_version": z.config_version,
                }
            )
        return result



class EdgeDeviceSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source="site.name", read_only=True)
    organization = serializers.UUIDField(source="site.organization_id", read_only=True)
    api_key = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = EdgeDevice
        fields = (
            "id",
            "site",
            "site_name",
            "organization",
            "name",
            "status",
            "last_seen_at",
            "version",
            "metadata",
            "is_active",
            "api_key_prefix",
            "api_key",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "api_key_prefix", "status", "last_seen_at", "created_at", "updated_at")


class EdgeHeartbeatSerializer(serializers.Serializer):
    device_id = serializers.UUIDField(required=False)
    version = serializers.CharField(max_length=64, required=False, allow_blank=True)
    status = serializers.CharField(max_length=20, default="online")
    metadata = serializers.JSONField(required=False)


class EdgeDeviceCreateResponseSerializer(EdgeDeviceSerializer):
    api_key = serializers.CharField(read_only=True)


class VisionCaptureSerializer(serializers.ModelSerializer):
    camera_name = serializers.CharField(source="camera.name", read_only=True)
    site_name = serializers.CharField(source="camera.site.name", read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = VisionCapture
        fields = (
            "id",
            "organization",
            "camera",
            "camera_name",
            "site_name",
            "event",
            "image_path",
            "image_url",
            "summary",
            "scene",
            "people",
            "detected_objects",
            "risks",
            "text_visible",
            "confidence",
            "model_name",
            "provider",
            "created_at",
        )
        read_only_fields = fields

    def get_image_url(self, obj):
        if not obj.image_path:
            return None
        if obj.image_path.startswith(("http://", "https://")):
            return obj.image_path
        from django.conf import settings

        media_url = settings.MEDIA_URL.rstrip("/") + "/"
        path = obj.image_path.lstrip("/")
        url = f"{media_url}{path}"
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url
