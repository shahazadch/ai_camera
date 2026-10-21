import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type Toast = { id: number; text: string };

const POLL_MS = 2000;
const TOAST_MS = 6000;

/**
 * Shows "Person on {camera}" when a camera with the person alert on sees someone
 * inside its time window. The sound itself comes from scripts/alert_player.py on
 * the host so it plays with no page open; this toast is silent to avoid doubling.
 */
export default function PersonAlertListener() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const lastSeq = useRef<Map<string, number>>(new Map());
  const nextId = useRef(1);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;

    const poll = async () => {
      try {
        const { cameras } = await api.getPersonAlerts();
        if (cancelled) return;
        const fresh: Toast[] = [];
        for (const cam of cameras) {
          const previous = lastSeq.current.get(cam.camera_id);
          lastSeq.current.set(cam.camera_id, cam.seq);
          // First poll (or backend restart) only sets the baseline.
          if (previous === undefined || cam.seq <= previous || !cam.active) continue;
          fresh.push({ id: nextId.current++, text: `Person on ${cam.name}` });
        }
        if (fresh.length) {
          setToasts((current) => [...current, ...fresh].slice(-4));
          for (const toast of fresh) {
            window.setTimeout(
              () => setToasts((current) => current.filter((t) => t.id !== toast.id)),
              TOAST_MS
            );
          }
        }
      } catch {
        // Not logged in yet or backend restarting; try again next tick.
      } finally {
        if (!cancelled) timer = window.setTimeout(poll, POLL_MS);
      }
    };

    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  if (!toasts.length) return null;
  return (
    <div className="person-alert-toasts" role="status" aria-live="polite">
      {toasts.map((toast) => (
        <div key={toast.id} className="person-alert-toast">
          🔔 {toast.text}
        </div>
      ))}
    </div>
  );
}
