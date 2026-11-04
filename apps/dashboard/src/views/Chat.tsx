import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  type Agent,
  api,
  type Conversation,
  call,
  download,
  type EngineCatalog,
  type Instance,
  type Message,
  type Pool,
  uploadConversationFile,
} from "../api";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { Working } from "../Working";
import { useNow } from "./Activity";

function messageRole(author: string): "human" | "orchestrator" | "system" {
  if (author === "system") return "system";
  if (author.startsWith("orchestrator:")) return "orchestrator";
  return "human";
}

// Mirrors the server's TURN_TIMEOUT: a turn older than this was given up on.
const TURN_TIMEOUT_MS = 15 * 60 * 1000;

/** The wait line under the thread: the admin spoke last and no reply has come yet. */
export function pendingReply(
  items: Message[],
  registered: boolean,
  now: number,
): { label: string; rolling: boolean } | null {
  const last = items[items.length - 1];
  if (!last || messageRole(last.author) !== "human") return null;
  if (last.state !== "queued" && last.state !== "delivered") return null;
  if (now - Date.parse(last.created_at) > TURN_TIMEOUT_MS) return null;
  if (last.state === "delivered") return { label: "Working…", rolling: true };
  return registered
    ? { label: "Waiting for the orchestrator…", rolling: true }
    : { label: "Queued until the orchestrator is available", rolling: false };
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
          reason:
            ended.end_reason ??
            (ended.state === "lost"
              ? "The orchestrator stopped heartbeating."
              : "The orchestrator left."),
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

function Glyph({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" width={14} height={14} aria-hidden="true">
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
const CLOSE = "M6 6l12 12M18 6 6 18";
const TRASH = "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3";
const UNDO = "M3 12a9 9 0 1 0 3-6.7M3 4v5h5";
const PAPERCLIP =
  "M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48";
const MAX_CHAT_FILES = 10;

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function Chat({ onOpenSetup }: { onOpenSetup?: (step?: 1 | 2 | 3) => void }) {
  const { data: onboarding } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
  );
  const { data: list, reload: reloadList } = useLiveData(
    (signal) =>
      call(api.GET("/api/conversations", { params: { query: { archived: true } }, signal })),
    [],
  );
  const { data: poolsData, error: poolsError } = useLiveData(
    (signal) => call(api.GET("/api/pools", { signal })),
    [],
  );
  // `useLiveData` keeps the last result when a refetch fails; a failed read must not
  // keep showing a registration the dashboard can no longer confirm.
  const pools = poolsError ? undefined : poolsData;
  const systemPool = poolsData?.find((p) => p.system === "orchestrator");
  const { data: instancesData, error: instancesError } = useLiveData(
    (signal) =>
      systemPool
        ? call(api.GET("/api/instances", { params: { query: { pool: systemPool.name } }, signal }))
        : Promise.resolve([] as Instance[]),
    [systemPool?.name],
  );
  const { data: agentsData, error: agentsError } = useLiveData(
    (signal) => call(api.GET("/api/agents", { signal })),
    [],
  );
  const { data: enginesData, error: enginesError } = useLiveData(
    (signal) => call(api.GET("/api/engines", { signal })),
    [],
  );
  const instances = instancesError ? undefined : instancesData;
  const agents = agentsError ? undefined : agentsData;
  const engines = enginesError ? undefined : enginesData;
  const now = useNow(5000);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string>();
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const action = useAction();
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const uploadedIds = useRef(new Map<File, string>());

  const everything = list?.items ?? [];
  const conversations = everything.filter((c) => !c.archived_at);
  const deleted = everything.filter((c) => c.archived_at);
  const selected = everything.find((c) => c.id === selectedId) ?? null;

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
    setFiles([]);
    uploadedIds.current.clear();
    setError(undefined);
    if (box.current) {
      box.current.style.height = "auto";
    }
  }

  /** Delete is an archive: the chat leaves the list and can be restored (3.8). */
  async function setDeleted(conv: Conversation, deleting: boolean) {
    setError(undefined);
    try {
      const params = { params: { path: { conversation_id: conv.id } } };
      await call(
        deleting
          ? api.POST("/api/conversations/{conversation_id}/archive", params)
          : api.POST("/api/conversations/{conversation_id}/unarchive", params),
      );
      if (deleting) goHome();
      reloadList();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = draft.trim();
    const pending = files;
    if (!text && pending.length === 0) return;
    await action.run(async () => {
      setError(undefined);
      try {
        // `selectedId`, not the listed chat: a new one is not listed until it has a message,
        // so a retry after a failed send must still find it.
        let conversationId = selectedId;
        if (!conversationId) {
          const created = await call(api.POST("/api/conversations", { body: {} }));
          conversationId = created.id;
          setSelectedId(created.id);
        }
        const attachment_ids: string[] = [];
        for (const file of pending) {
          let id = uploadedIds.current.get(file);
          if (!id) {
            const uploaded = await uploadConversationFile(conversationId, file);
            id = uploaded.id;
            uploadedIds.current.set(file, id);
          }
          attachment_ids.push(id);
        }
        await call(
          api.POST("/api/conversations/{conversation_id}/messages", {
            params: { path: { conversation_id: conversationId } },
            body: attachment_ids.length ? { body: text, attachment_ids } : { body: text },
          }),
        );
        setDraft("");
        setFiles([]);
        uploadedIds.current.clear();
        if (box.current) box.current.style.height = "auto";
        reloadMessages();
        reloadList();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  const registration = orchestratorRegistration(pools, instances, agents, engines, now);
  const status = (
    <p className={`chat-status${registration.ok ? " registered" : ""}`} role="status">
      <span className="chat-status-word">Orchestrator: {registration.word}</span>
      <span className="muted small"> · {registration.reason}</span>
    </p>
  );

  const composer = (
    <form className="chat-composer-wrap" onSubmit={(e) => void send(e)}>
      {files.length > 0 && (
        <ul className="chat-file-chips">
          {files.map((file, i) => (
            <li key={`${file.name}-${file.size}-${i}`}>
              <span className="chat-file-chip">
                {file.name}
                <span className="muted small">{formatSize(file.size)}</span>
                <button
                  type="button"
                  className="chat-file-remove"
                  aria-label={`Remove ${file.name}`}
                  disabled={action.busy}
                  onClick={() => {
                    uploadedIds.current.delete(file);
                    setFiles((all) => all.filter((_, j) => j !== i));
                  }}
                >
                  <Glyph d={CLOSE} />
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="chat-composer">
        <textarea
          ref={box}
          value={draft}
          rows={1}
          maxLength={8000}
          disabled={action.busy}
          placeholder="Message the orchestrator"
          aria-label="Message"
          onChange={(e) => {
            setDraft(e.target.value);
            grow(e.target);
          }}
          onKeyDown={onKeyDown}
        />
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const picked = [...(e.target.files ?? [])];
            e.target.value = "";
            setFiles((all) => [...all, ...picked].slice(0, MAX_CHAT_FILES));
          }}
        />
        <button
          type="button"
          className="chat-attach"
          aria-label="Attach files"
          disabled={action.busy}
          onClick={() => picker.current?.click()}
        >
          <Glyph d={PAPERCLIP} />
        </button>
        <ActionButton
          type="submit"
          className="chat-send"
          iconOnly
          disabled={!draft.trim() && files.length === 0}
          pending={action.busy}
          aria-label="Send"
        >
          <svg viewBox="0 0 24 24" width={15} height={15} aria-hidden="true">
            <path
              d="M22 2 11 13M22 2l-7 20-4-9-9-4z"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </ActionButton>
      </div>
      {error && <p className="error small">{error}</p>}
    </form>
  );

  const pending = selected?.archived_at ? null : pendingReply(items, registration.ok, now);

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
          <div key={c.id} className="chat-list-row">
            <button
              type="button"
              className={selected?.id === c.id ? "chat-list-item current" : "chat-list-item"}
              onClick={() => setSelectedId(c.id)}
            >
              <span className="chat-list-title">{c.title}</span>
              {c.kind === "updates" && <span className="chat-list-kind">Updates</span>}
            </button>
            {c.kind === "chat" && (
              <ActionButton
                className="chat-row-action"
                iconOnly
                aria-label={`Delete ${c.title}`}
                title="Delete"
                action={() => setDeleted(c, true)}
              >
                <Glyph d={CLOSE} />
              </ActionButton>
            )}
          </div>
        ))}
        {deleted.length > 0 && (
          <details className="chat-deleted-group">
            <summary>Deleted ({deleted.length})</summary>
            {deleted.map((c) => (
              <div key={c.id} className="chat-list-row">
                <button
                  type="button"
                  className={selected?.id === c.id ? "chat-list-item current" : "chat-list-item"}
                  onClick={() => setSelectedId(c.id)}
                >
                  <span className="chat-list-title">{c.title}</span>
                </button>
                <ActionButton
                  className="chat-row-action"
                  iconOnly
                  aria-label={`Restore ${c.title}`}
                  title="Restore"
                  action={() => setDeleted(c, false)}
                >
                  <Glyph d={UNDO} />
                </ActionButton>
              </div>
            ))}
          </details>
        )}
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
          {selected && selected.kind === "chat" && !selected.archived_at && (
            <ActionButton
              className="chat-thread-delete"
              iconOnly
              aria-label="Delete chat"
              title="Delete chat"
              action={() => setDeleted(selected, true)}
            >
              <Glyph d={TRASH} />
            </ActionButton>
          )}
          <div className="chat-messages" ref={scroller}>
            {items.map((m) => {
              const role = messageRole(m.author);
              return (
                <article key={m.id} className={`chat-msg chat-msg-${role}`}>
                  {m.body ? <Markdown>{m.body}</Markdown> : null}
                  {(m.attachments ?? []).length > 0 && (
                    <ul className="chat-msg-files">
                      {(m.attachments ?? []).map((a) => (
                        <li key={a.id}>
                          <button
                            type="button"
                            className="chat-msg-file"
                            onClick={() =>
                              download(
                                `/api/conversations/${m.conversation_id}/attachments/${a.id}/content`,
                                a.filename,
                              )
                            }
                          >
                            {a.filename}
                            <span className="muted small">{formatSize(a.size)}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                  {m.truncated && <p className="muted small">Truncated</p>}
                </article>
              );
            })}
            {pending && <Working label={pending.label} rolling={pending.rolling} />}
          </div>
          {selected?.archived_at ? (
            <div className="chat-composer-wrap chat-deleted">
              <span className="muted">This chat was deleted.</span>
              <ActionButton className="primary" action={() => setDeleted(selected, false)}>
                Restore
              </ActionButton>
              {error && <p className="error small">{error}</p>}
            </div>
          ) : (
            composer
          )}
        </div>
      )}
    </section>
  );
}
