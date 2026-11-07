import { useEffect, useSyncExternalStore } from "react";

/**
 * Unsaved edits and the hash route. Every page change goes through the hash, so one
 * `hashchange` listener asks before a dirty form is left and puts the old hash back
 * when the answer is no; reloads and closes get the browser's own prompt.
 */
const dirty = new Set<object>();
const listeners = new Set<() => void>();
let current = "";
let installed = false;

export const LEAVE_MESSAGE = "You have unsaved changes. Leave and discard them?";

/** True when nothing is unsaved or the user agrees to discard it. */
export function confirmDiscard(): boolean {
  if (dirty.size === 0) return true;
  if (!window.confirm(LEAVE_MESSAGE)) return false;
  dirty.clear();
  return true;
}

function install() {
  if (installed) return;
  installed = true;
  current = window.location.hash;
  window.addEventListener("hashchange", () => {
    if (window.location.hash === current) return;
    if (!confirmDiscard()) {
      history.replaceState(null, "", current || window.location.pathname + window.location.search);
      return;
    }
    current = window.location.hash;
    for (const listener of listeners) listener();
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty.size === 0) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

/** Call `listener` after each hash change the user did not take back. */
export function subscribeHash(listener: () => void): () => void {
  install();
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The current hash, re-rendering on each accepted change. */
export function useHash(): string {
  return useSyncExternalStore(subscribeHash, () => window.location.hash);
}

/** Mark this component's edits unsaved while `isDirty` holds. */
export function useUnsavedChanges(isDirty: boolean) {
  useEffect(() => {
    install();
    if (!isDirty) return;
    const token = {};
    dirty.add(token);
    return () => {
      dirty.delete(token);
    };
  }, [isDirty]);
}
