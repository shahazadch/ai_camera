export const API_BASE = import.meta.env.VITE_API_BASE || "/api/v1";

export interface TokenPair {
  access: string;
  refresh: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
}

export interface Site {
  id: string;
  name: string;
  organization: string;
}

export interface DashboardSummary {
  cameras_total: number;
  cameras_online: number;
  events_today: number;
  events_pending_review: number;
  alerts_open: number;
  alerts_critical: number;
  edge_devices_online: number;
  edge_devices_total: number;
}

export interface Camera {
  id: string;
  site: string;
  name: string;
  site_name: string;
  zone?: string | null;
  status: string;
  rtsp_url: string;
  enabled_modules: string[];
  last_seen_at: string | null;
  is_active: boolean;
  auto_record: boolean;
  rule_ids: string[];
  metadata: Record<string, unknown>;
  zone_name?: string | null;
  zone_polygon?: number[][] | null;
  zone_is_restricted?: boolean | null;
}

export type RuleDetect = "any_person" | "unknown_person" | "saved_person" | "mobile_use" | "stairs";
// "" = silent, a built-in tune name, or "custom:<AlertSound id>".
export type RuleSound = string;
export type RuleSeverity = "" | "info" | "warning" | "critical";

export interface CameraRule {
  id: string;
  organization: string;
  name: string;
  detect: RuleDetect;
  params: { person_types?: string[]; person_ids?: string[] };
  start_time: string | null;
  end_time: string | null;
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
  camera_names: string[];
  last_triggered_at: string | null;
}

export interface RuleHit {
  id: number;
  rule_id: string;
  rule_name: string;
  camera_id: string;
  camera_name: string;
  sound: RuleSound;
  speech: string;
  popup: boolean;
  message: string;
  event_id: string | null;
  at: string;
}

export interface AlertSound {
  id: string;
  organization: string;
  name: string;
  sound_key: string;
  size_bytes: number | null;
  created_at: string;
}

