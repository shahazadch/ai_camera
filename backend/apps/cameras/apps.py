import logging
import os
import sys
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def _start_monitor_loop(interval: float) -> None:
    """Poll active cameras in this process so the loaded models are shared."""

    def _loop() -> None:
        from django.db import close_old_connections, reset_queries

        from apps.cameras.monitor import monitor_active_cameras

        time.sleep(5.0)
        while True:
            # Outside the request cycle nothing clears DEBUG's query log or
            # recycles the DB connection, so do it per pass.
            reset_queries()
            close_old_connections()
            try:
                monitor_active_cameras(run_vision=True)
            except Exception:
                logger.debug("Camera monitor loop failed", exc_info=True)
            time.sleep(interval)

    threading.Thread(target=_loop, name="camera-monitor", daemon=True).start()


class CamerasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cameras"
    label = "cameras"

    def ready(self):
        # Django autoreload parent process
        if os.environ.get("RUN_MAIN") == "false":
            return
        # Only boot workers with the web server, not migrate/shell/test.
        argv0 = os.path.basename(sys.argv[0]) if sys.argv else ""
        command = sys.argv[1] if len(sys.argv) > 1 else ""
        web_servers = {"runserver", "uvicorn", "gunicorn"}
        if command not in web_servers and argv0 not in web_servers:
            return
        from django.conf import settings

        try:
            from apps.cameras.continuous_recorder import recorder_manager, start_retention_loop

            # Retention runs even with recording off so old clips still expire.
            start_retention_loop()
            if settings.CONTINUOUS_RECORDING:
                recorder_manager.ensure_started()
        except Exception:
            pass
        try:
            from apps.cameras.detection import boot_person_watcher

            boot_person_watcher()
        except Exception:
            pass
        if settings.CAMERA_MONITOR_INTERVAL > 0:
            _start_monitor_loop(settings.CAMERA_MONITOR_INTERVAL)
