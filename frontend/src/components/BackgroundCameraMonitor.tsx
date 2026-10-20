import { useEffect, useState } from "react";
import { api } from "../api/client";
import Icon from "./Icon";

type MonitorStatus = {
  active: boolean;
  lastOk: number;
  lastChecked: number;
  lastAt: string;
};

/**
 * Shows camera online status in the header. The backend already monitors every
 * active camera on its own (CAMERA_MONITOR_INTERVAL), so this only refreshes the
 * pill and never asks for vision analysis.
 */
const STATUS_INTERVAL_MS = 30000;

export default function BackgroundCameraMonitor() {
  const [status, setStatus] = useState<MonitorStatus>({
    active: false,
    lastOk: 0,
    lastChecked: 0,
    lastAt: "",
  });

  useEffect(() => {
    let cancelled = false;
    let running = false;
    let timer = 0;

    const tick = async () => {
      if (cancelled || running) return;
      if (document.hidden) {
        timer = window.setTimeout(tick, STATUS_INTERVAL_MS);
        return;
      }
      running = true;
      try {
        const result = await api.monitorCamerasTick(false);
        if (!cancelled) {
          setStatus({
            active: true,
            lastOk: result.ok,
            lastChecked: result.checked,
            lastAt: new Date().toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            }),
          });
        }
      } catch {
        if (!cancelled) {
          setStatus((prev) => ({ ...prev, active: false }));
        }
      } finally {
        running = false;
        if (!cancelled) {
          timer = window.setTimeout(tick, STATUS_INTERVAL_MS);
        }
      }
    };

    timer = window.setTimeout(tick, 1500);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  if (!status.active && !status.lastAt) return null;

  const checked = status.lastChecked || 0;
  const online = status.lastOk;
  const title = status.active
    ? `Cameras online ${online}/${checked || "…"}${status.lastAt ? ` · last check ${status.lastAt}` : ""}`
    : "Camera monitor reconnecting…";

  return (
    <span className={`monitor-pill ${status.active ? "on" : "off"}`} title={title}>
      <Icon name="camera" />
      {status.active ? (
        <>
          <span className="monitor-label">Cameras</span>
          <strong>
            {online}/{checked || "…"}
          </strong>
        </>
      ) : (
        <span className="monitor-label">Reconnecting</span>
      )}
    </span>
  );
}
