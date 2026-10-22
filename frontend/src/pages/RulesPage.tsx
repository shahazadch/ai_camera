import { FormEvent, useEffect, useState } from "react";
import {
  AlertSound,
  api,
  Camera,
  CameraRule,
  Organization,
  Person,
  RuleDetect,
  RuleSeverity,
  RuleSound,
} from "../api/client";
import { actionsLabel, BUILTIN_SOUNDS, DAY_NAMES, detectLabel, hhmm, RULE_DETECTS, whenLabel } from "../ruleSummary";

const PERSON_GROUPS = [
  ["employee", "Employee"],
  ["staff", "Staff"],
  ["manager", "Manager"],
  ["teacher", "Teacher"],
  ["student", "Student"],
  ["visitor", "Visitor"],
  ["contractor", "Contractor"],
  ["blacklist", "Blacklist"],
] as const;

type RuleForm = {
  name: string;
  detect: RuleDetect;
  person_types: string[];
  person_ids: string[];
  start_time: string;
  end_time: string;
  days: number[];
  sound: RuleSound;
  speak: boolean;
  speak_text: string;
  save_photo: boolean;
  alert_severity: RuleSeverity;
  popup: boolean;
  cooldown_sec: number;
  apply_to_all: boolean;
  enabled: boolean;
  camera_ids: string[];
};

const EMPTY_FORM: RuleForm = {
  name: "",
  detect: "any_person",
  person_types: [],
  person_ids: [],
  start_time: "",
  end_time: "",
  days: [],
  sound: "chime",
  speak: false,
  speak_text: "",
  save_photo: false,
  alert_severity: "",
  popup: true,
  cooldown_sec: 30,
  apply_to_all: false,
  enabled: true,
  camera_ids: [],
};

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

