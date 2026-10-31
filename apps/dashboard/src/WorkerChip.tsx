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

function stateWord(kind: WorkerKind): string {
  return kind === "busy" ? "running" : kind;
}

/** Compact age for the workers rail: "12s", "3m", "2h". */
function age(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86_400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86_400)}d`;
}

/** Rail status mark: icon instead of a text pill so the name keeps the width. */
function WorkerStateIcon({ kind, titled = true }: { kind: WorkerKind; titled?: boolean }) {
  const label = stateWord(kind);
  // play, pause, hourglass, moon, stop — one path each, 24 viewBox.
  const path =
    kind === "busy"
      ? "M8 5.5v13l11-6.5Z"
      : kind === "idle"
        ? "M8 5h3v14H8Zm5 0h3v14h-3Z"
        : kind === "draining"
          ? "M7 3h10v3.5L13.5 11 17 15.5V21H7v-5.5L10.5 11 7 6.5Zm2 2.7 3.5 3.8L15 5.7Zm0 12.6h6v-1.8l-3-3.5-3 3.5Z"
          : kind === "quiet"
            ? "M12 3.5A7.5 7.5 0 1 0 19.2 15 6.2 6.2 0 0 1 12 3.5Z"
            : kind === "ended"
              ? "M7 7h10v10H7Z"
              : "M12 4a8 8 0 1 1 0 16 8 8 0 0 1 0-16Zm0 2a6 6 0 1 0 0 12 6 6 0 0 0 0-12Z";
  return (
    <span
      className={`worker-state worker-state-${kind}`}
      title={titled ? label : undefined}
      aria-label={titled ? label : undefined}
      aria-hidden={titled ? undefined : true}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d={path} fill="currentColor" />
      </svg>
    </span>
  );
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
  layout?: "inline" | "rail" | "icon";
  selected?: boolean;
  className?: string;
  now?: number;
}) {
  const { openWorker, selectedId, agents } = useWorkerPanel();
  const resolved = agent ?? agents?.find((a) => a.id === agentId);
  const kind = workerKind(resolved);
  const label = workerLabel(resolved, agentId);
  const isSelected = selected ?? selectedId === agentId;
  const state = stateWord(kind);
  const title =
    layout === "icon"
      ? (resolved?.name ?? label)
      : [resolved?.name, resolved?.runtime?.engine, state, resolved?.end_reason]
          .filter(Boolean)
          .join(" · ");

  return (
    <button
      type="button"
      className={`worker-chip worker-${kind}${isSelected ? " selected" : ""}${
        layout === "inline" ? " worker-chip-inline" : ""
      }${layout === "icon" ? " worker-chip-icon" : ""}${className ? ` ${className}` : ""}`}
      title={title || agentId}
      aria-current={isSelected ? "true" : undefined}
      aria-label={`Open worker ${label}${layout === "icon" ? "" : ` (${state})`}`}
      onClick={(e) => {
        e.stopPropagation();
        openWorker(agentId);
      }}
    >
      {layout === "icon" ? (
        <WorkerStateIcon kind={kind} titled={false} />
      ) : (
        <>
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
              <WorkerStateIcon kind={kind} />
            </>
          )}
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
