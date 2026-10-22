import uuid

from rest_framework import serializers

from apps.cameras.models import Camera

from .models import (
    BUILTIN_SOUNDS,
    CUSTOM_SOUND_PREFIX,
    MAX_SOUND_BYTES,
    SOUND_EXTENSIONS,
    AlertSound,
    Rule,
    RuleCheck,
)


class AlertSoundSerializer(serializers.ModelSerializer):
    sound_key = serializers.CharField(read_only=True)
    size_bytes = serializers.SerializerMethodField()

    class Meta:
        model = AlertSound
        fields = ("id", "organization", "name", "file", "sound_key", "size_bytes", "created_at")
        read_only_fields = ("id", "created_at")
        extra_kwargs = {"file": {"write_only": True}}

    def get_size_bytes(self, obj):
        try:
            return obj.file.size
        except Exception:
            return None

    def validate_file(self, value):
        name = (value.name or "").lower()
        if not name.endswith(SOUND_EXTENSIONS):
            raise serializers.ValidationError(
                f"Use an audio file: {', '.join(ext.lstrip('.') for ext in SOUND_EXTENSIONS)}."
            )
        if value.size > MAX_SOUND_BYTES:
            raise serializers.ValidationError("Keep tunes under 3 MB (a few seconds is best).")
        return value


class RuleSerializer(serializers.ModelSerializer):
    camera_ids = serializers.PrimaryKeyRelatedField(
        source="cameras", many=True, queryset=Camera.objects.all(), required=False
    )
    camera_names = serializers.SerializerMethodField()

    class Meta:
        model = Rule
        fields = (
            "id",
            "organization",
            "name",
            "detect",
            "params",
            "start_time",
            "end_time",
            "days",
            "sound",
            "speak",
            "speak_text",
            "save_photo",
            "alert_severity",
            "popup",
            "cooldown_sec",
            "apply_to_all",
            "enabled",
            "camera_ids",
            "camera_names",
            "last_triggered_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "last_triggered_at", "created_at", "updated_at")

    def get_camera_names(self, obj):
        return [camera.name for camera in obj.cameras.all()]

    def validate_days(self, value):
        days = sorted({int(d) for d in value or []})
        if any(d < 0 or d > 6 for d in days):
            raise serializers.ValidationError("Days are 0 (Mon) to 6 (Sun).")
        return days

    def validate_params(self, value):
        value = value or {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("params must be an object.")
        return {
            "person_types": [str(t) for t in value.get("person_types") or []],
            "person_ids": [str(p) for p in value.get("person_ids") or []],
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        sound = attrs.get("sound", getattr(instance, "sound", "")) or ""
        org_for_sound = attrs.get("organization", getattr(instance, "organization", None))
        if sound and sound not in BUILTIN_SOUNDS:
            try:
                sound_id = uuid.UUID(sound[len(CUSTOM_SOUND_PREFIX):]) if sound.startswith(CUSTOM_SOUND_PREFIX) else None
            except ValueError:
                sound_id = None
            exists = sound_id is not None and AlertSound.objects.filter(
                id=sound_id, organization=org_for_sound
            ).exists()
            if not exists:
                raise serializers.ValidationError({"sound": "Pick a built-in tune or one of your uploaded tunes."})
        start = attrs.get("start_time", getattr(instance, "start_time", None))
        end = attrs.get("end_time", getattr(instance, "end_time", None))
        if (start is None) != (end is None):
            raise serializers.ValidationError(
                {"end_time": "Set both From and To, or leave both empty for all day."}
            )
        org = attrs.get("organization", getattr(instance, "organization", None))
        cameras = attrs.get("cameras")
        if org is not None and cameras:
            foreign = [c.name for c in cameras if c.site.organization_id != org.id]
            if foreign:
                raise serializers.ValidationError(
                    {"camera_ids": f"Cameras from another organization: {', '.join(foreign)}"}
                )
        detect = attrs.get("detect", getattr(instance, "detect", None))
        if detect != RuleCheck.SAVED_PERSON:
            attrs["params"] = {}
        return attrs
