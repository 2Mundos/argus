import type { ArgusEvent } from "./api";

/** Transient output kinds: not history and not status-bar material. */
export function isTransientEvent(event: Pick<ArgusEvent, "kind">): boolean {
  return event.kind === "task.log" || event.kind === "task.changes";
}

/** One-line summary for a status bar or event list row. */
export function describeEvent(event: Pick<ArgusEvent, "data">): string {
  const d = event.data as Record<string, unknown>;
  if (typeof d.from === "string" && typeof d.to === "string") return `${d.from} → ${d.to}`;
  if (typeof d.message === "string") return d.message;
  if (typeof d.reason === "string") return d.reason;
  if (typeof d.to === "string") return `→ ${d.to}`;
  return "";
}
