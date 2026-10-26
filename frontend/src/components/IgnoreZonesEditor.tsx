import { MouseEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";

export type Zone = number[][]; // [[x, y], ...] in 0..1 image coordinates

/**
 * Draw "ignore areas" (detection mask) on a live snapshot. A person whose feet
 * (bottom-centre of the box) stand inside an area is ignored — the standard
 * NVR fix for animals, statues or posters mistaken for people.
 */
export default function IgnoreZonesEditor({
  cameraId,
  zones,
  onChange,
}: {
  cameraId: string;
  zones: Zone[];
  onChange: (zones: Zone[]) => void;
}) {
  const [snapshot, setSnapshot] = useState("");
  const [error, setError] = useState("");
  const [draft, setDraft] = useState<Zone>([]);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let url = "";
    let cancelled = false;
    api
      .getCameraFrame(cameraId, false)
      .then(({ blob }) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setSnapshot(url);
      })
      .catch(() => !cancelled && setError("Couldn't load a snapshot (is the camera online?)."));
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [cameraId]);

  function addPoint(event: MouseEvent<HTMLDivElement>) {
    const rect = boxRef.current?.getBoundingClientRect();
    if (!rect) return;
    const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
    setDraft((points) => [...points, [Number(x.toFixed(4)), Number(y.toFixed(4))]]);
  }

  function finish() {
    if (draft.length < 3) return;
    onChange([...zones, draft]);
    setDraft([]);
  }

  const toPoints = (zone: Zone) => zone.map(([x, y]) => `${x * 100},${y * 100}`).join(" ");

  return (
    <div className="ignore-zones">
      <p className="muted">
        Click around an area where people should be ignored (e.g. the animal pen), then <strong>Finish area</strong>.
        People walking elsewhere are still detected.
      </p>
      {error && <div className="error-banner">{error}</div>}
      {snapshot && (
        <div className="ignore-zones-canvas" ref={boxRef} onClick={addPoint}>
          <img src={snapshot} alt="Camera snapshot" draggable={false} />
          <svg viewBox="0 0 100 100" preserveAspectRatio="none">
            {zones.map((zone, index) => (
              <polygon key={index} points={toPoints(zone)} className="zone-saved" />
            ))}
            {draft.length > 1 && <polyline points={toPoints(draft)} className="zone-draft" />}
            {draft.map(([x, y], index) => (
              <circle key={index} cx={x * 100} cy={y * 100} r={0.9} className="zone-point" />
            ))}
          </svg>
        </div>
      )}
      <div className="row-actions">
        <button type="button" disabled={draft.length < 3} onClick={finish}>
          Finish area
        </button>
        <button type="button" disabled={!draft.length} onClick={() => setDraft((points) => points.slice(0, -1))}>
          Undo point
        </button>
        <button type="button" disabled={!zones.length && !draft.length} onClick={() => { onChange([]); setDraft([]); }}>
          Clear all areas
        </button>
        <span className="muted">
          {zones.length} area{zones.length === 1 ? "" : "s"}
          {draft.length ? ` · drawing (${draft.length} points)` : ""}
        </span>
      </div>
    </div>
  );
}
