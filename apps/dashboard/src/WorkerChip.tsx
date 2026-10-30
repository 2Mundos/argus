import type { Agent } from "./api";
import { eventWorker } from "./format";
import {
  agentIdFromActor,
  useWorkerPanel,
  type WorkerKind,
  workerKind,
  workerLabel,
} from "./WorkerPanel";

function dotKind(kind: WorkerKind): WorkerKind {
  return kind === "unknown" ? "idle" : kind;
}

/** Compact age for the workers rail: "12s", "3m", "2h". */
function age(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86_400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86_400)}d`;
}

export function WorkerChip({
  agentId,
  agent,
  layout = "inline",
  selected,
  className,
  now,
}: {
  agentId: string;
  agent?: Agent;
  layout?: "inline" | "rail";
  selected?: boolean;
  className?: string;
  now?: number;
}) {
  const { openWorker, selectedId, agents } = useWorkerPanel();
  const resolved = agent ?? agents?.find((a) => a.id === agentId);
  const kind = workerKind(resolved);
  const label = workerLabel(resolved, agentId);
  const isSelected = selected ?? selectedId === agentId;
  const title = [resolved?.name, resolved?.runtime?.engine, resolved?.end_reason]
    .filter(Boolean)
    .join(" · ");

  return (
    <button
      type="button"
      className={`worker-chip worker-${kind}${isSelected ? " selected" : ""}${
        layout === "inline" ? " worker-chip-inline" : ""
      }${className ? ` ${className}` : ""}`}
      title={title || agentId}
      aria-current={isSelected ? "true" : undefined}
      aria-label={`Open worker ${label}`}
      onClick={(e) => {
        e.stopPropagation();
        openWorker(agentId);
      }}
    >
      <span
        className={`worker-dot worker-state-${dotKind(kind)}${kind === "unknown" ? " worker-dot-unknown" : ""}`}
      />
      <span className="worker-name">{label}</span>
      {layout === "rail" && resolved && now !== undefined && (
        <>
          <span className="muted small worker-age">
            {kind === "ended"
              ? resolved.ended_at
                ? age(resolved.ended_at, now)
                : "—"
              : age(resolved.last_heartbeat_at, now)}
          </span>
          <span className={`worker-state worker-state-${kind}`}>
            {kind === "busy" ? "running" : kind}
          </span>
        </>
      )}
    </button>
  );
}

/** Event-table cell: chip for `agent:<id>` actors, plain text otherwise. */
export function WorkerActorChip({ actor }: { actor: string }) {
  const agentId = agentIdFromActor(actor);
  if (!agentId) return <span className="muted small">{eventWorker(actor)}</span>;
  return <WorkerChip agentId={agentId} layout="inline" />;
}
