const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
  ["second", 1],
];

// English to match the rest of the UI, whatever the browser locale.
const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** Clock time, "14:32:10", from an event timestamp. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** "3 minutes ago" / "in 40 seconds", relative to `now`. */
export function relative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return rtf.format(Math.round(seconds / size), unit);
    }
  }
  return "—";
}

/** Stable UTC ISO-8601 for tooltips and `dateTime` attributes. */
export function utcIso(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

/** "agent:0999…-uuid" → "agent 09990f46"; other actors unchanged. */
export function actor(value: string): string {
  const [kind, id] = value.split(":", 2);
  return kind === "agent" && id ? `agent ${shortId(id)}` : value;
}

/** Compact worker id for event tables (`agent:uuid` → short id). */
export function eventWorker(value: string): string {
  const [kind, id] = value.split(":", 2);
  if (kind === "agent" && id) return shortId(id);
  if (id) return `${kind} ${shortId(id)}`;
  return value || "—";
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}
