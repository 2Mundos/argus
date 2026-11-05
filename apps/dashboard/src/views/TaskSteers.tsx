import type { components } from "@argus/api-client";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { useNow } from "./Activity";

type Props = {
  taskId: string;
  attempt: number;
  claimed: boolean;
  assigneeId: string | null;
  supportsSteer?: boolean;
  /** A source already loaded for this task and attempt; without one each copy fetches. */
  source?: SteersSource;
  composerOpen: boolean;
  onComposerOpenChange: (open: boolean) => void;
  /** False for the composer in the task actions row: the Live panel owns the list. */
  showList?: boolean;
};

export interface SteersSource {
  taskId: string;
  attempt: number;
  page: components["schemas"]["SteerPage"] | undefined;
  reload: () => void;
  can: boolean;
  known: boolean;
}

/** Load the steers and the steer support once, so several copies can share them. */
export function useSteersSource({
  taskId,
  attempt,
  claimed,
  assigneeId,
  supportsSteer,
  enabled = true,
}: {
  taskId: string;
  attempt: number;
  claimed: boolean;
  assigneeId: string | null;
  supportsSteer?: boolean;
  enabled?: boolean;
}): SteersSource {
  const needsLookup = supportsSteer === undefined && claimed && !!assigneeId;
  const { data: page, reload } = useLiveData(
    (signal) =>
      enabled
        ? call(
            api.GET("/api/tasks/{task_id}/steers", {
              params: { path: { task_id: taskId }, query: { attempt } },
              signal,
            }),
          )
        : Promise.resolve(undefined),
    [taskId, attempt, enabled],
  );
  const { data: assignee, error: lookupError } = useLiveData(
    async (signal) => {
      if (!enabled || !needsLookup || !assigneeId) return undefined;
      return call(
        api.GET("/api/agents/{agent_id}", { params: { path: { agent_id: assigneeId } }, signal }),
      );
    },
    [enabled, needsLookup, assigneeId],
  );
  return {
    taskId,
    attempt,
    page,
    reload,
    can: supportsSteer ?? assignee?.runtime?.features?.includes("steer") ?? false,
    known: supportsSteer !== undefined || !needsLookup || assignee !== undefined || !!lookupError,
  };
}

/**
 * Steers for the attempt in flight (task steering spec 3.9). The composer opens
 * from the terminal's or the actions row's Steer button; the message is text, never HTML.
 * Another task or attempt gets a fresh composer: the draft never carries over, and
 * the open composer closes.
 */
export function TaskSteers(props: Props) {
  const key = `${props.taskId}:${props.attempt}`;
  const { onComposerOpenChange } = props;
  const shown = useRef(key);
  useEffect(() => {
    if (shown.current === key) return;
    shown.current = key;
    onComposerOpenChange(false);
  }, [key, onComposerOpenChange]);
  return <Steers key={key} {...props} />;
}

function Steers({
  taskId,
  attempt,
  claimed,
  assigneeId,
  supportsSteer,
  source,
  composerOpen,
  onComposerOpenChange,
  showList = true,
}: Props) {
  const now = useNow();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();
  const box = useRef<HTMLTextAreaElement>(null);
  // A send that lands after the composer moved to another task or attempt must not touch it.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const shared = source?.taskId === taskId && source.attempt === attempt ? source : undefined;
  const own = useSteersSource({
    taskId,
    attempt,
    claimed,
    assigneeId,
    supportsSteer,
    enabled: !shared,
  });
  const { reload, can, known } = shared ?? own;
  const items = showList ? ((shared ?? own).page?.items ?? []) : [];
  const busy = action.busy;

  useEffect(() => {
    if (composerOpen) box.current?.focus();
  }, [composerOpen]);

  if (!claimed && items.length === 0) return null;

  async function send() {
    const message = draft.trim();
    if (!message || busy || !can || !claimed) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/tasks/{task_id}/steers", {
            params: { path: { task_id: taskId } },
            body: { message },
          }),
        );
        if (!mounted.current) return;
        setDraft("");
        onComposerOpenChange(false);
        reload();
      } catch (err) {
        if (mounted.current) setError((err as Error).message);
      }
    }, "send");
  }

  async function cancel(steerId: string) {
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.DELETE("/api/tasks/{task_id}/steers/{steer_id}", {
            params: { path: { task_id: taskId, steer_id: steerId } },
          }),
        );
        reload();
      } catch (err) {
        setError((err as Error).message);
      }
    }, `cancel:${steerId}`);
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onComposerOpenChange(false);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  const showComposer = composerOpen && claimed && known;
  if (!showComposer && items.length === 0) return null;

  return (
    <section className="steer" aria-label="Steers">
      {showComposer && (
        <>
          <textarea
            ref={box}
            aria-label="Steer this attempt…"
            placeholder="Steer this attempt…"
            value={draft}
            disabled={!can || busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {!can && <p className="muted small">This worker does not accept steering.</p>}
          {can && (
            <div className="actions left">
              <ActionButton
                className="primary"
                pending={action.is("send")}
                disabled={busy || !draft.trim()}
                action={() => send()}
              >
                Send steer
              </ActionButton>
              <button type="button" className="link" onClick={() => onComposerOpenChange(false)}>
                Close
              </button>
            </div>
          )}
        </>
      )}
      {error && <p className="error">{error}</p>}
      {items.length > 0 && (
        <ul className="steer-list">
          {items.map((item) => (
            <li key={item.id}>
              <span className={`badge steer-${item.state}`}>
                {item.state === "unknown" ? "unknown: may have been delivered" : item.state}
              </span>
              <span className="steer-message">{item.message}</span>
              <span className="muted small">
                {item.state === "delivered" && item.delivered_at ? (
                  <>
                    delivered <DateChip at={item.delivered_at} now={now} />
                  </>
                ) : item.state === "pending" ? (
                  <>
                    sent <DateChip at={item.created_at} now={now} />
                  </>
                ) : item.state === "unknown" ? null : (
                  item.state
                )}
                {item.state === "pending" && (
                  <>
                    {" "}
                    <ActionButton
                      className="link"
                      pending={action.is(`cancel:${item.id}`)}
                      disabled={action.busy}
                      action={() => cancel(item.id)}
                    >
                      Cancel
                    </ActionButton>
                  </>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
