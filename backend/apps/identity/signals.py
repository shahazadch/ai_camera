"""Drop the cached face gallery whenever enrolled identities change."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import FaceEmbedding, FaceImage, Person
from .services import invalidate_gallery_cache


@receiver([post_save, post_delete], sender=Person)
@receiver([post_save, post_delete], sender=FaceEmbedding)
@receiver([post_save, post_delete], sender=FaceImage)
def _identity_changed(**kwargs) -> None:
    invalidate_gallery_cache()
