import type { components } from "@argus/api-client";
import { useState } from "react";
import { api, call, download } from "../api";
import { shortId } from "../format";
import { useLiveData } from "../live";

type Round = components["schemas"]["ReviewRound"];
type Steward = NonNullable<components["schemas"]["ReviewRounds"]["steward"]>;

const VERDICT_TEXT = {
  approve: "approved",
  request_changes: "changes requested",
  void: "void",
} as const;

export function verdictText(verdict: Round["verdict"] | undefined, state: string): string {
  return verdict ? VERDICT_TEXT[verdict] : state === "succeeded" ? "no verdict" : state;
}

function RoundPanel({ round }: { round: Round }) {
  return (
    <div role="tabpanel" aria-label={`Review round ${round.round}`} className="task-review-round">
      <p>
        <span className={`badge review-${round.verdict ?? "none"}`}>
          {verdictText(round.verdict, round.state)}
        </span>{" "}
        {round.head_sha && (
          <span className="muted small">
            reviewed head <code>{round.head_sha.slice(0, 12)}</code>
          </span>
        )}
        {round.reviewer_model && (
          <span className="muted small">
            {" "}
            · {round.reviewer_provider ? `${round.reviewer_provider} ` : ""}
            {round.reviewer_model}
          </span>
        )}
      </p>
      {round.findings.length === 0 ? (
        <p className="muted small">No findings.</p>
      ) : (
        <ul className="plain">
          {round.findings.map((f, i) => (
            <li key={i}>
              <span className="badge">{f.priority ?? "unranked"}</span>{" "}
              {f.file && (
                <code>
                  {f.file}
                  {f.lines ? `:${f.lines}` : ""}
                </code>
              )}{" "}
              {f.description}
            </li>
          ))}
        </ul>
      )}
      <p className="small">
        <a href={`#tasks/${round.child_id}`}>Review task {shortId(round.child_id)}</a>
        {round.artifact_ids.map((id) => (
          <span key={id}>
            {" · "}
            <button
              type="button"
              className="link"
              onClick={() => download(`/api/artifacts/${id}/content`, id)}
            >
              report {shortId(id)}
            </button>
          </span>
        ))}
      </p>
    </div>
  );
}

const short = (sha: string | null | undefined) => (sha ? sha.slice(0, 12) : "unknown");

/** The newest PR steward state, shown between review rounds (PR steward spec 3.5). */
export function StewardLine({ steward }: { steward: Steward }) {
  const classes = (steward.classes ?? []).join(" + ");
  const failed = steward.failed_checks ?? [];
  if (steward.state === "repair_pushed")
    return (
      <p className="small" aria-label="PR steward">
        Repair pushed: head <code>{short(steward.old_head_sha)}</code> →{" "}
        <code>{short(steward.new_head_sha)}</code>
      </p>
    );
  if (steward.state === "stopped")
    return (
      <p className="small" aria-label="PR steward">
        PR steward stopped: {classes}
        {steward.decision_id && (
          <>
            {" · "}
            <a href="#inbox">decision</a>
          </>
        )}
      </p>
    );
  return (
    <p className="small" aria-label="PR steward">
      Repair requested: {classes}
      {failed.length > 0 && <> ({failed.join(", ")})</>}
    </p>
  );
}

/** One tab per independent review round of the task, the newest selected by default. */
export function TaskReviews({ taskId }: { taskId: string }) {
  const { data } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/reviews", { params: { path: { task_id: taskId } }, signal }),
      ).catch(() => null),
    [taskId],
  );
  const [picked, setPicked] = useState<{ taskId: string; round: number } | null>(null);
  const rounds = data?.rounds ?? [];
  const selected =
    rounds.find((r) => picked?.taskId === taskId && r.round === picked.round) ?? rounds.at(-1);
  const steward = data?.steward;
  if (!selected && !steward) return null;
  return (
    <section aria-label="Reviews">
      <header className="task-section-label">Reviews</header>
      {steward && <StewardLine steward={steward} />}
      {selected && (
        <>
          <div role="tablist" aria-label="Review rounds">
            {rounds.map((r) => (
              <button
                key={r.child_id}
                type="button"
                role="tab"
                aria-selected={r === selected}
                className={r === selected ? "primary" : undefined}
                onClick={() => setPicked({ taskId, round: r.round })}
              >
                Round {r.round} · {verdictText(r.verdict, r.state)}
              </button>
            ))}
          </div>
          <RoundPanel round={selected} />
        </>
      )}
    </section>
  );
}