export interface DetectionEvent {
  id: string;
  camera_name: string;
  site_name: string;
  event_type: string;
  severity: string;
  confidence: number;
  review_status: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface Alert {
  id: string;
  title: string;
  message: string;
  severity: string;
  status: string;
  camera_name: string;
  site_name: string;
  event_type: string;
  created_at: string;
}

export interface Person {
  id: string;
  organization: string;
  external_id: string;
  full_name: string;
  person_type: string;
  email: string;
  department?: string;
  embedding_count: number;
  image_count: number;
  primary_image_url: string | null;
  is_active: boolean;
  /** Strict watch: cameras log mobile-use activity for this person only. */
  watch_mobile_activity?: boolean;
}

export interface PersonSighting {
  id: string;
  person: string;
  person_name: string;
  camera_name?: string | null;
  snapshot_url: string | null;
  confidence: number;
  match_score: number;
  notes?: string;
  created_at: string;
}

export interface AttendanceRecord {
  id: string;
  person_name: string;
  direction: string;
  recorded_at: string;
  confidence: number;
  is_late: boolean;
  is_overtime: boolean;
}

export interface AttendanceReport {
  total_records: number;
  clock_ins: number;
  clock_outs: number;
  late_count: number;
  overtime_count: number;
  unique_persons: number;
}

export interface Visitor {
  id: string;
  full_name: string;
  host_name: string;
  status: string;
  qr_code: string;
  purpose: string;
}

export interface UnknownSighting {
  id: string;
  camera_name?: string | null;
  confidence: number;
  track_id?: number | null;
  snapshot_path: string;
  snapshot_url?: string | null;
  reviewed: boolean;
  created_at: string;
}

export interface VisionCapture {
  id: string;
  camera: string;
  camera_name: string;
  site_name: string;
  event?: string | null;
  image_path: string;
  image_url?: string | null;
  summary: string;
  scene: string;
  people: Array<{ description?: string; activity?: string }>;
  detected_objects: Array<{ name?: string; detail?: string }>;
  risks: Array<{ level?: string; detail?: string }>;
  text_visible: string[];
  confidence: number;
  model_name: string;
  provider: string;
  created_at: string;
}

export interface VisionLLMStatus {
  configured: boolean;
  provider: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  requires_api_key?: boolean;
  ollama_available?: boolean;
  mode?: string;
}

export interface ZoneInfo {
  id: string;
  site: string;
  name: string;
  site_name: string;
  priority: string;
  is_restricted: boolean;
  loitering_seconds: number;
  max_occupancy?: number | null;
  access_start?: string | null;
  access_end?: string | null;
  is_active: boolean;
}

export interface LoiterEvent {
  id: string;
  camera_name: string;
  zone_name?: string | null;
  dwell_seconds: number;
  escalation_count: number;
  severity: string;
  created_at: string;
}

export interface CrowdSummary {
  current_occupancy: number;
  average_occupancy: number;
  maximum_occupancy: number;
  over_capacity: boolean;
  density: number;
  heatmap: { x: number; y: number; w: number }[];
  peak_hours: { peak_hour: number; max_occ: number }[];
}

export interface ObjectEvent {
  id: string;
  kind: string;
  object_class: string;
  camera_name: string;
  dwell_seconds: number;
  confidence: number;
  severity: string;
  created_at: string;
}

export interface RiskIncident {
  id: string;
  title: string;
  risk_level: string;
  risk_score: number;
  status: string;
  event_count: number;
  created_at: string;
}

export interface TimelineItem {
  kind: string;
  id: string;
  title: string;
  severity: string;
  created_at: string;
}

export interface SafetyEvent {
  id: string;
  camera_name: string;
  site_name: string;
  zone_name?: string | null;
  confidence: number;
  duration_seconds: number;
  created_at: string;
  verified?: boolean;
  environment?: string;
  density?: number;
  density_level?: string;
  trend?: string;
  persistent?: boolean;
  weapon_type?: string;
  track_id?: number | null;
  person_name?: string | null;
  associated?: boolean;
  status?: string;
  immobile_seconds?: number;
  person_type?: string;
  required_items?: string[];
  missing_items?: string[];
  compliant?: boolean;
}

export interface SafetySummary {
  fire_today: number;
  smoke_today: number;
  weapon_today: number;
  falls_open: number;
  ppe_compliance_percent: number;
  incidents_open: number;
  incidents_emergency: number;
}

export interface EmergencyIncident {
  id: string;
  title: string;
  severity: string;
  status: string;
  score: number;
  summary: string;
  recommended_actions: string[];
  site_name?: string | null;
  camera_name?: string | null;
  zone_name?: string | null;
  event_count: number;
  created_at: string;
}

export interface NotificationJob {
  id: string;
  channel: string;
  destination: string;
  subject: string;
  status: string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string;
  last_error: string;
  event: string | null;
  created_at: string;
}

export interface NotificationDeliveryLog {
  id: string;
  job: string;
  attempt: number;
  success: boolean;
  provider_response: string;
  latency_ms: number;
  error: string;
  created_at: string;
}

export interface RecordingJob {
  id: string;
  camera: string;
  camera_name: string;
  event: string | null;
  vendor: string;
  status: string;
  evidence_url: string;
  clip_key: string;
  started_at: string | null;
  ended_at: string | null;
  details: Record<string, unknown> & { mode?: string };
  created_at: string;
}

export interface SnapshotObject {
  id: string;
  camera: string;
  camera_name: string;
  event: string | null;
  snapshot_key: string;
  snapshot_url: string;
  thumbnail_url: string;
  content_type: string;
  mime_type: string;
  checksum: string;
  width: number | null;
  height: number | null;
  size_bytes: number;
  expires_at: string | null;
  uploaded_by_edge: boolean;
  storage_backend: string;
  created_at: string;
}

export interface SIEMJob {
  id: string;
  event: string | null;
  target: string;
  destination: string;
  status: string;
  attempts: number;
  max_attempts: number;
  last_error: string;
  created_at: string;
}

export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

class ApiClient {
  private accessToken: string | null = null;
  private refreshToken: string | null = null;
  private refreshPromise: Promise<boolean> | null = null;
  private onUnauthorized: (() => void) | null = null;
  private onTokensUpdated: ((access: string, refresh: string | null) => void) | null = null;

