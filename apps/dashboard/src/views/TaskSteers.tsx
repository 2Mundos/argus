import { type KeyboardEvent, useEffect, useRef, useState } from "react";
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
  composerOpen: boolean;
  onComposerOpenChange: (open: boolean) => void;
  /** False for the composer in the task actions row: the Live panel owns the list. */
  showList?: boolean;
};

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
  composerOpen,
  onComposerOpenChange,
  showList = true,
}: Props) {
  const now = useNow();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const box = useRef<HTMLTextAreaElement>(null);
  // A send that lands after the composer moved to another task or attempt must not touch it.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const needsLookup = supportsSteer === undefined && claimed && !!assigneeId;
  const { data: page, reload } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/steers", {
          params: { path: { task_id: taskId }, query: { attempt } },
          signal,
        }),
      ),
    [taskId, attempt],
  );
  const { data: assignee, error: lookupError } = useLiveData(
    async (signal) => {
      if (!needsLookup || !assigneeId) return null;
      return call(
        api.GET("/api/agents/{agent_id}", { params: { path: { agent_id: assigneeId } }, signal }),
      );
    },
    [needsLookup, assigneeId],
  );

  const can = supportsSteer ?? assignee?.runtime?.features?.includes("steer") ?? false;
  const known =
    supportsSteer !== undefined || !needsLookup || assignee !== undefined || !!lookupError;
  const items = showList ? (page?.items ?? []) : [];

  useEffect(() => {
    if (composerOpen) box.current?.focus();
  }, [composerOpen]);

  if (!claimed && items.length === 0) return null;

  async function send() {
    const message = draft.trim();
    if (!message || busy || !can || !claimed) return;
    setBusy(true);
    setError(undefined);
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
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function cancel(steerId: string) {
    setError(undefined);
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
              <button
                type="button"
                className="primary"
                disabled={busy || !draft.trim()}
                onClick={() => void send()}
              >
                Send steer
              </button>
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
                    <button type="button" className="link" onClick={() => void cancel(item.id)}>
                      Cancel
                    </button>
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
