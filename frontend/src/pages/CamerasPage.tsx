import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { api, Camera, Site, ZoneInfo } from "../api/client";
import Icon from "../components/Icon";
import { playStairsAlert } from "../stairsAlert";

const AI_MODULES = [
  ["multi_person_tracking", "Person tracking"],
  ["face_recognition", "Face recognition"],
  ["intrusion_detection", "Intrusion"],
  ["activity_recognition", "Activity"],
  ["unknown_person", "Unknown person"],
  ["mobile_use_tracking", "Mobile use tracking (saved members)"],
  ["fire_smoke", "Fire & smoke"],
  ["weapon_detection", "Weapon"],
];

type CameraForm = {
  name: string;
  site: string;
  zone: string;
  rtsp_url: string;
  enabled_modules: string[];
  is_active: boolean;
  auto_record: boolean;
};

const EMPTY_FORM: CameraForm = {
  name: "",
  site: "",
  zone: "",
  rtsp_url: "",
  enabled_modules: [
    "multi_person_tracking",
    "face_recognition",
    "unknown_person",
    "mobile_use_tracking",
  ],
  is_active: true,
  auto_record: true,
};

const LIVE_PREVIEW_STORAGE_KEY = "sentinelvision.live-camera-ids";
// ~5 fps preview per camera. Each frame is a backend request + JPEG decode.
const FRAME_INTERVAL_MS = 200;
// Name check. Boxes already come from the backend watcher on every frame.
const DETECT_INTERVAL_MS = 2000;

