from django.urls import path

from .views import (
    AlertSoundDetailView,
    AlertSoundFileView,
    AlertSoundListCreateView,
    RuleDetailView,
    RuleHitsView,
    RuleListCreateView,
    RuleTestView,
)

urlpatterns = [
    path("camera-rules/", RuleListCreateView.as_view(), name="rule-list"),
    path("camera-rules/hits/", RuleHitsView.as_view(), name="rule-hits"),
    path("camera-rules/sounds/", AlertSoundListCreateView.as_view(), name="alert-sound-list"),
    path("camera-rules/sounds/<uuid:pk>/", AlertSoundDetailView.as_view(), name="alert-sound-detail"),
    path("camera-rules/sounds/<uuid:pk>/file/", AlertSoundFileView.as_view(), name="alert-sound-file"),
    path("camera-rules/<uuid:pk>/", RuleDetailView.as_view(), name="rule-detail"),
    path("camera-rules/<uuid:pk>/test/", RuleTestView.as_view(), name="rule-test"),
]
