"""Drop cached per-camera rules whenever rules or their camera links change."""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from apps.cameras.models import Camera

from .engine import invalidate_rules
from .models import Rule


@receiver([post_save, post_delete], sender=Rule)
@receiver([post_save, post_delete], sender=Camera)
@receiver(m2m_changed, sender=Rule.cameras.through)
def _rules_changed(**kwargs) -> None:
    # Frequent bookkeeping saves (monitor ticks, trigger stamps) don't change matching.
    update_fields = kwargs.get("update_fields")
    if update_fields and set(update_fields) <= {
        "last_triggered_at",
        "last_seen_at",
        "status",
        "updated_at",
    }:
        return
    invalidate_rules()