  setToken(token: string | null) {
    this.accessToken = token;
  }

  setRefreshToken(token: string | null) {
    this.refreshToken = token;
  }

  setUnauthorizedHandler(handler: (() => void) | null) {
    this.onUnauthorized = handler;
  }

  setTokensUpdatedHandler(handler: ((access: string, refresh: string | null) => void) | null) {
    this.onTokensUpdated = handler;
  }

  private async tryRefresh(): Promise<boolean> {
    if (!this.refreshToken) return false;
    if (this.refreshPromise) return this.refreshPromise;

    this.refreshPromise = (async () => {
      try {
        const response = await fetch(`${API_BASE}/auth/token/refresh/`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh: this.refreshToken }),
        });
        if (!response.ok) return false;
        const data = (await response.json()) as { access: string; refresh?: string };
        this.accessToken = data.access;
        if (data.refresh) this.refreshToken = data.refresh;
        this.onTokensUpdated?.(this.accessToken, this.refreshToken);
        return true;
      } catch {
        return false;
      } finally {
        this.refreshPromise = null;
      }
    })();

    return this.refreshPromise;
  }

  private clearSession() {
    this.accessToken = null;
    this.refreshToken = null;
    this.onUnauthorized?.();
  }

  private async request<T>(path: string, options: RequestInit = {}, retried = false): Promise<T> {
    const headers: Record<string, string> = {
      ...(options.headers as Record<string, string>),
    };
    if (!(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (this.accessToken) {
      headers.Authorization = `Bearer ${this.accessToken}`;
    }
    const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
    if (!response.ok) {
      if (response.status === 401 && !path.includes("/auth/token")) {
        if (!retried && (await this.tryRefresh())) {
          return this.request<T>(path, options, true);
        }
        this.clearSession();
      }
      const body = await response.json().catch(() => ({}));
      throw new Error(body?.error?.message || body?.detail || `Request failed: ${response.status}`);
    }
    if (response.status === 204) return undefined as T;
    return response.json();
  }

  login(username: string, password: string) {
    return this.request<TokenPair>("/auth/token/", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  }

  getOrganizations() {
    return this.request<Paginated<Organization>>("/organizations/");
  }

  getMyMemberships() {
    return this.request<Array<{ organization: Organization; role: string }>>("/memberships/me/");
  }

  getSites() {
    return this.request<Paginated<Site>>("/sites/");
  }

  getDashboardSummary() {
    return this.request<DashboardSummary>("/dashboard/summary/");
  }

  getCameras() {
    return this.request<Paginated<Camera>>("/cameras/");
  }

  getCameraRules() {
    return this.request<CameraRule[]>("/camera-rules/");
  }

  createCameraRule(payload: Partial<CameraRule>) {
    return this.request<CameraRule>("/camera-rules/", { method: "POST", body: JSON.stringify(payload) });
  }

  updateCameraRule(id: string, payload: Partial<CameraRule>) {
    return this.request<CameraRule>(`/camera-rules/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }

  deleteCameraRule(id: string) {
    return this.request<void>(`/camera-rules/${id}/`, { method: "DELETE" });
  }

  testCameraRule(id: string) {
    return this.request<RuleHit>(`/camera-rules/${id}/test/`, { method: "POST" });
  }

  getAlertSounds() {
    return this.request<AlertSound[]>("/camera-rules/sounds/");
  }

  uploadAlertSound(organization: string, name: string, file: File) {
    const form = new FormData();
    form.append("organization", organization);
    form.append("name", name);
    form.append("file", file);
    return this.request<AlertSound>("/camera-rules/sounds/", { method: "POST", body: form });
  }

  deleteAlertSound(id: string) {
    return this.request<void>(`/camera-rules/sounds/${id}/`, { method: "DELETE" });
  }

  /** Uploaded tune as a blob URL for the preview player (caller revokes it). */
  async getAlertSoundUrl(id: string): Promise<string> {
    const headers: Record<string, string> = {};
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    const response = await fetch(`${API_BASE}/camera-rules/sounds/${id}/file/`, { headers });
    if (!response.ok) throw new Error(`Could not load tune: ${response.status}`);
    return URL.createObjectURL(await response.blob());
  }

  getRuleHits(after: number | null) {
    const query = after === null ? "" : `?after=${after}`;
    return this.request<{ latest: number; hits: RuleHit[] }>(`/camera-rules/hits/${query}`);
  }

  monitorCamerasTick(vision = true) {
    const visionParam = vision ? "1" : "0";
    return this.request<{
      checked: number;
      ok: number;
      cameras: Array<{
        camera_id: string;
        camera_name: string;
        ok: boolean;
        persons: number;
        objects: string[];
        vision: { id: string; summary: string } | null;
        error?: string;
      }>;
      timestamp: string;
    }>(`/cameras/monitor/tick/?vision=${visionParam}`, {
      method: "POST",
      body: "{}",
    });
  }

  createCamera(payload: Record<string, unknown>) {
    return this.request<Camera>("/cameras/", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  updateCamera(id: string, payload: Record<string, unknown>) {
    return this.request<Camera>(`/cameras/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }

  async deleteCamera(id: string) {
    try {
      await this.request<void>(`/cameras/${id}/`, { method: "DELETE" });
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      // A second click, or a camera already removed, should count as deleted.
      if (/not found|404/i.test(message)) return;
      throw err;
    }
  }

  getRecorderStatus() {
    return this.request<{
      count: number;
      workers: Array<{
        camera_id: string;
        recording: boolean;
        segments_written: number;
        last_error: string;
      }>;
    }>("/cameras/recorder/status/");
  }

  testCamera(id: string) {
    return this.request<{ ok: boolean; width?: number; height?: number; detail?: string }>(
      `/cameras/${id}/test/`,
      { method: "POST", body: "{}" }
    );
  }

  async getCameraFrame(
    id: string,
    detect = false,
    signal?: AbortSignal
  ): Promise<{
    blob: Blob;
    detections: number;
    objects: string[];
    stairsAlert: boolean;
    verify: {
      verified: number;
      unknown: number;
      threat: number;
      dangers: number;
      labels: string[];
      issue: string;
      announce: string;
    };
  }> {
    const headers: Record<string, string> = {};
    if (this.accessToken) headers.Authorization = `Bearer ${this.accessToken}`;
    const detectParam = detect ? "&detect=1" : "";
    const response = await fetch(`${API_BASE}/cameras/${id}/frame/?t=${Date.now()}${detectParam}`, {
      headers,
      cache: "no-store",
      signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const err = new Error(body?.detail || `Live preview failed: ${response.status}`) as Error & {
        status?: number;
      };
      err.status = response.status;
      throw err;
    }
    const detections = Number(response.headers.get("X-Detections") || "0");
    const objects = (response.headers.get("X-Objects") || "")
      .split(",")
      .map((label) => label.trim())
      .filter(Boolean);
    const verifyLabels = (response.headers.get("X-Verify-Labels") || "")
      .split(",")
      .map((label) => label.trim())
      .filter(Boolean);
    return {
      blob: await response.blob(),
      detections,
      objects,
      stairsAlert: response.headers.get("X-Stairs-Alert") === "1",
      verify: {
        verified: Number(response.headers.get("X-Verify-Ok") || "0"),
        unknown: Number(response.headers.get("X-Verify-Unknown") || "0"),
        threat: Number(response.headers.get("X-Verify-Threat") || "0"),
        dangers: Number(response.headers.get("X-Verify-Danger") || "0"),
        labels: verifyLabels,
        issue: (response.headers.get("X-Verify-Issue") || "").trim(),
        announce: (response.headers.get("X-Verify-Announce") || "").trim(),
      },
    };
  }

  analyzeCameraVision(id: string) {
    return this.request<VisionCapture | { skipped?: boolean; detail?: string; detected?: number }>(
      `/cameras/${id}/vision/`,
      {
        method: "POST",
        body: "{}",
      }
    );
  }

  getVisionCaptures() {
    return this.request<Paginated<VisionCapture>>("/vision/captures/");
  }

  deleteAllVisionCaptures() {
    return this.request<{ deleted: number }>("/vision/captures/", {
      method: "DELETE",
    });
  }

  getVisionStatus() {
    return this.request<VisionLLMStatus>("/vision/status/");
  }

  getEvents() {
    return this.request<Paginated<DetectionEvent>>("/events/");
  }

  deleteAllEvents() {
    return this.request<{ deleted: number }>("/events/", {
      method: "DELETE",
    });
  }

  getAlerts() {
    return this.request<Paginated<Alert>>("/alerts/");
  }

  acknowledgeAlert(id: string) {
    return this.request<Alert>(`/alerts/${id}/acknowledge/`, {
      method: "PATCH",
      body: JSON.stringify({ status: "acknowledged" }),
    });
  }

  reviewEvent(id: string, review_status: string) {
    return this.request<DetectionEvent>(`/events/${id}/review/`, {
      method: "PATCH",
      body: JSON.stringify({ review_status }),
    });
  }

  getPersons() {
    return this.request<Paginated<Person>>("/persons/");
  }

  getPersonSightings(personId?: string) {
    const query = personId ? `?person=${personId}` : "";
    return this.request<Paginated<PersonSighting>>(`/persons/sightings/${query}`);
  }

  deletePersonSighting(id: string) {
    return this.request<void>(`/persons/sightings/${id}/`, { method: "DELETE" });
  }

  createPerson(payload: Record<string, unknown>) {
    return this.request<Person>("/persons/", { method: "POST", body: JSON.stringify(payload) });
  }

  updatePerson(id: string, payload: Record<string, unknown>) {
    return this.request<Person>(`/persons/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  }

  createConsent(payload: Record<string, unknown>) {
    return this.request("/consents/", { method: "POST", body: JSON.stringify(payload) });
  }

  enrollPerson(personId: string) {
    return this.request("/persons/enroll/", {
      method: "POST",
      body: JSON.stringify({ person_id: personId, quality_score: 0.85, blur_score: 120 }),
    });
  }

  enrollPersonWithImage(personId: string, image: File, consentId?: string) {
    return this.enrollPersonWithImages(personId, [image], consentId);
  }

  enrollPersonWithImages(
    personId: string,
    images: File[] | Partial<Record<"front" | "left" | "right" | "up" | "down", File>>,
    consentId?: string
  ) {
    const form = new FormData();
    form.append("person_id", personId);
    const angles = ["front", "left", "right", "up", "down"] as const;
    if (Array.isArray(images)) {
      images.slice(0, 5).forEach((image, index) => {
        form.append(`image_${angles[index]}`, image);
        form.append("images", image);
      });
    } else {
      angles.forEach((angle) => {
        const file = images[angle];
        if (file) form.append(`image_${angle}`, file);
      });
    }
    if (consentId) form.append("consent_id", consentId);
    return this.request<{
      person_id: string;
      full_name?: string;
      face_scanned: boolean;
      angles_saved?: number;
      angles_failed?: number;
      embedding_count?: number;
      face_particles?: number;
      face_engine?: string;
      identity_mature?: boolean;
      enrollment_complete?: boolean;
      mature_quality?: number;
      issue?: string | null;
      missing_angles?: string[];
      errors?: { angle: string; detail: string }[];
      message?: string;
      primary_image_url?: string;
    }>("/persons/enroll/", { method: "POST", body: form });
  }

  deletePerson(id: string) {
    return this.request<void>(`/persons/${id}/`, { method: "DELETE" });
  }

  getAttendance() {
    return this.request<Paginated<AttendanceRecord>>("/attendance/");
  }

  getAttendanceReport(organizationId: string) {
    return this.request<AttendanceReport>(`/attendance/report/?organization=${organizationId}`);
  }

  getVisitors() {
    return this.request<Paginated<Visitor>>("/visitors/");
  }

  createVisitor(payload: Record<string, unknown>) {
    return this.request<Visitor>("/visitors/", { method: "POST", body: JSON.stringify(payload) });
  }

  visitorAction(id: string, action: string) {
    return this.request<Visitor>(`/visitors/${id}/${action}/`, { method: "POST", body: "{}" });
  }

  getUnknownSightings() {
    return this.request<Paginated<UnknownSighting>>("/unknown-sightings/");
  }

  deleteAllUnknownSightings() {
    return this.request<{ deleted: number }>("/unknown-sightings/", {
      method: "DELETE",
    });
  }

  reviewUnknown(id: string, reviewed = true) {
    return this.request<UnknownSighting>(`/unknown-sightings/${id}/review/`, {
      method: "PATCH",
      body: JSON.stringify({ reviewed }),
    });
  }

  getZones() {
    return this.request<Paginated<ZoneInfo>>("/zones/");
  }

  getLoiterEvents() {
    return this.request<Paginated<LoiterEvent>>("/loiter-events/");
  }

  getCrowdSummary(organizationId: string) {
    return this.request<CrowdSummary>(`/crowd/summary/?organization=${organizationId}`);
  }

  getObjectEvents() {
    return this.request<Paginated<ObjectEvent>>("/object-events/");
  }

  getRiskIncidents() {
    return this.request<Paginated<RiskIncident>>("/risk-incidents/");
  }

  riskIncidentAction(id: string, action: string) {
    return this.request<RiskIncident>(`/risk-incidents/${id}/${action}/`, { method: "POST", body: "{}" });
  }

  getIncidentTimeline() {
    return this.request<{ results: TimelineItem[] }>("/incidents/timeline/");
  }

  getSafetyEvents(kind: "fire" | "smoke" | "weapon" | "fall" | "ppe") {
    return this.request<Paginated<SafetyEvent>>(`/${kind}-events/`);
  }

  getSafetySummary() {
    return this.request<SafetySummary>("/safety/summary/");
  }

  getEmergencyIncidents() {
    return this.request<Paginated<EmergencyIncident>>("/emergency-incidents/");
  }

  emergencyIncidentAction(id: string, action: string) {
    return this.request<EmergencyIncident>(`/emergency-incidents/${id}/${action}/`, {
      method: "POST",
      body: "{}",
    });
  }

  getNotifications() {
    return this.request<Paginated<NotificationJob>>("/notifications/");
  }

  getNotificationLogs() {
    return this.request<Paginated<NotificationDeliveryLog>>("/notifications/logs/");
  }

  getRecordings() {
    return this.request<Paginated<RecordingJob>>("/recordings/");
  }

  getSnapshots() {
    return this.request<Paginated<SnapshotObject>>("/snapshots/");
  }

  getSiemJobs() {
    return this.request<Paginated<SIEMJob>>("/siem/jobs/");
  }

  async getHealth() {
    const res = await fetch(`${API_BASE}/health/`);
    return res.ok;
  }

  async getReady() {
    const res = await fetch(`${API_BASE}/ready/`);
    return res.ok;
  }
}

export const api = new ApiClient();
