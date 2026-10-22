from datetime import time, timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.alerts.models import AlertRule
from apps.analytics.models import AnalyticsRule, RiskLevel, RuleAction
from apps.cameras.models import Camera, EdgeDevice
from apps.events.models import EventType, Severity
from apps.identity.models import (
    BiometricConsent,
    Person,
    PersonType,
    VisitorRegistration,
    VisitorStatus,
)
from apps.rules.defaults import ensure_default_rules
from apps.safety.models import SafetyProfile
from apps.sites.models import Site, Zone
from apps.tenancy.models import Organization, Role, UserMembership


class Command(BaseCommand):
    help = "Seed development data for SentinelVision AI"

    def handle(self, *args, **options):
        org, _ = Organization.objects.get_or_create(
            slug="acme-security",
            defaults={"name": "ACME Security Corp", "settings": {"retention_days": 90}},
        )
        site, _ = Site.objects.get_or_create(
            organization=org,
            name="Headquarters",
            defaults={"address": "100 Security Blvd", "timezone": "America/New_York"},
        )
        zone, _ = Zone.objects.get_or_create(
            site=site,
            name="Server Room",
            defaults={
                "polygon_json": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                "is_restricted": True,
                "priority": "critical",
                "loitering_seconds": 120,
                "visitor_loitering_seconds": 60,
                "max_occupancy": 4,
                "access_start": time(9, 0),
                "access_end": time(18, 0),
                "after_hours_restricted": True,
                "entry_line_json": [[0.1, 0.5], [0.9, 0.5]],
            },
        )
        zone.priority = "critical"
        zone.loitering_seconds = 120
        zone.max_occupancy = 4
        zone.access_start = time(9, 0)
        zone.access_end = time(18, 0)
        zone.after_hours_restricted = True
        zone.entry_line_json = [[0.1, 0.5], [0.9, 0.5]]
        zone.save()

        lobby, _ = Zone.objects.get_or_create(
            site=site,
            name="Lobby Queue",
            defaults={
                "polygon_json": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
                "is_restricted": False,
                "priority": "medium",
                "loitering_seconds": 180,
                "max_occupancy": 20,
            },
        )
        camera, _ = Camera.objects.get_or_create(
            site=site,
            name="Lobby Camera 01",
            defaults={
                "zone": zone,
                # Placeholder only: inactive so nothing tries to connect until a
                # real camera URL is entered in the UI.
                "rtsp_url": "rtsp://demo:demo@localhost:8554/stream",
                "status": "offline",
                "is_active": False,
                "enabled_modules": [
                    "multi_person_tracking",
                    "intrusion_detection",
                    "face_recognition",
                    "attendance",
                    "unknown_person",
                ],
            },
        )
        camera.enabled_modules = [
            "multi_person_tracking",
            "intrusion_detection",
            "face_recognition",
            "attendance",
            "unknown_person",
            "crowd_analytics",
            "object_left_removed",
            "fire_detection",
            "smoke_detection",
            "weapon_detection",
            "fall_detection",
            "ppe_detection",
        ]
        camera.zone = zone
        camera.save()

        EdgeDevice.objects.get_or_create(
            site=site,
            name="Edge Node HQ-01",
            defaults={
                "api_key_hash": EdgeDevice.hash_api_key("edge-dev-api-key-change-in-production"),
                "api_key_prefix": "edge-dev",
                "status": "online",
                "version": "0.5.0-m5",
            },
        )
        zone.linked_cameras.add(camera)
        lobby.linked_cameras.add(camera)
        safety_profile, _ = SafetyProfile.objects.get_or_create(
            camera=camera,
            defaults={
                "environment": "indoor",
                "fire_threshold": 0.75,
                "fire_confirmation_frames": 3,
                "smoke_threshold": 0.55,
                "smoke_persistence_seconds": 8,
                "weapon_threshold": 0.65,
                "detect_knives": False,
                "fall_confirmation_seconds": 2,
                "fall_immobility_seconds": 20,
                "ppe_required": ["helmet", "safety_vest"],
                "ppe_model_enabled": False,
                "ppe_contractor_only": True,
                "ppe_shift_start": time(7, 0),
                "ppe_shift_end": time(19, 0),
            },
        )
        safety_profile.ppe_zones.add(lobby)

        admin_user, user_created = User.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@acme-security.com", "is_staff": True},
        )
        if user_created:
            admin_user.set_password("admin123")
            admin_user.save()
        UserMembership.objects.get_or_create(user=admin_user, organization=org, defaults={"role": Role.OWNER})
        ensure_default_rules(org)
        operator, op_created = User.objects.get_or_create(
            username="operator",
            defaults={"email": "operator@acme-security.com"},
        )
        if op_created:
            operator.set_password("operator123")
            operator.save()
        UserMembership.objects.get_or_create(user=operator, organization=org, defaults={"role": Role.OPERATOR})

        for name, etype, sev, conf in [
            ("Intrusion Critical", EventType.INTRUSION, Severity.CRITICAL, 0.7),
            ("Person Detected Warning", EventType.PERSON_DETECTED, Severity.WARNING, 0.5),
            ("Unknown Person Alert", EventType.UNKNOWN_PERSON, Severity.WARNING, 0.3),
            ("Face Recognized Info", EventType.FACE_RECOGNIZED, Severity.INFO, 0.4),
            ("Loitering Alert", EventType.LOITERING, Severity.WARNING, 0.5),
            ("Object Left Behind", EventType.OBJECT_LEFT, Severity.WARNING, 0.5),
            ("Verified Fire Critical", EventType.FIRE, Severity.CRITICAL, 0.7),
            ("Persistent Smoke", EventType.SMOKE, Severity.WARNING, 0.5),
            ("Weapon Critical", EventType.WEAPON, Severity.CRITICAL, 0.6),
            ("Fall Critical", EventType.FALL, Severity.CRITICAL, 0.6),
            ("PPE Violation", EventType.PPE_VIOLATION, Severity.WARNING, 0.5),
        ]:
            AlertRule.objects.get_or_create(
                organization=org,
                name=name,
                defaults={"event_type": etype, "min_severity": sev, "min_confidence": conf},
            )

        rule, created = AnalyticsRule.objects.get_or_create(
            organization=org,
            name="AfterHoursIntrusion",
            defaults={
                "description": "Person enters restricted zone outside working hours",
                "event_types": ["intrusion"],
                "zone": zone,
                "site": site,
                "time_start": time(20, 0),
                "time_end": time(6, 0),
                "risk_level": RiskLevel.CRITICAL,
                "window_seconds": 60,
            },
        )
        if created:
            RuleAction.objects.create(rule=rule, action_type="alert")
            RuleAction.objects.create(rule=rule, action_type="snapshot")
            RuleAction.objects.create(rule=rule, action_type="recording")

        AnalyticsRule.objects.get_or_create(
            organization=org,
            name="UnknownLoiterRestricted",
            defaults={
                "description": "Unknown + loitering + restricted zone → high risk",
                "event_types": ["unknown_person", "loitering", "intrusion"],
                "require_all": True,
                "site": site,
                "risk_level": RiskLevel.CRITICAL,
                "window_seconds": 180,
            },
        )

        employee, _ = Person.objects.get_or_create(
            organization=org,
            external_id="EMP-001",
            defaults={
                "full_name": "Alice Employee",
                "person_type": PersonType.EMPLOYEE,
                "email": "alice@acme-security.com",
                "department": "Operations",
                "shift_start": time(9, 0),
                "shift_end": time(18, 0),
            },
        )
        # No face vector is seeded: a fake one can never match a real face.
        # Enroll Alice from the People page with real photos instead.
        BiometricConsent.objects.get_or_create(
            organization=org,
            person=employee,
            consent_type="enrollment",
            defaults={"granted_at": timezone.now()},
        )

        visitor_person, _ = Person.objects.get_or_create(
            organization=org,
            external_id="VIS-001",
            defaults={
                "full_name": "Bob Visitor",
                "person_type": PersonType.VISITOR,
                "email": "bob@example.com",
            },
        )
        now = timezone.now()
        VisitorRegistration.objects.get_or_create(
            organization=org,
            site=site,
            full_name="Bob Visitor",
            defaults={
                "person": visitor_person,
                "host_name": "Alice Employee",
                "host_email": "alice@acme-security.com",
                "purpose": "Vendor meeting",
                "valid_from": now - timedelta(hours=1),
                "valid_until": now + timedelta(days=1),
                "status": VisitorStatus.APPROVED,
            },
        )

        self.stdout.write(self.style.SUCCESS(f"Seeded org={org.name}, site={site.name}, camera={camera.name}"))
        self.stdout.write(self.style.SUCCESS("M3: Alice Employee enrolled; Bob Visitor approved"))
        self.stdout.write(self.style.SUCCESS("M4: zones, crowd, AfterHoursIntrusion rule"))
        self.stdout.write(self.style.SUCCESS("M5: safety profile, detector rules, emergency manager"))
        self.stdout.write(self.style.SUCCESS("Login: admin / admin123  |  operator / operator123"))