function getSavedPreviewIds(): Set<string> {
  try {
    const saved = localStorage.getItem(LIVE_PREVIEW_STORAGE_KEY);
    return saved ? new Set(JSON.parse(saved) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

function LiveCameraCard({
  camera,
  active,
}: {
  camera: Camera;
  active: boolean;
}) {
  const [frameUrl, setFrameUrl] = useState("");
  const [frameError, setFrameError] = useState("");
  const [loading, setLoading] = useState(false);
  const [objectLabels, setObjectLabels] = useState<string[]>([]);
  const [verify, setVerify] = useState({
    verified: 0,
    unknown: 0,
    threat: 0,
    dangers: 0,
    labels: [] as string[],
    issue: "",
    announce: "",
  });
  const [visionError, setVisionError] = useState("");
  const [stairsAlert, setStairsAlert] = useState(false);
  const stairsOn = useRef(false);
  const lastStairsSound = useRef(0);

  useEffect(() => {
    if (!active) {
      setFrameUrl((current) => {
        if (current) URL.revokeObjectURL(current);
        return "";
      });
      setFrameError("");
      setObjectLabels([]);
      setVerify({ verified: 0, unknown: 0, threat: 0, dangers: 0, labels: [], issue: "", announce: "" });
      setStairsAlert(false);
      stairsOn.current = false;
      return;
    }

    let cancelled = false;
    let currentUrl = "";
    let nextFrameTimer = 0;
    let stairsTimer = 0;
    let paintCtrl: AbortController | null = null;
    let inFlight = false;
    setLoading(true);

    // Paint loop only — never runs YOLO, so motion stays smooth.
    const loadFrame = async () => {
      if (cancelled) return;
      if (inFlight) {
        nextFrameTimer = window.setTimeout(loadFrame, 100);
        return;
      }
      inFlight = true;
      paintCtrl?.abort();
      paintCtrl = new AbortController();
      const abortTimer = window.setTimeout(() => paintCtrl?.abort(), 4000);
      let retryMs = FRAME_INTERVAL_MS;
      let stopPolling = false;
      try {
        const { blob, objects, stairsAlert: stairsNow, verify: nextVerify } = await api.getCameraFrame(
          camera.id,
          false,
          paintCtrl.signal
        );
        if (cancelled) return;
        if (stairsNow && !stairsOn.current) {
          const now = Date.now();
          if (now - lastStairsSound.current > 4000) {
            lastStairsSound.current = now;
            playStairsAlert();
            setStairsAlert(true);
          }
        }
        if (!stairsNow && stairsOn.current) {
          window.clearTimeout(stairsTimer);
          stairsTimer = window.setTimeout(() => setStairsAlert(false), 2200);
        }
        stairsOn.current = stairsNow;
        const nextUrl = URL.createObjectURL(blob);
        if (currentUrl) URL.revokeObjectURL(currentUrl);
        currentUrl = nextUrl;
        setFrameUrl(nextUrl);
        if (objects.length > 0) {
          setObjectLabels(objects);
        }
        // Sticky labels from paint path keep name / UNKNOWN visible between AI ticks.
        if (
          nextVerify.verified > 0 ||
          nextVerify.unknown > 0 ||
          nextVerify.threat > 0 ||
          nextVerify.announce ||
          nextVerify.labels.length > 0
        ) {
          setVerify(nextVerify);
        }
        setFrameError("");
      } catch (err) {
        if (cancelled) return;
        const status = (err as { status?: number } | null)?.status;
        if (status === 404) {
          setFrameError("Camera removed");
          stopPolling = true;
          return;
        }
        const aborted = err instanceof DOMException && err.name === "AbortError";
        setFrameError(aborted ? "Camera slow to respond…" : err instanceof Error ? err.message : "Preview unavailable");
        retryMs = 1500;
      } finally {
        window.clearTimeout(abortTimer);
        inFlight = false;
        if (!cancelled) {
          setLoading(false);
          if (!stopPolling) {
            // Background tabs don't need fresh frames; check back once a second.
            nextFrameTimer = window.setTimeout(loadFrame, document.hidden ? 1000 : retryMs);
          }
        }
      }
    };

    loadFrame();
    return () => {
      cancelled = true;
      paintCtrl?.abort();
      window.clearTimeout(nextFrameTimer);
      window.clearTimeout(stairsTimer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [active, camera.id]);

  useEffect(() => {
    if (!active) return;

    let cancelled = false;
    let detectCtrl: AbortController | null = null;
    let detectTimer = 0;

    // Heavy AI on a slow separate loop — never blocks the paint loop.
    const runDetect = async () => {
      if (cancelled) return;
      detectCtrl?.abort();
      detectCtrl = new AbortController();
      try {
        const { objects, verify: nextVerify } = await api.getCameraFrame(
          camera.id,
          true,
          detectCtrl.signal
        );
        if (cancelled) return;
        setObjectLabels(objects);
        setVerify(nextVerify);
      } catch {
        // Paint loop owns connection errors; detect failures stay quiet.
      } finally {
        if (!cancelled) {
          // Name check. The box is already painted by the fast watcher, so this
          // does not need to run on every frame.
          detectTimer = window.setTimeout(runDetect, document.hidden ? 5000 : DETECT_INTERVAL_MS);
        }
      }
    };

    detectTimer = window.setTimeout(runDetect, 200);
    return () => {
      cancelled = true;
      detectCtrl?.abort();
      window.clearTimeout(detectTimer);
    };
  }, [active, camera.id]);

  useEffect(() => {
    if (!active) return;

    let cancelled = false;
    let running = false;
    const runVisionScan = async () => {
      if (running || cancelled) return;
      running = true;
      try {
        const result = await api.analyzeCameraVision(camera.id);
        if (!cancelled) {
          // Skipped = no real activity — that is success, not an error.
          if (result && typeof result === "object" && "skipped" in result && (result as { skipped?: boolean }).skipped) {
            setVisionError("");
          } else {
            setVisionError("");
          }
        }
      } catch (err) {
        if (!cancelled) {
          const msg = err instanceof Error ? err.message : "Automatic AI analysis failed";
          // Quiet noise when stream is briefly busy / empty.
          if (!/skip|no security activity|no clear person|no meaningful/i.test(msg)) {
            setVisionError(msg);
          } else {
            setVisionError("");
          }
        }
      } finally {
        running = false;
      }
    };

    // Only scan for real activity — less frequent to avoid junk captures.
    const initialTimer = window.setTimeout(runVisionScan, 20000);
    const scanTimer = window.setInterval(runVisionScan, 60000);
    return () => {
      cancelled = true;
      window.clearTimeout(initialTimer);
      window.clearInterval(scanTimer);
    };
  }, [active, camera.id]);

  return (
    <article className="camera-live-card">
      <div className="camera-viewport">
        {frameUrl ? (
          <img src={frameUrl} alt={`${camera.name} live view`} />
        ) : (
          <div className="camera-placeholder">
            <Icon name="camera" />
            <span>{frameError || (active ? "Connecting to camera…" : "Preview stopped")}</span>
          </div>
        )}
        <div className="camera-overlay-top">
          {active && verify.announce && !verify.announce.toUpperCase().startsWith("THIEF") && (
            <span
              className={`verify-pill ${
                verify.announce.startsWith("UNKNOWN") ? "unknown" : "ok"
              }`}
              title={verify.announce}
            >
              {verify.announce}
            </span>
          )}
          {active && verify.verified > 0 && !verify.announce && (
            <span className="verify-pill ok">
              {camera.enabled_modules?.includes("mobile_use_tracking")
                ? /mobile|MOBILE|profile/i.test(verify.issue || "")
                  ? `${verify.verified} recognized · mobile → profile`
                  : `${verify.verified} recognized`
                : `${verify.verified} recognized`}
            </span>
          )}
          {active && verify.unknown > 0 && (
            <span className="verify-pill unknown">UNKNOWN {verify.unknown} — capturing</span>
          )}
          {active && verify.dangers > 0 && (
            <span className="verify-pill threat">ILLEGAL detect {verify.dangers}</span>
          )}
          {active &&
            verify.issue &&
            !verify.announce &&
            !/thief/i.test(verify.issue) && (
            <span className="verify-pill issue" title={verify.issue}>
              {verify.issue.startsWith("Face") ||
              verify.issue.includes("UNKNOWN") ||
              verify.issue.includes("Known") ||
              verify.issue.includes("Dangerous") ||
              verify.issue.includes("Illegal")
                ? verify.issue
                : `Issue: ${verify.issue}`}
            </span>
          )}
          {active && (verify.labels.length > 0 || objectLabels.length > 0) && (
            <span className="object-live-pill">
              {(verify.labels.length > 0 ? verify.labels : objectLabels)
                .map((label) => label.replace("_", " "))
                .join(" · ")}
            </span>
          )}
        </div>
        {active && loading && <span className="frame-loading">Updating…</span>}
        {active && stairsAlert && (
          <div className="stairs-alert-banner" role="status">
            STAIRS ALERT — someone coming up
          </div>
        )}
      </div>
      <div className="camera-card-footer">
        <div>
          <strong>{camera.name}</strong>
          <span>{camera.site_name} · {camera.zone_name || "No zone"}</span>
        </div>
      </div>
      {visionError && <div className="vision-inline-error">{visionError}</div>}
    </article>
  );
}

export default function CamerasPage() {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [sites, setSites] = useState<Site[]>([]);
  const [zones, setZones] = useState<ZoneInfo[]>([]);
  const [view, setView] = useState<"live" | "inventory">("live");
  const [previewIds, setPreviewIds] = useState<Set<string>>(getSavedPreviewIds);
  const [previewsReady, setPreviewsReady] = useState(false);
  const [editing, setEditing] = useState<Camera | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState<CameraForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [removingIds, setRemovingIds] = useState<Set<string>>(new Set());
  const [recordingIds, setRecordingIds] = useState<Set<string>>(new Set());
  const removedIdsRef = useRef<Set<string>>(new Set());
  const listEpoch = useRef(0);

  function applyCameras(list: Camera[]) {
    const visible = list.filter((camera) => !removedIdsRef.current.has(camera.id));
    setCameras(visible);
    setPreviewIds((current) => {
      const allowed = new Set(visible.map((camera) => camera.id));
      const next = new Set([...current].filter((id) => allowed.has(id)));
      visible
        .filter((camera) => camera.is_active && camera.rtsp_url)
        .forEach((camera) => next.add(camera.id));
      return next;
    });
  }

  const load = () => {
    const epoch = ++listEpoch.current;
    return Promise.all([api.getCameras(), api.getSites(), api.getZones()])
      .then(([cameraData, siteData, zoneData]) => {
        if (epoch !== listEpoch.current) return;
        setSites(siteData.results);
        setZones(zoneData.results);
        applyCameras(cameraData.results);
        setPreviewsReady(true);
      })
      .catch((e) => {
        if (epoch === listEpoch.current) setError(e.message);
      });
  };

  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      const epoch = ++listEpoch.current;
      api
        .getCameras()
        .then((data) => {
          if (epoch !== listEpoch.current) return;
          applyCameras(data.results);
        })
        .catch(() => undefined);
      api
        .getRecorderStatus()
        .then((status) => {
          setRecordingIds(
            new Set(status.workers.filter((w) => w.recording).map((w) => w.camera_id))
          );
        })
        .catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!previewsReady) return;
    localStorage.setItem(LIVE_PREVIEW_STORAGE_KEY, JSON.stringify([...previewIds]));
  }, [previewIds, previewsReady]);

  const availableZones = useMemo(
    () => zones.filter((zone) => !form.site || zone.site === form.site),
    [zones, form.site]
  );

  function openCreate() {
    setEditing(null);
    setForm({ ...EMPTY_FORM, site: sites[0]?.id || "" });
    setError("");
    setMessage("");
    setFormOpen(true);
  }

  function openEdit(camera: Camera) {
    setEditing(camera);
    setForm({
      name: camera.name,
      site: camera.site,
      zone: camera.zone || "",
      rtsp_url: camera.rtsp_url,
      enabled_modules: camera.enabled_modules || [],
      is_active: camera.is_active,
      auto_record: camera.auto_record !== false,
    });
    setError("");
    setMessage("");
    setFormOpen(true);
  }

  async function saveCamera(event: FormEvent) {
    event.preventDefault();
    if (!form.name.trim() || !form.site || !form.rtsp_url.trim()) {
      setError("Name, site and stream URL are required.");
      return;
    }
    setSaving(true);
    setError("");
    // Pause live RTSP pulls so Save is not stuck behind stream timeouts.
    const resumePreviews = new Set(previewIds);
    setPreviewIds(new Set());
    try {
      const payload = { ...form, zone: form.zone || null };
      if (editing) await api.updateCamera(editing.id, payload);
      else await api.createCamera(payload);
      setFormOpen(false);
      setMessage(
        editing
          ? "Camera updated."
          : form.auto_record
            ? "Camera connected. Auto recording started — clips appear under Recordings."
            : "Camera added. Use Test connection, then enable Auto record if needed."
      );
      await load();
      setPreviewIds(resumePreviews);
    } catch (err) {
      setPreviewIds(resumePreviews);
      setError(err instanceof Error ? err.message : "Unable to save camera.");
    } finally {
      setSaving(false);
    }
  }

  async function testCamera(camera: Camera) {
    setTestingId(camera.id);
    setError("");
    setMessage("");
    try {
      const result = await api.testCamera(camera.id);
      setMessage(`Connected to ${camera.name} · ${result.width}×${result.height}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connection test failed.");
    } finally {
      setTestingId("");
    }
  }

  async function removeCamera(camera: Camera) {
    if (removingIds.has(camera.id)) return;
    if (!window.confirm(`Remove ${camera.name}? This camera will stop processing.`)) return;

    removedIdsRef.current.add(camera.id);
    listEpoch.current += 1;
    setRemovingIds((current) => new Set(current).add(camera.id));
    setCameras((current) => current.filter((item) => item.id !== camera.id));
    setPreviewIds((current) => {
      const next = new Set(current);
      next.delete(camera.id);
      return next;
    });
    setError("");
    setMessage("");

    try {
      await api.deleteCamera(camera.id);
      setMessage(`${camera.name} removed.`);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Unable to remove camera.";
      const alreadyGone = /not found|no camera matches|404/i.test(detail);
      if (alreadyGone) {
        setMessage(`${camera.name} removed.`);
      } else {
        removedIdsRef.current.delete(camera.id);
        setError(detail);
        await load();
      }
    } finally {
      setRemovingIds((current) => {
        const next = new Set(current);
        next.delete(camera.id);
        return next;
      });
    }
  }

  return (
    <div>
      <header className="page-header camera-page-header">
        <div>
          <h1>Control Center</h1>
        </div>
        <button className="btn" onClick={openCreate}>Connect</button>
      </header>

      <div className="camera-toolbar">
        <div className="view-switch">
          <button className={view === "live" ? "active" : ""} onClick={() => setView("live")}>
            <Icon name="camera" /> Live view
          </button>
          <button className={view === "inventory" ? "active" : ""} onClick={() => setView("inventory")}>
            <Icon name="event" /> Inventory
          </button>
        </div>
        <div className="camera-summary">
          <span>{cameras.filter((c) => c.status === "online").length} online</span>
          <span>{recordingIds.size} recording</span>
          <span>{cameras.length} total</span>
        </div>
      </div>

      {error && <div className="error-banner camera-message">{error}</div>}
      {message && <div className="success-banner camera-message">{message}</div>}

      {view === "live" ? (
        <div className="camera-live-grid">
          {cameras.map((camera) => (
            <LiveCameraCard
              key={camera.id}
              camera={camera}
              active={!formOpen && previewIds.has(camera.id)}
            />
          ))}
          {cameras.length === 0 && (
            <div className="camera-onboarding">
              <Icon name="camera" />
              <h2>No cameras connected</h2>
              <p>
                Connect a home IP camera (Hikvision / TP-Link / Xiaomi RTSP) or phone IP Webcam.
                Auto recording starts as soon as the stream is active.
              </p>
              <button className="btn" onClick={openCreate}>Connect your first home camera</button>
            </div>
          )}
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Site / Zone</th>
                <th>Status</th>
                <th>Stream</th>
                <th>Auto REC</th>
                <th>AI Modules</th>
                <th>Last Seen</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {cameras.map((camera) => (
                <tr key={camera.id}>
                  <td><strong>{camera.name}</strong></td>
                  <td>{camera.site_name}<div className="muted">{camera.zone_name || "No zone"}</div></td>
                  <td><span className={`badge ${camera.status}`}>{camera.status}</span></td>
                  <td><span className="secret-url">{camera.rtsp_url ? "Configured" : "Missing"}</span></td>
                  <td>
                    <span className={`badge ${camera.auto_record ? "online" : "offline"}`}>
                      {recordingIds.has(camera.id) ? "Recording" : camera.auto_record ? "On" : "Off"}
                    </span>
                  </td>
                  <td>{camera.enabled_modules?.length || 0} enabled</td>
                  <td>{camera.last_seen_at ? new Date(camera.last_seen_at).toLocaleString() : "—"}</td>
                  <td>
                    <div className="row-actions">
                      <button onClick={() => testCamera(camera)} disabled={testingId === camera.id}>
                        {testingId === camera.id ? "Testing…" : "Test"}
                      </button>
                      <button onClick={() => openEdit(camera)}>Edit</button>
                      <button
                        className="danger-action"
                        disabled={removingIds.has(camera.id)}
                        onClick={() => removeCamera(camera)}
                      >
                        {removingIds.has(camera.id) ? "Removing…" : "Remove"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {formOpen && (
        <div className="modal-backdrop" onMouseDown={() => !saving && setFormOpen(false)}>
          <form className="camera-modal" onSubmit={saveCamera} onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h2>{editing ? "Edit camera" : "Connect home camera"}</h2>
                <p className="muted">Realtime live view + automatic continuous video recording.</p>
              </div>
              <button type="button" className="modal-close" onClick={() => setFormOpen(false)}>×</button>
            </div>

            {!editing && (
              <div className="home-camera-guide">
                <strong>How to connect a home camera</strong>
                <ol>
                  <li>Same Wi‑Fi: phone/laptop and camera must be on the same network.</li>
                  <li>
                    Phone: install <em>IP Webcam</em>, start server, paste{" "}
                    <code>http://192.168.x.x:8080</code>
                  </li>
                  <li>
                    CCTV / NVR: use RTSP like{" "}
                    <code>rtsp://user:pass@192.168.x.x:554/Streaming/Channels/101</code>
                  </li>
                  <li>Leave <em>Auto record video</em> ON — clips save every ~60 seconds.</li>
                </ol>
              </div>
            )}

            <div className="form-grid">
              <label>
                Camera name
                <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Living room / Gate / Phone cam" />
              </label>
              <label>
                Site
                <select value={form.site} onChange={(e) => setForm({ ...form, site: e.target.value, zone: "" })}>
                  <option value="">Select site</option>
                  {sites.map((site) => <option key={site.id} value={site.id}>{site.name}</option>)}
                </select>
              </label>
              <label>
                Zone
                <select value={form.zone} onChange={(e) => setForm({ ...form, zone: e.target.value })}>
                  <option value="">No zone</option>
                  {availableZones.map((zone) => <option key={zone.id} value={zone.id}>{zone.name}</option>)}
                </select>
              </label>
              <label className="full-field">
                Stream URL (home camera)
                <input
                  value={form.rtsp_url}
                  onChange={(e) => setForm({ ...form, rtsp_url: e.target.value })}
                  placeholder="http://192.168.100.53:8080 or rtsp://user:pass@ip:554/stream1"
                />
                <small>
                  Phone IP Webcam: <code>http://IP:8080</code>. Password on? use{" "}
                  <code>http://user:pass@IP:8080/video</code>. Hikvision/Dahua/TP-Link: RTSP from camera settings.
                </small>
              </label>
            </div>

            <fieldset className="module-picker">
              <legend>AI modules</legend>
              {AI_MODULES.map(([value, label]) => (
                <label key={value} className="check-option">
                  <input
                    type="checkbox"
                    checked={form.enabled_modules.includes(value)}
                    onChange={(e) => setForm({
                      ...form,
                      enabled_modules: e.target.checked
                        ? [...form.enabled_modules, value]
                        : form.enabled_modules.filter((item) => item !== value),
                    })}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </fieldset>

            <label className="check-option active-camera-option">
              <input type="checkbox" checked={form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} />
              <span>Camera active (live AI + monitoring)</span>
            </label>

            <label className="check-option active-camera-option">
              <input
                type="checkbox"
                checked={form.auto_record}
                onChange={(e) => setForm({ ...form, auto_record: e.target.checked })}
              />
              <span>Auto record video continuously (rolling clips every ~60s)</span>
            </label>

            <div className="modal-actions">
              <button type="button" className="btn secondary" onClick={() => setFormOpen(false)}>Cancel</button>
              <button className="btn" type="submit" disabled={saving}>
                {saving ? "Saving…" : editing ? "Save changes" : "Connect & start recording"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
