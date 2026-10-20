/** How the API shows a stored env value: values are write-only. */
export const REDACTED = "***";

/**
 * The `env` part of a pool PATCH from the editor's text: changed or new values
 * are set, removed keys become null, and a key left at *** keeps its stored
 * value (it is not sent). Undefined when nothing changed.
 */
export function envPatch(
  current: Record<string, string>,
  edited: Record<string, string>,
): Record<string, string | null> | undefined {
  const patch: Record<string, string | null> = {};
  for (const [key, value] of Object.entries(edited)) {
    if (!(key in current && value === REDACTED)) patch[key] = value;
  }
  for (const key of Object.keys(current)) {
    if (!(key in edited)) patch[key] = null;
  }
  return Object.keys(patch).length > 0 ? patch : undefined;
}
