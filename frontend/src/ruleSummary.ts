import type { AlertSound, CameraRule, RuleDetect } from "./api/client";

export const BUILTIN_SOUNDS: Array<[string, string]> = [
  ["chime", "Chime"],
  ["bell", "Bell"],
  ["doorbell", "Doorbell (ding-dong)"],
  ["beep", "Beep beep beep"],
  ["horn", "Horn"],
  ["alarm", "Alarm"],
  ["siren", "Siren"],
];

export function soundLabel(sound: string, customSounds: AlertSound[] = []): string {
  if (!sound) return "";
  const custom = customSounds.find((item) => item.sound_key === sound);
  if (custom) return custom.name;
  if (sound.startsWith("custom:")) return "Uploaded tune";
  return BUILTIN_SOUNDS.find(([value]) => value === sound)?.[1] || sound;
}

export const RULE_DETECTS: Array<[RuleDetect, string]> = [
  ["any_person", "Any person"],
  ["unknown_person", "Unknown person"],
  ["saved_person", "Saved person"],
  ["mobile_use", "Person using mobile"],
  ["stairs", "Coming up the stairs"],
];

export const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function hhmm(value: string | null | undefined): string {
  return value ? value.slice(0, 5) : "";
}

export function detectLabel(detect: RuleDetect): string {
  return RULE_DETECTS.find(([value]) => value === detect)?.[1] || detect;
}

export function whenLabel(rule: Pick<CameraRule, "start_time" | "end_time" | "days">): string {
  const time = rule.start_time && rule.end_time ? `${hhmm(rule.start_time)}–${hhmm(rule.end_time)}` : "All day";
  const days = rule.days?.length && rule.days.length < 7 ? ` · ${rule.days.map((d) => DAY_NAMES[d]).join(", ")}` : "";
  return time + days;
}

export function actionsLabel(
  rule: Pick<CameraRule, "sound" | "speak" | "save_photo" | "alert_severity" | "popup">,
  customSounds: AlertSound[] = []
): string {
  const parts: string[] = [];
  if (rule.sound) parts.push(`🔊 ${soundLabel(rule.sound, customSounds)}`);
  if (rule.speak) parts.push("🗣 voice");
  if (rule.save_photo) parts.push("📸 photo");
  if (rule.alert_severity) parts.push(`⚠ ${rule.alert_severity} alert`);
  if (rule.popup) parts.push("💬 pop-up");
  return parts.join(" · ") || "No action";
}

/** "Any person · 22:00–06:00 · 🔊 siren · 📸 photo" */
export function ruleSummary(rule: CameraRule, customSounds: AlertSound[] = []): string {
  return `${detectLabel(rule.detect)} · ${whenLabel(rule)} · ${actionsLabel(rule, customSounds)}`;
}
