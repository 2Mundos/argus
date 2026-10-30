import { type FormEvent, useEffect, useState } from "react";
import { api, call, type Message } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { PageHeader } from "../PageHeader";

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

  const conversations = list?.items ?? [];
  const selected = conversations.find((c) => c.id === selectedId) ?? conversations[0] ?? null;

  useEffect(() => {
    if (selected && selectedId !== selected.id) setSelectedId(selected.id);
  }, [selected, selectedId]);

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

  if (onboarding && !onboarding.orchestrator) {
    return (
      <section className="chat-page">
        <PageHeader title="Chat" lead="Talk to the internal orchestrator." />
        <div className="empty-state">
          <p>The internal orchestrator is off.</p>
          <p className="muted small">
            Enable it in onboarding to chat about goals; it runs as a system pool and spends against
            a daily budget.
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

  async function newChat() {
    setError(undefined);
    try {
      const created = await call(api.POST("/api/conversations", { body: {} }));
      setSelectedId(created.id);
      reloadList();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function send(e: FormEvent) {
    e.preventDefault();
    if (!selected || !draft.trim() || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      await call(
        api.POST("/api/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: selected.id } },
          body: { body: draft },
        }),
      );
      setDraft("");
      reloadMessages();
      reloadList();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="chat-page">
      <PageHeader title="Chat" lead="Goals and updates with the internal orchestrator.">
        <button type="button" className="primary" onClick={() => void newChat()}>
          New chat
        </button>
      </PageHeader>
      {error && <p className="error">{error}</p>}
      <div className="chat-layout">
        <aside className="chat-list" aria-label="Conversations">
          {conversations.length === 0 && <p className="muted small">No conversations yet.</p>}
          {conversations.map((c) => (
            <button
              key={c.id}
              type="button"
              className={selected?.id === c.id ? "chat-list-item current" : "chat-list-item"}
              onClick={() => setSelectedId(c.id)}
            >
              <span className="chat-list-title">{c.title}</span>
              {c.kind === "updates" && <span className="badge">Updates</span>}
              {c.cost_usd != null && <span className="muted small">${c.cost_usd.toFixed(2)}</span>}
            </button>
          ))}
        </aside>
        <div className="chat-thread">
          {selected ? (
            <>
              <header className="chat-thread-header">
                <h2>{selected.title}</h2>
                {selected.cost_usd != null && (
                  <span className="muted small">~${selected.cost_usd.toFixed(2)} this thread</span>
                )}
              </header>
              <div className="chat-messages">
                {(messages?.items ?? []).map((m) => (
                  <article key={m.id} className={`chat-msg chat-msg-${m.state}`}>
                    <p className="muted small">
                      {m.author} · <DateChip at={m.created_at} />
                      {m.truncated ? " · truncated" : ""}
                    </p>
                    <Markdown>{m.body}</Markdown>
                  </article>
                ))}
              </div>
              <form className="chat-composer" onSubmit={(e) => void send(e)}>
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="Ask the orchestrator…"
                  rows={3}
                  maxLength={8000}
                  disabled={busy}
                />
                <button type="submit" className="primary" disabled={busy || !draft.trim()}>
                  Send
                </button>
              </form>
            </>
          ) : (
            <p className="muted">Select a conversation or start a new chat.</p>
          )}
        </div>
      </div>
    </section>
  );
}
