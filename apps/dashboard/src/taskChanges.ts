import type { ArgusEvent } from "./api";

// Task changes panel (docs/specs/task-changes.spec.md 3.7).

/** Files whose patch is open when the panel loads; the rest are collapsed. */
export const OPEN_FILES = 5;

/** Attempts that keep a snapshot: the current one and the two before it. */
export const KEEP_ATTEMPTS = 3;

/** The attempt a `task.changes` event for `taskId` is about, or null for any other event. */
export function changesEvent(
  event: Pick<ArgusEvent, "kind" | "data">,
  taskId: string,
): number | null {
  if (event.kind !== "task.changes") return null;
  const d = event.data as { task_id?: unknown; attempt?: unknown };
  return d.task_id === taskId && typeof d.attempt === "number" ? d.attempt : null;
}

/** The attempts that may still hold a snapshot, newest first. */
export function snapshotAttempts(current: number): number[] {
  const out: number[] = [];
  for (let a = current; a >= 1 && out.length < KEEP_ATTEMPTS; a -= 1) out.push(a);
  return out;
}

export interface FilePatch {
  /** The path after the change, from the `diff --git a/… b/…` header. */
  path: string;
  text: string;
}

/** A patch split per file at its `diff --git` headers; text before the first is dropped. */
export function splitPatch(patch: string): FilePatch[] {
  const out: FilePatch[] = [];
  for (const piece of patch.split(/^(?=diff --git )/m)) {
    if (!piece.startsWith("diff --git ")) continue;
    const header = piece.slice(0, piece.indexOf("\n") >>> 0);
    const b = header.lastIndexOf(" b/");
    out.push({ path: b >= 0 ? header.slice(b + 3) : header.slice(11), text: piece });
  }
  return out;
}

/** The style of one patch line: added, removed, hunk header, file header or context. */
export function lineKind(line: string): "add" | "del" | "hunk" | "meta" | "ctx" {
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  if (line.startsWith("@@")) return "hunk";
  if (
    /^(diff --git|index |new file|deleted file|similarity|rename |old mode|new mode|Binary)/.test(
      line,
    )
  )
    return "meta";
  return "ctx";
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 8) : "unborn";
}
