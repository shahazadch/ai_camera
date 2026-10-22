import { FormEvent, useEffect, useState } from "react";
import { api, RecordingCameraSummary, RecordingJob, RecordingSettings } from "../api/client";
import Icon from "../components/Icon";
import LiveRewindPlayer from "../components/LiveRewindPlayer";

function clipLength(job: RecordingJob): string {
  if (job.started_at && job.ended_at) {
    const seconds = Math.max(
      0,
      Math.round((new Date(job.ended_at).getTime() - new Date(job.started_at).getTime()) / 1000)
    );
    return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
  }
  return job.status === "recording" ? "live…" : "—";
}

function sizeLabel(bytes: unknown): string {
  const value = Number(bytes);
  if (!value) return "—";
  return value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`;
}

export default function RecordingsPage() {
  const [cameras, setCameras] = useState<RecordingCameraSummary[]>([]);
  const [selected, setSelected] = useState<RecordingCameraSummary | null>(null);
  const [jobs, setJobs] = useState<RecordingJob[]>([]);
  const [pages, setPages] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [playing, setPlaying] = useState<RecordingJob | null>(null);
  const [error, setError] = useState("");
  const [recSettings, setRecSettings] = useState<RecordingSettings | null>(null);
  const [days, setDays] = useState(3);
  const [eventDays, setEventDays] = useState(30);
  const [rewindMinutes, setRewindMinutes] = useState(120);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const [quality, setQuality] = useState<"standard" | "high">("standard");
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState("");

  useEffect(() => {
    api
      .getRecordingSettings()
      .then((data) => {
        setRecSettings(data);
        setDays(data.retention_days);
        setEventDays(data.event_retention_days);
        setRewindMinutes(data.live_rewind_minutes);
        setQuality(data.quality);
      })
      .catch(() => undefined);
  }, []);

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    setSavingSettings(true);
    setSettingsMessage("");
    try {
      const data = await api.updateRecordingSettings({
        retention_days: days,
        event_retention_days: eventDays,
        live_rewind_minutes: rewindMinutes,
        quality,
      });
      setRecSettings(data);
      setSettingsMessage(
        `Saved. Continuous video is deleted after ${data.retention_days} days, event clips after ${data.event_retention_days} days.`
      );
    } catch (err) {
      setSettingsMessage(err instanceof Error ? err.message : "Unable to save settings.");
    } finally {
      setSavingSettings(false);
    }
  }

  // Camera cards; also refreshed while a camera is open so its live player
  // appears/disappears as recording starts or stops.
  const selectedId = selected?.camera_id ?? "";
  const liveUrl = cameras.find((c) => c.camera_id === selectedId)?.live_url ?? null;
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .getRecordingCameras()
        .then((data) => !cancelled && setCameras(data))
        .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "Unable to load cameras."));
    load();
    const timer = window.setInterval(load, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // Step 2: the selected camera's clips, newest first.
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    const load = async () => {
      try {
        // Re-read every page shown so "Load more" results stay fresh too.
        const results: RecordingJob[] = [];
        let more = false;
        let count = 0;
        for (let page = 1; page <= pages; page++) {
          const data = await api.getRecordings(selected.camera_id, page);
          results.push(...data.results);
          count = data.count;
          more = Boolean(data.next);
          if (!data.next) break;
        }
        if (cancelled) return;
        setJobs(results);
        setTotal(count);
        setHasMore(more);
        // Drop ticks for clips that no longer exist (deleted or expired).
        setChecked((current) => {
          const ids = new Set(results.map((job) => job.id));
          const next = new Set([...current].filter((id) => ids.has(id)));
          return next.size === current.size ? current : next;
        });
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Unable to load recordings.");
      }
    };
    load();
    const timer = window.setInterval(load, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [selected, pages, refreshKey]);

  function openCamera(camera: RecordingCameraSummary) {
    setSelected(camera);
    setJobs([]);
    setTotal(0);
    setPages(1);
    setPlaying(null);
    setChecked(new Set());
  }

  function back() {
    setSelected(null);
    setPlaying(null);
    setChecked(new Set());
  }

  const allChecked = jobs.length > 0 && jobs.every((job) => checked.has(job.id));

  function toggleAll() {
    setChecked(allChecked ? new Set() : new Set(jobs.map((job) => job.id)));
  }

  function toggleOne(id: string) {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function deleteChecked() {
    const ids = [...checked];
    if (!ids.length) return;
    if (!window.confirm(`Delete ${ids.length} recording${ids.length > 1 ? "s" : ""}? The video files are removed permanently.`)) {
      return;
    }
    setDeleting(true);
    try {
      const { deleted } = await api.deleteRecordings(ids);
      if (playing && ids.includes(playing.id)) setPlaying(null);
      setChecked(new Set());
      setSettingsMessage(`Deleted ${deleted} recording${deleted === 1 ? "" : "s"}.`);
      setRefreshKey((n) => n + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete recordings.");
    } finally {
      setDeleting(false);
    }
  }

  if (error) return <div className="error-banner">{error}</div>;

  const recordingNow = cameras.filter((c) => c.recording_now).length;

  return (
    <div>
      <header className="page-header">
        <div>
          <h1>
            {selected ? (
              <>
                <button type="button" className="back-link" onClick={back}>
                  ← Cameras
                </button>{" "}
                {selected.camera_name}
              </>
            ) : (
              "Recordings"
            )}
          </h1>
          <p className="muted">
            {selected
              ? `${total} clips · up to 30 minutes each · newest first`
              : "Pick a camera to see its recordings. Each file holds up to 30 minutes."}
          </p>
        </div>
        {!selected && (
          <div className="camera-summary">
            <span>{recordingNow} cameras recording now</span>
          </div>
        )}
      </header>

      {!selected && (
        <>
          <form className="recording-settings" onSubmit={saveSettings}>
            <label>
              Keep continuous video for
              <span className="inline-input">
                <input type="number" min={1} max={365} value={days} onChange={(e) => setDays(Number(e.target.value))} />
                days
              </span>
            </label>
            <label>
              Keep event clips for
              <span className="inline-input">
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={eventDays}
                  onChange={(e) => setEventDays(Number(e.target.value))}
                />
                days
              </span>
            </label>
            <label>
              Live rewind
              <select value={rewindMinutes} onChange={(e) => setRewindMinutes(Number(e.target.value))}>
                <option value={60}>Last 1 hour</option>
                <option value={120}>Last 2 hours</option>
                <option value={360}>Last 6 hours</option>
              </select>
            </label>
            <label>
              Quality
              <select value={quality} onChange={(e) => setQuality(e.target.value as "standard" | "high")}>
                <option value="standard">Standard (sub-stream, ~5 GB/day per camera)</option>
                <option value="high">High (full resolution, ~20 GB/day per camera)</option>
              </select>
            </label>
            <button className="btn" type="submit" disabled={savingSettings}>
              {savingSettings ? "Saving…" : "Save"}
            </button>
            <div className="muted recording-usage">
              {recSettings
                ? `${recSettings.enabled ? "Recording is on" : "Recording is turned off (CONTINUOUS_RECORDING)"} · ` +
                  `${(recSettings.disk_usage_bytes / 1024 ** 3).toFixed(2)} GB used. ` +
                  "Turn recording on/off per camera with Auto record in Cameras → Edit."
                : ""}
            </div>
            {settingsMessage && <div className="muted">{settingsMessage}</div>}
          </form>

          <div className="recording-cameras">
            {cameras.map((camera) => (
              <button
                type="button"
                key={camera.camera_id}
                className="recording-camera-card"
                onClick={() => openCamera(camera)}
              >
                <span className="recording-camera-icon">
                  <Icon name="camera" />
                </span>
                <strong>{camera.camera_name}</strong>
                <span
                  className={`badge ${camera.recording_now ? "online" : camera.auto_record ? "warning" : "offline"}`}
                >
                  {camera.recording_now ? "● Recording" : camera.auto_record ? "Not connected" : "Auto record off"}
                </span>
                <span className="muted">
                  {camera.clip_count} clips
                  {camera.last_clip_at ? ` · last ${new Date(camera.last_clip_at).toLocaleString()}` : ""}
                </span>
                {camera.auto_record && !camera.recording_now && camera.last_error && (
                  <small className="muted recording-camera-error" title={camera.last_error}>
                    {camera.last_error.slice(-90)}
                  </small>
                )}
              </button>
            ))}
            {cameras.length === 0 && (
              <div className="empty-state">
                <span className="empty-ic">
                  <Icon name="recording" />
                </span>
                No cameras yet. Add one under Cameras with <strong>Auto record</strong> on.
              </div>
            )}
          </div>
        </>
      )}

      {selected && (
        <>
          {!playing &&
            (liveUrl ? (
              <LiveRewindPlayer src={liveUrl} rewindMinutes={recSettings?.live_rewind_minutes ?? 120} />
            ) : (
              <div className="muted live-rewind-off">
                Live rewind appears here while this camera is recording (Auto record on and connected).
              </div>
            ))}
          {playing && playing.evidence_url && (
            <div className="recording-player">
              <div className="recording-player-head">
                <strong>{new Date(playing.started_at || playing.created_at).toLocaleString()}</strong>
                <button type="button" onClick={() => setPlaying(null)}>
                  {liveUrl ? "Back to live" : "Close"}
                </button>
              </div>
              <video key={playing.id} src={playing.evidence_url} controls autoPlay playsInline />
            </div>
          )}

          <div className="recording-bulk">
            <button
              type="button"
              className="btn danger"
              disabled={!checked.size || deleting}
              onClick={deleteChecked}
            >
              {deleting ? "Deleting…" : `Delete selected (${checked.size})`}
            </button>
            {settingsMessage && <span className="muted">{settingsMessage}</span>}
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="check-col">
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} aria-label="Check all" title="Check all" />
                  </th>
                  <th>Started</th>
                  <th>Type</th>
                  <th>Length</th>
                  <th>Size</th>
                  <th>Status</th>
                  <th>Clip</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => {
                  const isContinuous = job.details?.mode === "continuous";
                  return (
                    <tr key={job.id} className={playing?.id === job.id ? "row-active" : ""}>
                      <td className="check-col">
                        <input
                          type="checkbox"
                          checked={checked.has(job.id)}
                          onChange={() => toggleOne(job.id)}
                          aria-label="Select recording"
                        />
                      </td>
                      <td>{new Date(job.started_at || job.created_at).toLocaleString()}</td>
                      <td>
                        <span className={`badge ${isContinuous ? "online" : "info"}`}>
                          {isContinuous ? "Continuous" : "Event"}
                        </span>
                      </td>
                      <td>{clipLength(job)}</td>
                      <td>{sizeLabel(job.details?.size_bytes)}</td>
                      <td>
                        <span className={`badge ${job.status}`}>{job.status}</span>
                      </td>
                      <td>
                        {job.evidence_url && job.status === "ready" ? (
                          <span className="row-actions">
                            <button type="button" onClick={() => setPlaying(job)}>
                              ▶ Play
                            </button>
                            <a href={job.evidence_url} download className="badge info">
                              Download
                            </a>
                          </span>
                        ) : (
                          <span className="muted">{(job.details?.error as string) || "—"}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {hasMore && (
              <div className="load-more">
                <button type="button" className="btn secondary" onClick={() => setPages((n) => n + 1)}>
                  Load more (showing {jobs.length} of {total})
                </button>
              </div>
            )}
            {jobs.length === 0 && (
              <div className="empty-state">
                <span className="empty-ic">
                  <Icon name="recording" />
                </span>
                No recordings for this camera yet.
                {selected.auto_record
                  ? " A new file appears every 30 minutes while it's connected."
                  : " Turn on Auto record in Cameras → Edit."}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
