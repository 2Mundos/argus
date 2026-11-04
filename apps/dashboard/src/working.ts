import { useEffect, useSyncExternalStore } from "react";

// docs/specs/dashboard-working.spec.md: one count of "Argus is working for you" claims,
// outside React so any view can hold one.
let claims = 0;
const listeners = new Set<() => void>();

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

function change(by: 1 | -1) {
  claims += by;
  for (const listener of [...listeners]) listener();
}

/** Hold a working claim; the returned release is safe to call more than once. */
export function startWorking(): () => void {
  let held = true;
  change(1);
  return () => {
    if (!held) return;
    held = false;
    change(-1);
  };
}

/** Whether anything is working right now. */
export function useWorking(): boolean {
  return useSyncExternalStore(subscribe, () => claims > 0);
}

/** Hold a working claim for as long as `active` is true. */
export function useWorkingWhile(active: boolean): void {
  useEffect(() => (active ? startWorking() : undefined), [active]);
}
