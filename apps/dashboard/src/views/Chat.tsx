import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import {
  type Agent,
  api,
  call,
  type EngineCatalog,
  type Instance,
  type Message,
  type Pool,
} from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { useNow } from "./Activity";

function messageRole(author: string): "human" | "orchestrator" | "system" {
  if (author === "system") return "system";
  if (author.startsWith("orchestrator:")) return "orchestrator";
  return "human";
}

type Registration = { word: string; reason: string; ok: boolean };

/** Mirrors `agents.service.LOST_AFTER_S` (3 x the 30 s heartbeat). */
const STALE_AFTER_MS = 90_000;

function harnessCapacity(engines: EngineCatalog | undefined): string {
  if (!engines) return "Account capacity unknown.";
  const sums = engines.engines.flatMap((e) =>
    e.layers.filter((l) => l.layer === "harness" && l.accounts).map((l) => l.accounts),
  );
  if (sums.some((a) => (a?.leasable ?? 0) > 0)) return "A harness account is free.";
  if (sums.some((a) => (a?.busy ?? 0) > 0)) return "Every harness account is busy.";
  return "No harness account is free.";
}

/**
 * Registration and heartbeat evidence for the internal orchestrator, from its system
 * pool, instances and agent. This is not readiness: the daily budget and the spend
 * against it are not exposed to the dashboard, so they stay unknown, and a loading
 * or failed read is never treated as healthy.
 */
function orchestratorRegistration(
  pools: Pool[] | undefined,
  instances: Instance[] | undefined,
  agents: Agent[] | undefined,
  engines: EngineCatalog | undefined,
  now: number,
): Registration {
  const unknown = "Budget unknown.";
  if (!pools || !instances || !agents) {
    return { word: "Status unknown", reason: "Loading registration state.", ok: false };
  }
  const pool = pools.find((p) => p.system === "orchestrator");
  if (!pool) {
    return { word: "Starting", reason: "The orchestrator pool is not created yet.", ok: false };
  }
  if (pool.status === "unreachable") {
    return {
      word: "Unreachable",
      reason: pool.last_error ?? "The orchestrator pool's node cannot be reached.",
      ok: false,
    };
  }
  if (pool.status !== "ok" || pool.last_error) {
    return {
      word: "Offline",
      reason: pool.last_error ?? `The orchestrator pool is ${pool.status}.`,
      ok: false,
    };
  }
  const running = instances.filter((i) => i.state === "running");
  if (running.length === 0) {
    const failed = instances.find((i) => i.last_error);
    return {
      word: "Starting",
      reason: failed?.last_error ?? "No instance is running yet. Messages queue until it is.",
      ok: false,
    };
  }
  const ids = new Set(running.map((i) => i.id));
  const mine = agents.filter(
    (a) => a.role === "orchestrator" && a.instance_id && ids.has(a.instance_id),
  );
  const current = mine.find((a) => a.state === "active" || a.state === "draining");
  if (!current) {
    const ended = mine.find((a) => a.state === "lost" || a.state === "gone");
    return ended
      ? {
          word: ended.state === "lost" ? "Lost" : "Ended",
          reason: ended.end_reason ?? "The orchestrator stopped heartbeating.",
          ok: false,
        }
      : { word: "Starting", reason: "Waiting for the orchestrator to register.", ok: false };
  }
  if (
    current.state === "draining" ||
    running.some((i) => i.id === current.instance_id && i.draining)
  ) {
    return { word: "Draining", reason: "It is shutting down; messages queue.", ok: false };
  }
  const age = now - new Date(current.last_heartbeat_at).getTime();
  if (age > STALE_AFTER_MS) {
    return {
      word: "Stale",
      reason: `No heartbeat for ${Math.round(age / 1000)} s. Messages queue until it returns.`,
      ok: false,
    };
  }
  return {
    word: "Registered",
    reason: `Heartbeat ${Math.max(0, Math.round(age / 1000))} s ago. Readiness is not checked. ${unknown} ${harnessCapacity(engines)}`,
    ok: true,
  };
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
  const { data: pools } = useLiveData((signal) => call(api.GET("/api/pools", { signal })), []);
  const systemPool = pools?.find((p) => p.system === "orchestrator");
  const { data: instances } = useLiveData(
    (signal) =>
      systemPool
        ? call(api.GET("/api/instances", { params: { query: { pool: systemPool.name } }, signal }))
        : Promise.resolve([] as Instance[]),
    [systemPool?.name],
  );
  const { data: agents } = useLiveData((signal) => call(api.GET("/api/agents", { signal })), []);
  const { data: engines } = useLiveData((signal) => call(api.GET("/api/engines", { signal })), []);
  const now = useNow(5000);
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

  const liveness = orchestratorRegistration(pools, instances, agents, engines, now);
  const status = (
    <p className={`chat-status${liveness.ok ? " registered" : ""}`} role="status">
      <span className="chat-status-word">Orchestrator: {liveness.word}</span>
      <span className="muted small"> · {liveness.reason}</span>
    </p>
  );

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
            {status}
            <h1>What should we work on?</h1>
            {composer}
          </div>
        </div>
      ) : (
        <div className="chat-stage chat-thread">
          <header className="chat-thread-head">
            <h2 className="chat-thread-title">{selected?.title}</h2>
            {status}
            {selected?.cost_usd != null && (
              <span className="muted small">~${selected.cost_usd.toFixed(2)} this thread</span>
            )}
          </header>
          <div className="chat-messages" ref={scroller}>
            {items.map((m) => {
              const role = messageRole(m.author);
              return (
                <article key={m.id} className={`chat-msg chat-msg-${role}`}>
                  <p className="muted small chat-msg-meta">
                    {m.author} · <DateChip at={m.created_at} />
                  </p>
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
