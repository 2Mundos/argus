import { useWorkingWhile } from "./working";

/** Rolling-gradient status line; mounted, it also makes the wordmark roll. */
export function Working({ label, rolling = true }: { label: string; rolling?: boolean }) {
  useWorkingWhile(rolling);
  return (
    <p className="working" role="status">
      <span className={rolling ? "roll-text rolling" : "muted"}>{label}</span>
    </p>
  );
}
