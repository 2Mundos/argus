import { type FormEvent, useState } from "react";
import { ApiError, api, call, type Page } from "../api";
import { actor, relative } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";

export function Knowledge() {
  const [project, setProject] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const { data: projects } = useLiveData(() => call(api.GET("/api/projects")), []);
  const { data: pages } = useLiveData(
    () =>
      call(
        api.GET("/api/knowledge/pages", {
          params: { query: project ? { project } : {} },
        }),
      ),
    [project],
  );
  const { data: hits } = useLiveData(
    () =>
      query.trim()
        ? call(
            api.GET("/api/knowledge/search", {
              params: { query: { q: query, ...(project ? { project } : {}) } },
            }),
          )
        : Promise.resolve(null),
    [query, project],
  );

  const list = hits
    ? hits.map((h) => ({ id: h.page_id, title: h.title, sub: h.snippet, stale: h.stale }))
    : (pages ?? []).map((p) => ({
        id: p.id,
        title: p.title,
        sub: `${p.project} · ${p.summary}`,
        stale: p.stale,
      }));

  return (
    <section className="knowledge">
      <div className="toolbar">
        <h2>Knowledge</h2>
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">All projects</option>
          {(projects ?? []).map((p) => (
            <option key={p.slug} value={p.slug}>
              {p.name}
            </option>
          ))}
        </select>
        <input
          type="search"
          placeholder="Search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="search"
        />
        <button type="button" className="primary" onClick={() => setCreating(true)}>
          New page
        </button>
      </div>
      <div className="split">
        <ul className="page-list">
          {list.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                aria-current={selected === item.id ? "true" : undefined}
                onClick={() => {
                  setSelected(item.id);
                  setCreating(false);
                }}
              >
                <span className="title">
                  {item.title} {item.stale && <span className="badge state-blocked">stale</span>}
                </span>
                <span className="muted small">{item.sub}</span>
              </button>
            </li>
          ))}
          {list.length === 0 && (
            <li className="muted small">{hits ? "No matches." : "No pages yet."}</li>
          )}
        </ul>
        <div>
          {creating ? (
            <PageEditor
              projects={(projects ?? []).map((p) => p.slug)}
              defaultProject={project || "global"}
              onDone={(id) => {
                setCreating(false);
                if (id) setSelected(id);
              }}
            />
          ) : selected ? (
            <PageView pageId={selected} />
          ) : (
            <p className="muted">
              Shared knowledge for agents and people. Agents get relevant pages with every task they
              claim.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function PageView({ pageId }: { pageId: string }) {
  const [editing, setEditing] = useState(false);
  const [history, setHistory] = useState(false);
  const [error, setError] = useState<string>();
  const { data: page } = useLiveData(
    () =>
      call(api.GET("/api/knowledge/pages/{page_id}", { params: { path: { page_id: pageId } } })),
    [pageId],
  );
  if (!page) return null;
  if (editing) return <PageEditor page={page} onDone={() => setEditing(false)} />;

  async function act(action: "reviewed" | "archive" | "unarchive") {
    setError(undefined);
    try {
      const params = { params: { path: { page_id: pageId } } };
      if (action === "reviewed")
        await call(api.POST("/api/knowledge/pages/{page_id}/reviewed", params));
      else if (action === "archive")
        await call(api.POST("/api/knowledge/pages/{page_id}/archive", params));
      else await call(api.POST("/api/knowledge/pages/{page_id}/unarchive", params));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <article className="card page">
      <div className="toolbar">
        <h2>{page.title}</h2>
        {page.state === "active" && (
          <button type="button" onClick={() => setEditing(true)}>
            Edit
          </button>
        )}
        <button type="button" onClick={() => act("reviewed")}>
          Still accurate
        </button>
        <button
          type="button"
          onClick={() => act(page.state === "active" ? "archive" : "unarchive")}
        >
          {page.state === "active" ? "Archive" : "Unarchive"}
        </button>
      </div>
      <p className="muted small">
        {page.project} · revision {page.revision_number} by {actor(page.author)}{" "}
        {relative(page.updated_at)} · reviewed {relative(page.reviewed_at)}
        {page.stale && <span className="badge state-blocked"> stale</span>}
        {page.state === "archived" && <span className="badge"> archived</span>}
      </p>
      {error && <p className="error">{error}</p>}
      {page.summary && <p className="summary">{page.summary}</p>}
      <Markdown>{page.body || "_Empty page._"}</Markdown>
      {page.sources.length > 0 && (
        <p className="muted small">
          Sources: {page.sources.map((s) => `${s.type} ${s.ref.slice(0, 8)}`).join(", ")}
        </p>
      )}
      <button type="button" className="link" onClick={() => setHistory((h) => !h)}>
        {history ? "Hide history" : "History"}
      </button>
      {history && <History page={page} />}
    </article>
  );
}

function History({ page }: { page: Page }) {
  const [error, setError] = useState<string>();
  const { data: revisions } = useLiveData(
    () =>
      call(
        api.GET("/api/knowledge/pages/{page_id}/revisions", {
          params: { path: { page_id: page.id } },
        }),
      ),
    [page.id, page.revision_id],
  );

  async function restore(revisionId: string, number: number) {
    setError(undefined);
    try {
      await call(
        api.POST("/api/knowledge/pages/{page_id}/restore", {
          params: { path: { page_id: page.id } },
          body: {
            revision_id: revisionId,
            base_revision_id: page.revision_id,
            reason: `restored from the dashboard (revision ${number})`,
          },
        }),
      );
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <>
      {error && <p className="error">{error}</p>}
      <ol className="events">
        {(revisions ?? []).map((r) => (
          <li key={r.id}>
            <time dateTime={r.created_at}>{relative(r.created_at)}</time>
            <span className="badge">r{r.number}</span>
            <span className="small">{actor(r.author)}</span>
            <span className="muted small">{r.reason}</span>
            {r.id !== page.revision_id && page.state === "active" ? (
              <button type="button" className="link" onClick={() => restore(r.id, r.number)}>
                Restore
              </button>
            ) : (
              <span className="muted small">current</span>
            )}
          </li>
        ))}
      </ol>
    </>
  );
}

function PageEditor(props: {
  page?: Page;
  projects?: string[];
  defaultProject?: string;
  onDone: (pageId?: string) => void;
}) {
  const { page } = props;
  const [form, setForm] = useState({
    project: props.defaultProject ?? "global",
    title: page?.title ?? "",
    summary: page?.summary ?? "",
    body: page?.body ?? "",
    tags: page?.tags.join(", ") ?? "",
    reason: "",
  });
  const [error, setError] = useState<string>();
  const set = (key: keyof typeof form, value: string) => setForm((f) => ({ ...f, [key]: value }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const content = {
      title: form.title,
      summary: form.summary,
      body: form.body,
      tags: form.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      reason: form.reason,
    };
    try {
      const result = page
        ? await call(
            api.POST("/api/knowledge/pages/{page_id}/revisions", {
              params: { path: { page_id: page.id } },
              body: { ...content, base_revision_id: page.revision_id },
            }),
          )
        : await call(
            api.POST("/api/knowledge/pages", { body: { ...content, project: form.project } }),
          );
      props.onDone(result.page?.id);
    } catch (err) {
      if (err instanceof ApiError && err.code === "knowledge_conflict") {
        setError(
          "Someone else changed this page while you were editing. Copy your text, cancel, and re-apply it on the latest version.",
        );
      } else setError((err as Error).message);
    }
  }

  return (
    <form className="card editor" onSubmit={submit}>
      <h2>{page ? `Edit: ${page.title}` : "New page"}</h2>
      {!page && (
        <label>
          Project
          <select value={form.project} onChange={(e) => set("project", e.target.value)}>
            {(props.projects ?? ["global"]).map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>
      )}
      <label>
        Title
        <input value={form.title} onChange={(e) => set("title", e.target.value)} required />
      </label>
      <label>
        Summary (one line, shown in search results and claims)
        <input value={form.summary} onChange={(e) => set("summary", e.target.value)} />
      </label>
      <label>
        Body (Markdown)
        <textarea rows={16} value={form.body} onChange={(e) => set("body", e.target.value)} />
      </label>
      <div className="row">
        <label>
          Tags (comma-separated)
          <input value={form.tags} onChange={(e) => set("tags", e.target.value)} />
        </label>
        <label>
          Reason for the change
          <input value={form.reason} onChange={(e) => set("reason", e.target.value)} />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" onClick={() => props.onDone()}>
          Cancel
        </button>
        <button type="submit" className="primary">
          {page ? "Publish revision" : "Create page"}
        </button>
      </div>
    </form>
  );
}
