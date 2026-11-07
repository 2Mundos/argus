import type { components } from "@argus/api-client";
import { type ReactNode, useState } from "react";
import { api, call } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";

const TEXT_EXCERPT = 600;
const LIST_CAP = 10;

type Continuation = components["schemas"]["EffectiveContinuation"];

const STATUS_TEXT = {
  done: "done",
  pending: "pending",
  verification_missing: "verification missing",
} as const;

const PHASE_TEXT = {
  implement: "Implement",
  resume: "Resume",
  reconcile: "Reconcile",
  qa: "QA",
  close_evidence: "Close evidence",
} as const;

const ENV_DIMENSIONS = [
  ["installed", "installed"],
  ["credential_usable", "credential usable"],
  ["reachable", "reachable"],
  ["permitted", "permitted"],
  ["validated", "validated"],
] as const;

const GAP_TEXT: Record<string, string> = {
  acceptance_not_structured: "Acceptance criteria are not structured",
  artifacts_unknown: "No branch, PR or commit is known",
  artifact_state_unobserved: "The state of a known artifact was never observed",
};

/** A gap code as readable text; the code stays visible. */
function gapLabel(gap: string): string {
  const [code, ...rest] = gap.split(":");
  const arg = rest.join(":");
  if (code === "environment_not_ready" && arg) return `Environment not ready: ${arg}`;
  if (code === "decision_unresolved" && arg) return `Decision unresolved: ${arg}`;
  return GAP_TEXT[gap] ?? "Unrecognised gap";
}

