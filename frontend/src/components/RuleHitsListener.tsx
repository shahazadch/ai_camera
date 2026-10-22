import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";

type Toast = { id: number; text: string };

const POLL_MS = 2000;
const TOAST_MS = 6000;

/**
 * Shows a pop-up when a camera rule with "Dashboard pop-up" fires. Sounds come
 * from scripts/alert_player.py on the host so they play with no page open; the
 * pop-up stays silent so nothing plays twice.
 */
export default function RuleHitsListener() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const after = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;

    const poll = async () => {
      try {
        const { latest, hits } = await api.getRuleHits(after.current);
        if (cancelled) return;
        // First poll, or backend restarted (ids start over): begin from now.
        if (after.current === null || latest < after.current) {
          after.current = latest;
          return;
        }
        const fresh: Toast[] = [];
        for (const hit of hits) {
          after.current = Math.max(after.current, hit.id);
          if (hit.popup) fresh.push({ id: hit.id, text: hit.message });
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
