from django.urls import path

from .views import (
    LiveView,
    MetricsView,
    NotificationDeliveryLogListView,
    NotificationJobListView,
    PasswordPolicyView,
    ReadyView,
    RecordingBulkDeleteView,
    RecordingCameraSummaryView,
    RecordingJobListView,
    RecordingSettingsView,
    SessionPolicyView,
    SIEMJobListView,
    SnapshotDetailView,
    SnapshotListView,
    SnapshotUploadView,
    SSOProviderListCreateView,
)

urlpatterns = [
    path("notifications/", NotificationJobListView.as_view(), name="notification-list"),
    path("notifications/logs/", NotificationDeliveryLogListView.as_view(), name="notification-logs"),
    path("recordings/", RecordingJobListView.as_view(), name="recording-list"),
    path("recordings/settings/", RecordingSettingsView.as_view(), name="recording-settings"),
    path("recordings/cameras/", RecordingCameraSummaryView.as_view(), name="recording-cameras"),
    path("recordings/delete/", RecordingBulkDeleteView.as_view(), name="recording-bulk-delete"),
    path("snapshots/", SnapshotListView.as_view(), name="snapshot-list"),
    path("snapshots/upload/", SnapshotUploadView.as_view(), name="snapshot-upload"),
    path("snapshots/<uuid:pk>/", SnapshotDetailView.as_view(), name="snapshot-detail"),
    path("sso/providers/", SSOProviderListCreateView.as_view(), name="sso-provider-list"),
    path("identity/password-policy/", PasswordPolicyView.as_view(), name="password-policy"),
    path("identity/session-policy/", SessionPolicyView.as_view(), name="session-policy"),
    path("siem/jobs/", SIEMJobListView.as_view(), name="siem-job-list"),
    path("metrics/", MetricsView.as_view(), name="prometheus-metrics"),
    path("ready/", ReadyView.as_view(), name="ready"),
    path("live/", LiveView.as_view(), name="live"),
]