/** Untrusted text, plain: cut at TEXT_EXCERPT with a toggle. */
function Text({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const needsMore = text.length > TEXT_EXCERPT;
  const shown = open || !needsMore ? text : `${text.slice(0, TEXT_EXCERPT).trimEnd()}…`;
  return (
    <span className="task-continuation-text">
      {shown}
      {needsMore && (
        <button type="button" className="link" onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Read more"}
        </button>
      )}
    </span>
  );
}

function Capped<T>({
  items,
  render,
}: {
  items: T[];
  render: (item: T, index: number) => ReactNode;
}) {
  return (
    <ul className="task-continuation-list">
      {items.slice(0, LIST_CAP).map((item, i) => (
        <li key={i}>{render(item, i)}</li>
      ))}
      {items.length > LIST_CAP && <li className="muted small">+{items.length - LIST_CAP} more</li>}
    </ul>
  );
}

function Block({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="task-continuation-block">
      <div className="muted small">{label}</div>
      {children}
    </div>
  );
}

function Checklist({ c }: { c: Continuation }) {
  const structured = c.checklist_state === "structured" && c.checklist.length > 0;
  if (!structured) {
    return (
      <Block label="Checklist">
        <p className="small">No structured checklist exists; nothing is confirmed complete.</p>
      </Block>
    );
  }
  return (
    <Block label="Checklist">
      <Capped
        items={c.checklist}
        render={(item) => (
          <>
            <span className={`badge task-continuation-${item.status ?? "pending"}`}>
              {STATUS_TEXT[item.status ?? "pending"]}
            </span>{" "}
            <Text text={item.text} />
            {item.evidence ? (
              <div className="muted small">
                Evidence: <Text text={item.evidence} />
              </div>
            ) : (
              <div className="muted small">No evidence recorded</div>
            )}
          </>
        )}
      />
    </Block>
  );
}

function ContinuationBody({ c }: { c: Continuation }) {
  const textOf = new Map(c.checklist.map((i) => [i.id, i.text]));
  return (
    <div className="task-continuation-body">
      <p className="small">
        Phase: <strong>{PHASE_TEXT[c.phase]}</strong>{" "}
        <span className="muted">
          ({c.phase_source === "explicit" ? "set explicitly" : "derived"})
        </span>
      </p>
      {c.objective && (
        <Block label="Objective">
          <Text text={c.objective} />
        </Block>
      )}
      {c.gaps.length > 0 && (
        <Block label="Gaps (not known)">
          <Capped
            items={c.gaps}
            render={(gap) => (
              <>
                {gapLabel(gap)} <code className="small">{gap}</code>
              </>
            )}
          />
        </Block>
      )}
      <Checklist c={c} />
      {c.pending.length > 0 && (
        <Block label={`Pending (${c.pending.length})`}>
          <Capped items={c.pending} render={(id) => <Text text={textOf.get(id) ?? id} />} />
        </Block>
      )}
      {c.artifacts.length > 0 && (
        <Block label="Artifacts">
          <Capped
            items={c.artifacts}
            render={(a) => (
              <>
                {a.kind} <Text text={a.ref} /> <span className="badge">{a.state ?? "unknown"}</span>{" "}
                <span className="muted small">
                  {a.provenance.replace("_", " ")}, observed <DateChip at={a.observed_at} />
                </span>
              </>
            )}
          />
        </Block>
      )}
      {c.environment.length > 0 && (
        <Block label="Environment">
          <Capped
            items={c.environment}
            render={(e) => (
              <>
                <Text text={e.resource} />{" "}
                <span className="muted small">
                  {ENV_DIMENSIONS.map(([key, label]) => `${label}: ${e[key] ?? "unknown"}`).join(
                    ", ",
                  )}
                </span>
              </>
            )}
          />
        </Block>
      )}
      {c.reconciliation.length > 0 && (
        <Block label="Reconciliation open">
          <Capped
            items={c.reconciliation}
            render={(r) => (
              <Text text={`${r.subject}: recorded ${r.recorded}, observed ${r.observed}`} />
            )}
          />
        </Block>
      )}
      {c.interruption && (
        <Block label="Last interruption">
          <span className="small">
            Attempt {c.interruption.attempt}: <Text text={c.interruption.reason} />
            {c.interruption.detail ? (
              <>
                {" "}
                — <Text text={c.interruption.detail} />
              </>
            ) : null}{" "}
            <DateChip at={c.interruption.at} />
          </span>
        </Block>
      )}
      {c.wip && (
        <Block label="Work in progress">
          <Text text={c.wip.summary} />
        </Block>
      )}
      {c.decisions.length > 0 && (
        <Block label="Decisions">
          <Capped
            items={c.decisions}
            render={(d) => (
              <>
                <Text text={d.question} /> → <Text text={d.answer} />{" "}
                <span className="badge">{d.status ?? "effective"}</span>
                {d.premise_outdated && <span className="badge"> premise outdated</span>}
              </>
            )}
          />
        </Block>
      )}
      <p className="muted small">
        Shows the task as it stands now, not a single attempt. Revision {c.revision}.
      </p>
    </div>
  );
}

function ContinuationLoader({ taskId }: { taskId: string }) {
  const {
    data: c,
    error,
    reload,
  } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/continuation", {
          params: { path: { task_id: taskId } },
          signal,
        }),
      ),
    [taskId],
    { accepts: (event) => event.subject_id === taskId },
  );
  return (
    <>
      {error && (
        <p className="error small">
          Could not load the continuation: {error.message}{" "}
          <button type="button" onClick={reload}>
            Retry
          </button>
        </p>
      )}
      {!c && !error && <p className="muted small">Loading…</p>}
      {c && <ContinuationBody c={c} />}
    </>
  );
}

/** Read-only continuation contract (task continuation spec 3.1); fetched on first open. */
export function TaskContinuation({ taskId }: { taskId: string }) {
  const [opened, setOpened] = useState(false);
  return (
    <details
      className="task-continuation"
      aria-label="Continuation"
      onToggle={(e) => e.currentTarget.open && setOpened(true)}
    >
      <summary className="task-section-label">Continuation</summary>
      {opened && <ContinuationLoader taskId={taskId} />}
    </details>
  );
}