export default function RulesPage() {
  const [rules, setRules] = useState<CameraRule[]>([]);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [persons, setPersons] = useState<Person[]>([]);
  const [sounds, setSounds] = useState<AlertSound[]>([]);
  const [tuneName, setTuneName] = useState("");
  const [tuneFile, setTuneFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<{ id: string; url: string } | null>(null);
  const [orgs, setOrgs] = useState<Array<{ organization: Organization; role: string }>>([]);
  const [form, setForm] = useState<RuleForm>(EMPTY_FORM);
  const [editing, setEditing] = useState<CameraRule | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const canManage = orgs.some((m) => m.role === "owner" || m.role === "admin");

  async function load() {
    try {
      const [ruleList, cameraList, memberships, soundList] = await Promise.all([
        api.getCameraRules(),
        api.getCameras(),
        api.getMyMemberships(),
        api.getAlertSounds(),
      ]);
      setRules(ruleList);
      setSounds(soundList);
      setCameras(cameraList.results);
      setOrgs(memberships);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load rules.");
    }
  }

  useEffect(() => {
    load();
    api
      .getPersons()
      .then((data) => setPersons(data.results.filter((p) => p.person_type !== "unknown")))
      .catch(() => undefined);
  }, []);

  function openCreate() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setError("");
    setFormOpen(true);
  }

  function openEdit(rule: CameraRule) {
    setEditing(rule);
    setForm({
      name: rule.name,
      detect: rule.detect,
      person_types: rule.params?.person_types || [],
      person_ids: rule.params?.person_ids || [],
      start_time: hhmm(rule.start_time),
      end_time: hhmm(rule.end_time),
      days: rule.days || [],
      sound: rule.sound,
      speak: rule.speak,
      speak_text: rule.speak_text || "",
      save_photo: rule.save_photo,
      alert_severity: rule.alert_severity,
      popup: rule.popup,
      cooldown_sec: rule.cooldown_sec,
      apply_to_all: rule.apply_to_all,
      enabled: rule.enabled,
      camera_ids: rule.camera_ids.map(String),
    });
    setError("");
    setFormOpen(true);
  }

  async function saveRule(event: FormEvent) {
    event.preventDefault();
    if (!form.name.trim()) {
      setError("Give the rule a name.");
      return;
    }
    if (Boolean(form.start_time) !== Boolean(form.end_time)) {
      setError("Set both From and To, or leave both empty for all day.");
      return;
    }
    const organization = editing?.organization || orgs.find((m) => m.role === "owner" || m.role === "admin")?.organization.id;
    if (!organization) {
      setError("Only owners and admins can manage rules.");
      return;
    }
    setSaving(true);
    setError("");
    const payload: Partial<CameraRule> = {
      organization,
      name: form.name.trim(),
      detect: form.detect,
      params: form.detect === "saved_person" ? { person_types: form.person_types, person_ids: form.person_ids } : {},
      start_time: form.start_time || null,
      end_time: form.end_time || null,
      days: form.days,
      sound: form.sound,
      speak: form.speak,
      speak_text: form.speak_text.trim(),
      save_photo: form.save_photo,
      alert_severity: form.alert_severity,
      popup: form.popup,
      cooldown_sec: Math.max(0, Number(form.cooldown_sec) || 0),
      apply_to_all: form.apply_to_all,
      enabled: form.enabled,
      camera_ids: form.camera_ids,
    };
    try {
      if (editing) await api.updateCameraRule(editing.id, payload);
      else await api.createCameraRule(payload);
      setFormOpen(false);
      setMessage(editing ? "Rule updated." : "Rule created.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save rule.");
    } finally {
      setSaving(false);
    }
  }

  async function setEnabled(rule: CameraRule, enabled: boolean) {
    setRules((current) => current.map((r) => (r.id === rule.id ? { ...r, enabled } : r)));
    try {
      await api.updateCameraRule(rule.id, { enabled });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to update rule.");
      await load();
    }
  }

  async function removeRule(rule: CameraRule) {
    if (!window.confirm(`Delete rule "${rule.name}"? Cameras using it will stop applying it.`)) return;
    try {
      await api.deleteCameraRule(rule.id);
      setMessage("Rule deleted.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete rule.");
    }
  }

  async function testRule(rule: CameraRule) {
    try {
      await api.testCameraRule(rule.id);
      setMessage(`Test sent for "${rule.name}". The laptop player plays its sound if it's running.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to test rule.");
    }
  }

  const manageOrg = orgs.find((m) => m.role === "owner" || m.role === "admin")?.organization.id;

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview.url);
  }, [preview]);

  async function uploadTune(event: FormEvent) {
    event.preventDefault();
    if (!manageOrg || !tuneFile) return;
    setUploading(true);
    setError("");
    try {
      const name = tuneName.trim() || tuneFile.name.replace(/\.[^.]+$/, "");
      await api.uploadAlertSound(manageOrg, name, tuneFile);
      setTuneName("");
      setTuneFile(null);
      (event.target as HTMLFormElement).reset();
      setMessage(`Tune "${name}" uploaded. Pick it as a rule's sound.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to upload tune.");
    } finally {
      setUploading(false);
    }
  }

  async function playTune(sound: AlertSound) {
    try {
      setPreview({ id: sound.id, url: await api.getAlertSoundUrl(sound.id) });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to play tune.");
    }
  }

  async function removeTune(sound: AlertSound) {
    if (!window.confirm(`Delete tune "${sound.name}"? Rules using it switch to the chime.`)) return;
    try {
      await api.deleteAlertSound(sound.id);
      setMessage("Tune deleted.");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete tune.");
    }
  }

  return (
    <div>
      <header className="page-header">
        <div>
          <h1>Camera Rules</h1>
          <p className="muted">
            Create a rule once, then switch it on for any camera (here or in Cameras → Edit). Times are Pakistan
            time.
          </p>
        </div>
        {canManage && (
          <button className="btn" onClick={openCreate}>
            Add rule
          </button>
        )}
      </header>

      {error && !formOpen && <div className="error-banner">{error}</div>}
      {message && <div className="success-banner">{message}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>On</th>
              <th>Rule</th>
              <th>Detects</th>
              <th>When</th>
              <th>Actions</th>
              <th>Cameras</th>
              <th>Last triggered</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr key={rule.id} className={rule.enabled ? "" : "muted"}>
                <td>
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    disabled={!canManage}
                    onChange={(e) => setEnabled(rule, e.target.checked)}
                    aria-label={`Enable ${rule.name}`}
                  />
                </td>
                <td>
                  <strong>{rule.name}</strong>
                </td>
                <td>
                  {detectLabel(rule.detect)}
                  {rule.detect === "saved_person" && rule.params?.person_types?.length ? (
                    <div className="muted">{rule.params.person_types.join(", ")}</div>
                  ) : null}
                </td>
                <td>{whenLabel(rule)}</td>
                <td>{actionsLabel(rule, sounds)}</td>
                <td>
                  {rule.apply_to_all ? (
                    <span className="badge online">All cameras</span>
                  ) : rule.camera_names.length ? (
                    rule.camera_names.join(", ")
                  ) : (
                    <span className="muted">None yet</span>
                  )}
                </td>
                <td>{rule.last_triggered_at ? new Date(rule.last_triggered_at).toLocaleString() : "—"}</td>
                <td>
                  {canManage && (
                    <div className="row-actions">
                      <button onClick={() => testRule(rule)}>Test</button>
                      <button onClick={() => openEdit(rule)}>Edit</button>
                      <button className="danger-action" onClick={() => removeRule(rule)}>
                        Delete
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {!rules.length && (
              <tr>
                <td colSpan={8} className="muted">
                  No rules yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <section className="tunes-panel">
        <h2>Your tunes</h2>
        <p className="muted">
          Upload any short sound (wav, mp3, m4a, aiff, ogg, up to 3 MB). It appears in every rule's sound list; the
          laptop player downloads it automatically.
        </p>
        {canManage && (
          <form className="tune-upload" onSubmit={uploadTune}>
            <input placeholder="Tune name (optional)" value={tuneName} onChange={(e) => setTuneName(e.target.value)} />
            <input
              type="file"
              accept=".wav,.mp3,.m4a,.aiff,.aif,.ogg,audio/*"
              onChange={(e) => setTuneFile(e.target.files?.[0] || null)}
            />
            <button className="btn" type="submit" disabled={!tuneFile || uploading}>
              {uploading ? "Uploading…" : "Upload tune"}
            </button>
          </form>
        )}
        {sounds.length === 0 ? (
          <p className="muted">No uploaded tunes yet. Built-in tunes are always available.</p>
        ) : (
          <ul className="tune-list">
            {sounds.map((sound) => (
              <li key={sound.id}>
                <strong>{sound.name}</strong>
                {sound.size_bytes ? <span className="muted">{Math.ceil(sound.size_bytes / 1024)} KB</span> : null}
                {preview?.id === sound.id ? (
                  <audio src={preview.url} controls autoPlay />
                ) : (
                  <button type="button" onClick={() => playTune(sound)}>
                    ▶ Play
                  </button>
                )}
                {canManage && (
                  <button type="button" className="danger-action" onClick={() => removeTune(sound)}>
                    Delete
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {formOpen && (
        <div className="modal-backdrop" onMouseDown={() => !saving && setFormOpen(false)}>
          <form className="camera-modal" onSubmit={saveRule} onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h2>{editing ? "Edit rule" : "Add rule"}</h2>
                <p className="muted">What to look for, when, and what should happen.</p>
              </div>
              <button type="button" className="modal-close" onClick={() => setFormOpen(false)}>
                ×
              </button>
            </div>

            {error && <div className="error-banner">{error}</div>}

            <div className="form-grid">
              <label>
                Rule name
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Night intruder"
                />
              </label>
              <label>
                Detect
                <select value={form.detect} onChange={(e) => setForm({ ...form, detect: e.target.value as RuleDetect })}>
                  {RULE_DETECTS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {form.detect === "saved_person" && (
              <fieldset className="module-picker">
                <legend>Which saved people (none ticked = anyone saved)</legend>
                {PERSON_GROUPS.map(([value, label]) => (
                  <label key={value} className="check-option">
                    <input
                      type="checkbox"
                      checked={form.person_types.includes(value)}
                      onChange={() => setForm({ ...form, person_types: toggle(form.person_types, value) })}
                    />
                    <span>{label}</span>
                  </label>
                ))}
                {persons.length > 0 && (
                  <label className="full-field">
                    Or specific people
                    <select
                      multiple
                      value={form.person_ids}
                      onChange={(e) =>
                        setForm({ ...form, person_ids: Array.from(e.target.selectedOptions, (o) => o.value) })
                      }
                    >
                      {persons.map((person) => (
                        <option key={person.id} value={person.id}>
                          {person.full_name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </fieldset>
            )}

            <fieldset className="module-picker">
              <legend>When</legend>
              <div className="person-alert-window">
                <label>
                  From
                  <input type="time" value={form.start_time} onChange={(e) => setForm({ ...form, start_time: e.target.value })} />
                </label>
                <label>
                  To
                  <input type="time" value={form.end_time} onChange={(e) => setForm({ ...form, end_time: e.target.value })} />
                </label>
                <p className="muted">Leave both empty for all day. 22:00 → 06:00 covers the night.</p>
              </div>
              <div className="day-chips">
                {DAY_NAMES.map((day, index) => (
                  <label key={day} className="check-option">
                    <input
                      type="checkbox"
                      checked={form.days.includes(index)}
                      onChange={() => setForm({ ...form, days: toggle(form.days, index).sort() })}
                    />
                    <span>{day}</span>
                  </label>
                ))}
                <span className="muted">No day ticked = every day</span>
              </div>
            </fieldset>

            <fieldset className="module-picker">
              <legend>Then</legend>
              <label>
                Sound on laptop
                <select value={form.sound} onChange={(e) => setForm({ ...form, sound: e.target.value as RuleSound })}>
                  <option value="">No sound</option>
                  <optgroup label="Built-in">
                    {BUILTIN_SOUNDS.map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                  </optgroup>
                  {sounds.length > 0 && (
                    <optgroup label="Your tunes">
                      {sounds.map((sound) => (
                        <option key={sound.id} value={sound.sound_key}>
                          {sound.name}
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
              </label>
              <label className="check-option">
                <input type="checkbox" checked={form.speak} onChange={(e) => setForm({ ...form, speak: e.target.checked })} />
                <span>Speak an announcement on the laptop</span>
              </label>
              {form.speak && (
                <label className="full-field">
                  What to say
                  <input
                    value={form.speak_text}
                    onChange={(e) => setForm({ ...form, speak_text: e.target.value })}
                    placeholder={form.detect === "saved_person" ? "{name} at {camera}" : "{rule} at {camera}"}
                  />
                  <small className="muted">
                    Use {"{camera}"}, {"{name}"} (saved person) and {"{rule}"}. Empty = "
                    {form.detect === "saved_person" ? "Ali at Office" : `${form.name || "Rule"} at Office`}".
                  </small>
                </label>
              )}
              <label>
                Alert
                <select
                  value={form.alert_severity}
                  onChange={(e) => setForm({ ...form, alert_severity: e.target.value as RuleSeverity })}
                >
                  <option value="">No alert</option>
                  <option value="info">Info</option>
                  <option value="warning">Warning</option>
                  <option value="critical">Critical</option>
                </select>
              </label>
              <label className="check-option">
                <input type="checkbox" checked={form.save_photo} onChange={(e) => setForm({ ...form, save_photo: e.target.checked })} />
                <span>Save photo</span>
              </label>
              <label className="check-option">
                <input type="checkbox" checked={form.popup} onChange={(e) => setForm({ ...form, popup: e.target.checked })} />
                <span>Dashboard pop-up</span>
              </label>
              <label>
                Don't repeat for (seconds, per camera)
                <input
                  type="number"
                  min={0}
                  value={form.cooldown_sec}
                  onChange={(e) => setForm({ ...form, cooldown_sec: Number(e.target.value) })}
                />
              </label>
            </fieldset>

            <fieldset className="module-picker">
              <legend>Cameras</legend>
              <label className="check-option">
                <input
                  type="checkbox"
                  checked={form.apply_to_all}
                  onChange={(e) => setForm({ ...form, apply_to_all: e.target.checked })}
                />
                <span>Apply to all cameras (including ones added later)</span>
              </label>
              {!form.apply_to_all &&
                cameras.map((camera) => (
                  <label key={camera.id} className="check-option">
                    <input
                      type="checkbox"
                      checked={form.camera_ids.includes(camera.id)}
                      onChange={() => setForm({ ...form, camera_ids: toggle(form.camera_ids, camera.id) })}
                    />
                    <span>{camera.name}</span>
                  </label>
                ))}
            </fieldset>

            <label className="check-option active-camera-option">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              <span>Rule is on</span>
            </label>

            <div className="modal-actions">
              <button type="button" className="btn secondary" onClick={() => setFormOpen(false)}>
                Cancel
              </button>
              <button className="btn" type="submit" disabled={saving}>
                {saving ? "Saving…" : editing ? "Save rule" : "Create rule"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
