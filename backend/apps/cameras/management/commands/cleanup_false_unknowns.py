"""
Remove "unknown person" events and sightings that the old, looser rules created
from weak person guesses (e.g. cows on a farm camera).

    python manage.py cleanup_false_unknowns --camera "Home farm 2" --dry-run
    python manage.py cleanup_false_unknowns --camera "Home farm 2"

Only entries below the person-confidence bar now required for UNKNOWN are
removed, so confident detections (possibly real strangers) are kept for review.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.cameras.models import Camera
from apps.cameras.views import UNKNOWN_MIN_PERSON_CONFIDENCE
from apps.events.models import DetectionEvent, EventType
from apps.identity.models import UnknownSighting


class Command(BaseCommand):
    help = "Delete weak 'unknown person' events/sightings for one camera."

    def add_arguments(self, parser):
        parser.add_argument("--camera", required=True, help="Camera name (exact)")
        parser.add_argument(
            "--below",
            type=float,
            default=UNKNOWN_MIN_PERSON_CONFIDENCE,
            help="Person confidence bar (default: the current UNKNOWN minimum)",
        )
        parser.add_argument("--dry-run", action="store_true", help="Only show what would be deleted")

    def handle(self, *args, **options):
        cameras = list(Camera.objects.filter(name=options["camera"]))
        if not cameras:
            raise CommandError(f"No camera named {options['camera']!r}.")
        below = options["below"]
        events = DetectionEvent.objects.filter(
            camera__in=cameras, event_type=EventType.UNKNOWN_PERSON, confidence__lt=below
        )
        sightings = UnknownSighting.objects.filter(camera__in=cameras, confidence__lt=below)
        kept_events = DetectionEvent.objects.filter(
            camera__in=cameras, event_type=EventType.UNKNOWN_PERSON, confidence__gte=below
        ).count()

        self.stdout.write(
            f"{events.count()} unknown events and {sightings.count()} sightings below {below:.0%} person "
            f"confidence; {kept_events} confident unknown events kept for review."
        )
        if options["dry_run"]:
            self.stdout.write("Dry run: nothing deleted.")
            return

        media = Path(settings.MEDIA_ROOT).resolve()
        paths = set(sightings.exclude(snapshot_path="").values_list("snapshot_path", flat=True))
        for payload in events.values_list("payload", flat=True):
            if (payload or {}).get("snapshot_path"):
                paths.add(payload["snapshot_path"])
        removed_files = 0
        for relative in paths:
            path = (media / relative).resolve()
            if media in path.parents and path.is_file():
                path.unlink()
                removed_files += 1

        sighting_count = sightings.delete()[0]
        event_count = events.delete()[0]
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {event_count} event rows, {sighting_count} sighting rows and {removed_files} snapshot files."
            )
        )
