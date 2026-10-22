import hashlib
import secrets
from django.db import models

from apps.common.models import TimestampedModel, UUIDModel
from apps.sites.models import Site, Zone


class CameraStatus(models.TextChoices):
    ONLINE = "online", "Online"
    OFFLINE = "offline", "Offline"
    ERROR = "error", "Error"
    MAINTENANCE = "maintenance", "Maintenance"


class AIModule(models.TextChoices):
    FACE = "face_recognition", "Face Recognition"
    TRACKING = "multi_person_tracking", "Multi-Person Tracking"
    ACTIVITY = "activity_recognition", "Activity Recognition"
    INTRUSION = "intrusion_detection", "Intrusion Detection"
    UNKNOWN = "unknown_person", "Unknown Person Detection"
    MOBILE = "mobile_use_tracking", "Mobile Use Tracking"
    VISITOR = "visitor_management", "Visitor Management"
    ATTENDANCE = "attendance", "Attendance"
    FIRE = "fire_smoke", "Fire/Smoke Detection"
    WEAPON = "weapon_detection", "Weapon Detection"


class Camera(UUIDModel, TimestampedModel):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="cameras")
    zone = models.ForeignKey(Zone, on_delete=models.SET_NULL, null=True, blank=True, related_name="cameras")
    name = models.CharField(max_length=255)
    rtsp_url = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=20, choices=CameraStatus.choices, default=CameraStatus.OFFLINE)
    enabled_modules = models.JSONField(default=list, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    # Home / NVR cameras: keep writing rolling video clips automatically.
    auto_record = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("site", "name")

    def __str__(self):
        return f"{self.name} @ {self.site.name}"

    @property
    def organization(self):
        return self.site.organization


class EdgeDevice(UUIDModel, TimestampedModel):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="edge_devices")
    name = models.CharField(max_length=255)
    api_key_hash = models.CharField(max_length=128)
    api_key_prefix = models.CharField(max_length=8, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="offline")
    version = models.CharField(max_length=64, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("site", "name")

    def __str__(self):
        return f"{self.name} ({self.site.name})"

    @property
    def organization(self):
        return self.site.organization

    @staticmethod
    def hash_api_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @classmethod
    def generate_api_key(cls) -> str:
        return secrets.token_urlsafe(32)

    def verify_api_key(self, raw_key: str) -> bool:
        return self.api_key_hash == self.hash_api_key(raw_key)


class VisionCapture(UUIDModel, TimestampedModel):
    """LLM vision analysis of a captured camera frame."""

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="vision_captures",
    )
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="vision_captures")
    event = models.ForeignKey(
        "events.DetectionEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vision_captures",
    )
    image_path = models.CharField(max_length=512, blank=True, default="")
    summary = models.TextField(blank=True, default="")
    scene = models.TextField(blank=True, default="")
    people = models.JSONField(default=list, blank=True)
    detected_objects = models.JSONField(default=list, blank=True)
    risks = models.JSONField(default=list, blank=True)
    text_visible = models.JSONField(default=list, blank=True)
    confidence = models.FloatField(default=0.0)
    model_name = models.CharField(max_length=128, blank=True, default="")
    provider = models.CharField(max_length=64, blank=True, default="")
    raw_response = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "-created_at"])]

    def __str__(self):
        return f"VisionCapture {self.camera.name} @ {self.created_at}"
