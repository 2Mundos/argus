import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import { api, call, type Message } from "../api";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";

function messageRole(author: string): "human" | "orchestrator" | "system" {
  if (author === "system") return "system";
  if (author.startsWith("orchestrator:")) return "orchestrator";
  return "human";
}

export function Chat({ onOpenSetup }: { onOpenSetup?: (step?: 1 | 2 | 3) => void }) {
  const { data: onboarding } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
  );
  const { data: list, reload: reloadList } = useLiveData(
    (signal) => call(api.GET("/api/conversations", { signal })),
    [],
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string>();
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const conversations = list?.items ?? [];
  const selected = conversations.find((c) => c.id === selectedId) ?? null;

  const { data: messages, reload: reloadMessages } = useLiveData(
    (signal) =>
      selected
        ? call(
            api.GET("/api/conversations/{conversation_id}/messages", {
              params: { path: { conversation_id: selected.id } },
              signal,
            }),
          )
        : Promise.resolve({ items: [] as Message[] }),
    [selected?.id],
  );

  const items = messages?.items ?? [];
  const showHome = !selected || (messages !== undefined && items.length === 0);

  useEffect(() => {
    if (showHome) box.current?.focus();
  }, [showHome, selected?.id]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items.length, selected?.id]);

  if (onboarding && !onboarding.orchestrator) {
    return (
      <section className="chat-page chat-off">
        <div className="chat-home-center">
          <h1>The orchestrator is off</h1>
          <p className="muted">
            Enable it to chat about goals. It runs as a system pool and spends against a daily
            budget.
          </p>
          {onOpenSetup && (
            <button type="button" className="primary" onClick={() => onOpenSetup(3)}>
              Set up orchestrator
            </button>
          )}
        </div>
      </section>
    );
  }

  function grow(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }

  function goHome() {
    setSelectedId(null);
    setDraft("");
    setError(undefined);
    if (box.current) {
      box.current.style.height = "auto";
    }
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      let conversationId = selected?.id;
      if (!conversationId) {
        const created = await call(api.POST("/api/conversations", { body: {} }));
        conversationId = created.id;
        setSelectedId(created.id);
        reloadList();
      }
      await call(
        api.POST("/api/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId } },
          body: { body: text },
        }),
      );
      setDraft("");
      if (box.current) box.current.style.height = "auto";
      reloadMessages();
      reloadList();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  const composer = (
    <form className="chat-composer-wrap" onSubmit={(e) => void send(e)}>
      <div className="chat-composer">
        <textarea
          ref={box}
          value={draft}
          rows={1}
          maxLength={8000}
          disabled={busy}
          placeholder="Message the orchestrator"
          aria-label="Message"
          onChange={(e) => {
            setDraft(e.target.value);
            grow(e.target);
          }}
          onKeyDown={onKeyDown}
        />
        <button
          type="submit"
          className="chat-send"
          disabled={busy || !draft.trim()}
          aria-label="Send"
        >
          <svg viewBox="0 0 24 24" width={16} height={16} aria-hidden="true">
            <path
              d="M12 19V6M6 12l6-6 6 6"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </div>
      {error && <p className="error small">{error}</p>}
    </form>
  );

  return (
    <section className="chat-page">
      <aside className="chat-list" aria-label="Conversations">
        <button
          type="button"
          className={`chat-list-item chat-new${!selected ? " current" : ""}`}
          onClick={goHome}
        >
          <span className="chat-list-title">New chat</span>
        </button>
        {conversations.map((c) => (
          <button
            key={c.id}
            type="button"
            className={selected?.id === c.id ? "chat-list-item current" : "chat-list-item"}
            onClick={() => setSelectedId(c.id)}
          >
            <span className="chat-list-title">{c.title}</span>
            {c.kind === "updates" && <span className="chat-list-kind">Updates</span>}
          </button>
        ))}
      </aside>
      {showHome ? (
        <div className="chat-stage chat-home">
          <div className="chat-home-center">
            <h1>What should we work on?</h1>
            {composer}
          </div>
        </div>
      ) : (
        <div className="chat-stage chat-thread">
          <h2 className="chat-thread-title">{selected?.title}</h2>
          <div className="chat-messages" ref={scroller}>
            {items.map((m) => {
              const role = messageRole(m.author);
              return (
                <article key={m.id} className={`chat-msg chat-msg-${role}`} title={m.created_at}>
                  <Markdown>{m.body}</Markdown>
                  {m.truncated && <p className="muted small">Truncated</p>}
                </article>
              );
            })}
          </div>
          {composer}
        </div>
      )}
    </section>
  );
}
