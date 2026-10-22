"""Detection rules: defined once per organization, switched on per camera."""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.cameras.models import Camera
from apps.common.models import TimestampedModel, UUIDModel
from apps.tenancy.models import Organization


class RuleCheck(models.TextChoices):
    ANY_PERSON = "any_person", "Any person"
    UNKNOWN_PERSON = "unknown_person", "Unknown person"
    SAVED_PERSON = "saved_person", "Saved person"
    MOBILE_USE = "mobile_use", "Person using mobile"
    STAIRS = "stairs", "Coming up the stairs"


# Checks that need the face pass (YuNet + SFace) to run on the camera.
FACE_CHECKS = frozenset({RuleCheck.UNKNOWN_PERSON, RuleCheck.SAVED_PERSON})


# Tunes the laptop player generates itself. Uploaded tunes are "custom:<AlertSound id>".
BUILTIN_SOUNDS = ("chime", "bell", "doorbell", "beep", "horn", "alarm", "siren")
CUSTOM_SOUND_PREFIX = "custom:"
SOUND_EXTENSIONS = (".wav", ".mp3", ".m4a", ".aiff", ".aif", ".ogg")
MAX_SOUND_BYTES = 3 * 1024 * 1024


class AlertSound(UUIDModel, TimestampedModel):
    """A tune uploaded from the dashboard; the laptop player downloads and caches it."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="alert_sounds")
    name = models.CharField(max_length=80)
    file = models.FileField(upload_to="alert_sounds/")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def sound_key(self) -> str:
        return f"{CUSTOM_SOUND_PREFIX}{self.id}"


class RuleSeverity(models.TextChoices):
    NONE = "", "No alert"
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"


class Rule(UUIDModel, TimestampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="rules")
    name = models.CharField(max_length=120)
    detect = models.CharField(max_length=32, choices=RuleCheck.choices)
    # saved_person: {"person_types": [...], "person_ids": [...]} — empty = anyone saved.
    params = models.JSONField(default=dict, blank=True)

    # Schedule in ALERT_TIME_ZONE. Both times empty = all day; start > end wraps midnight.
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    # Weekdays 0=Mon … 6=Sun; empty = every day.
    days = models.JSONField(default=list, blank=True)

    # "" = silent, a BUILTIN_SOUNDS name, or "custom:<AlertSound id>".
    sound = models.CharField(max_length=64, blank=True, default="")
    # Laptop says this out loud after the sound. Placeholders: {rule} {camera} {name}.
    speak = models.BooleanField(default=False)
    speak_text = models.CharField(max_length=200, blank=True, default="")
    save_photo = models.BooleanField(default=False)
    alert_severity = models.CharField(max_length=16, choices=RuleSeverity.choices, blank=True, default="")
    popup = models.BooleanField(default=True)
    cooldown_sec = models.PositiveIntegerField(default=30)

    apply_to_all = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    cameras = models.ManyToManyField(Camera, blank=True, related_name="rules")
    last_triggered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def speech(self, camera_name: str, person_name: str = "") -> str:
        if not self.speak:
            return ""
        template = self.speak_text.strip() or (
            "{name} at {camera}" if person_name else "{rule} at {camera}"
        )
        values = {"rule": self.name, "camera": camera_name or "camera", "name": person_name or "Someone"}
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return template

    def active_at(self, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        local = (now or timezone.now()).astimezone(ZoneInfo(settings.ALERT_TIME_ZONE))
        start, end = self.start_time, self.end_time
        day = local.weekday()
        if start is not None and end is not None and start > end and local.time() < end:
            # Early-morning part of an overnight window belongs to the previous day.
            day = (day - 1) % 7
        if self.days and day not in self.days:
            return False
        if start is None or end is None:
            return True
        t = local.time()
        if start <= end:
            return start <= t < end
        return t >= start or t < end
