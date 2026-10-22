from django.urls import path

from .views import (
    CameraConnectionTestView,
    CameraDetailView,
    CameraFrameView,
    CameraListCreateView,
    CameraMonitorTickView,
    CameraRecorderStatusView,
    CameraVisionAnalyzeView,
    EdgeCameraListView,
    EdgeDeviceListCreateView,
    EdgeHeartbeatView,
    VisionCaptureListView,
    VisionLLMStatusView,
)

urlpatterns = [
    path("cameras/", CameraListCreateView.as_view(), name="camera-list"),
    path("cameras/monitor/tick/", CameraMonitorTickView.as_view(), name="camera-monitor-tick"),
    path("cameras/recorder/status/", CameraRecorderStatusView.as_view(), name="camera-recorder-status"),
    path("cameras/<uuid:pk>/", CameraDetailView.as_view(), name="camera-detail"),
    path("cameras/<uuid:pk>/test/", CameraConnectionTestView.as_view(), name="camera-test"),
    path("cameras/<uuid:pk>/frame/", CameraFrameView.as_view(), name="camera-frame"),
    path("cameras/<uuid:pk>/vision/", CameraVisionAnalyzeView.as_view(), name="camera-vision"),
    path("vision/captures/", VisionCaptureListView.as_view(), name="vision-capture-list"),
    path("vision/status/", VisionLLMStatusView.as_view(), name="vision-status"),
    path("edge-devices/", EdgeDeviceListCreateView.as_view(), name="edge-device-list"),
    path("edge/heartbeat/", EdgeHeartbeatView.as_view(), name="edge-heartbeat"),
    path("edge/cameras/", EdgeCameraListView.as_view(), name="edge-camera-list"),
]
