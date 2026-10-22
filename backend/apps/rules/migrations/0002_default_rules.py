"""
Seed starter rules per organization and move per-camera person alerts into rules.

Also ticks the Weapon module on existing cameras: weapon/bag checks used to run
on every camera regardless of the checkbox and are now gated by it.
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Organization = apps.get_model("tenancy", "Organization")
    Camera = apps.get_model("cameras", "Camera")
    Rule = apps.get_model("rules", "Rule")

    from apps.rules.defaults import ensure_default_rules

    for org in Organization.objects.all():
        ensure_default_rules(org, Rule)

    for camera in Camera.objects.select_related("site"):
        modules = list(camera.enabled_modules or [])
        if "weapon_detection" not in modules:
            camera.enabled_modules = modules + ["weapon_detection"]
            camera.save(update_fields=["enabled_modules"])
        if camera.person_alert_enabled:
            rule = Rule.objects.create(
                organization_id=camera.site.organization_id,
                name=f"Person alert: {camera.name}",
                detect="any_person",
                start_time=camera.person_alert_start,
                end_time=camera.person_alert_end,
                sound="chime",
                popup=True,
                cooldown_sec=10,
                enabled=True,
            )
            rule.cameras.add(camera)


class Migration(migrations.Migration):
    dependencies = [
        ("rules", "0001_initial"),
        ("cameras", "0004_camera_person_alert"),
        ("tenancy", "0001_initial"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
