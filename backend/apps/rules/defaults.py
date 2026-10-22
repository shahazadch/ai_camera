"""Starter rules every organization gets (attached to no camera until switched on)."""

from datetime import time

DEFAULT_RULES = [
    {
        "name": "Night intruder",
        "detect": "any_person",
        "start_time": time(22, 0),
        "end_time": time(6, 0),
        "sound": "siren",
        "save_photo": True,
        "alert_severity": "critical",
    },
    {
        "name": "Unknown visitor",
        "detect": "unknown_person",
        "sound": "chime",
        "save_photo": True,
        "alert_severity": "warning",
    },
    {
        "name": "Staff arrived",
        "detect": "saved_person",
        "params": {"person_types": ["employee", "staff"], "person_ids": []},
        "start_time": time(8, 0),
        "end_time": time(11, 0),
    },
    {
        "name": "Stairs",
        "detect": "stairs",
        "sound": "horn",
        "cooldown_sec": 10,
    },
]


def ensure_default_rules(organization, rule_model=None) -> None:
    if rule_model is None:
        from .models import Rule as rule_model
    for spec in DEFAULT_RULES:
        defaults = {k: v for k, v in spec.items() if k != "name"}
        defaults.setdefault("popup", True)
        rule_model.objects.get_or_create(organization=organization, name=spec["name"], defaults=defaults)
