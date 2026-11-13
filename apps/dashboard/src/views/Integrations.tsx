import type { components } from "@argus/api-client";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call, type SystemSummary } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { systemEvent } from "../liveEvents";
import { PageHeader } from "../PageHeader";

type Integration = components["schemas"]["Integration"];
type IntegrationCreate = components["schemas"]["IntegrationCreate"];

function secretStore(system: SystemSummary | undefined): string {
  const check = system?.readiness.checks.secrets;
  if (!check) return "unknown";
  return check.status === "ok" ? "loaded" : "not loaded";
}

/** Never echo an API error body: even a rejected request may contain a credential. */
function failure(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.status) {
      case 401:
      case 403:
        return "An admin token is required to manage integrations.";
      case 404:
        return "The project or integration no longer exists. Refresh the list.";
      case 409:
        return "The connection changed, was revoked, or its name is already in use. Refresh the list before retrying.";
      case 422:
        return "Check the name, HTTPS URL, namespace, CA bundle and token format. Re-enter the token to retry.";
      case 503:
        return "The secret store is unavailable. Check server readiness before retrying.";
    }
  }
  return "The request could not be completed. Check the connection and try again.";
}

/** Fixed messages for an edit, which carries no token. */
function editFailure(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "stale_version":
        return "Someone changed this connection since you opened it. Close, refresh the list and edit again.";
      case "integration_revoked":
        return "This connection was revoked. Add a new integration instead.";
      case "config_invalid":
        return "Check the HTTPS URL (no credentials, query or fragment), namespace, CA bundle and tool names.";
    }
  }
  return failure(error);
}

function toolPolicy(connection: Integration): string | undefined {
  const tools = connection.config.tools;
  switch (connection.tool_policy) {
    case "allowlist":
      return `Tool allowlist: ${Array.isArray(tools) ? tools.length : 0} tools`;
    case "all":
      return "Tools: all tools the server lists";
    case "unknown":
      return "Tools: unknown coverage; unusable by workers until an allowlist is set";
  }
  return undefined;
}

export function Integrations() {
  const { data: system, error: systemError } = useLiveData(
    (signal) => call(api.GET("/api/system", { signal })),
    [],
    { accepts: systemEvent },
  );
  const {
    data: projects,
    error,
    reload,
  } = useLiveData((signal) => call(api.GET("/api/projects", { signal })), [], {
    accepts: (event) => event.subject_type === "project",
  });
  const [selected, setSelected] = useState("");
  const available = projects?.filter((project) => project.slug !== "global");
  const project = available?.find((project) => project.slug === selected) ?? available?.[0];

  return (
    <section className="settings-page">
      <PageHeader
        title="Integrations"
        lead={
          <>
            Manage project-scoped MCP servers and Kubernetes connections. Model providers and forge
            tokens stay in <a href="#setup/accounts">Accounts</a>; typed{" "}
            <a href="#setup/settings">Settings</a> never hold a secret.
          </>
        }
      />
      <div className="card">
        <p>
          Credentials are stored in the encrypted SOPS secret store. A connection reaches a worker
          only when a task of the same project selects it, and only on a Claude engine. A Kubernetes
          connection becomes an ephemeral kubeconfig pinned to the target namespace. An MCP
          connection becomes a per-attempt server URL on the worker's Argus broker, which adds the
          token and passes only the allowed tools; the session never sees the token. Either ends
          when the attempt ends or the connection is revoked, edited, rotated or expires. That does
          not revoke the token; revoke it at the cluster or MCP server too.
        </p>
        <p className="muted small">
          Encrypted secret store:{" "}
          <strong>{systemError ? "unavailable" : secretStore(system)}</strong>. An admin token is
          required. Bearer tokens are write-only and never shown again. OAuth sign-in is not
          available.
        </p>
        <p className="muted small">
          Kubernetes connections use a service-account bearer token and a configured target
          namespace. Validation means only the explicit permission checks passed; it does not prove
          complete RBAC isolation or that grants are confined to the target namespace.{" "}
          <a href="#workers/pools">Worker pools</a> place worker pods; they do not give tasks access
          to these connections.
        </p>
      </div>
      {error && (
        <p className="error" role="alert">
          Could not load projects. {failure(error)}{" "}
          <button type="button" onClick={reload} aria-label="Retry projects">
            Retry
          </button>
        </p>
      )}
      {projects === undefined && !error && <p className="muted">Loading projects…</p>}
      {available?.length === 0 && (
        <p className="muted">Create a project before adding integrations.</p>
      )}
      {project && (
        <>
          <label>
            Project{" "}
            <select
              aria-label="Project"
              value={project.slug}
              onChange={(event) => setSelected(event.target.value)}
            >
              {available?.map((project) => (
                <option key={project.slug} value={project.slug}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>
          <ProjectIntegrations key={project.slug} project={project.slug} />
        </>
      )}
    </section>
  );
}

function ProjectIntegrations({ project }: { project: string }) {
  const {
    data: items,
    error,
    reload,
  } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/projects/{slug}/integrations", {
          params: { path: { slug: project } },
          signal,
        }),
      ),
    [project],
    { accepts: (event) => event.subject_type === "integration" },
  );
  const [adding, setAdding] = useState(false);
  const [notice, setNotice] = useState<string>();

  return (
    <>
      <div className="actions left">
        <button type="button" className="primary" onClick={() => setAdding(true)}>
          Add integration
        </button>
        <button type="button" onClick={reload} aria-label="Refresh integrations">
          Refresh
        </button>
      </div>
      {notice && <p role="status">{notice}</p>}
      {error && (
        <p className="error" role="alert">
          Could not load integrations. {failure(error)}{" "}
          <button type="button" onClick={reload} aria-label="Retry integrations">
            Retry
          </button>
        </p>
      )}
      {items === undefined && !error && <p className="muted">Loading integrations…</p>}
      {items?.length === 0 && !error && <p className="muted">No integrations for this project.</p>}
      {items && items.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption>Connections for {project}</caption>
            <thead>
              <tr>
                <th scope="col">Connection</th>
                <th scope="col">Target</th>
                <th scope="col">Intended access</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <IntegrationRow key={item.id} item={item} onChanged={reload} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted small">
        Argus limits MCP workers to the tool allowlist (read only needs one); what each tool does is
        up to the server. Kubernetes permissions are enforced by its RBAC bindings. Revoked records
        remain visible. To reconnect, add a new integration with a new token.
      </p>
      {adding && (
        <CredentialForm
          project={project}
          onCancel={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            setNotice(
              "Integration saved. Validate it to check the connection; worker access is not yet available.",
            );
            reload();
          }}
        />
      )}
    </>
  );
}

function IntegrationRow({ item, onChanged }: { item: Integration; onChanged: () => void }) {
  const [saved, setSaved] = useState<Integration>();
  // A confirmed revocation stays disabled even while an older list fetch finishes.
  const connection =
    item.state === "revoked" ||
    !saved ||
    (saved.state !== "revoked" && item.updated_at > saved.updated_at)
      ? item
      : saved;
  const [dialog, setDialog] = useState<"edit" | "rotate" | "revoke">();
  const [error, setError] = useState<string>();
  const action = useAction();
  const active = connection.state === "active";
  const target = connection.config[connection.kind === "mcp" ? "url" : "server"];
  const namespace = connection.config.namespace;
  const policy = toolPolicy(connection);

  function changed(result: Integration) {
    setSaved(result);
    setDialog(undefined);
    setError(undefined);
    onChanged();
  }

  async function validate() {
    await action.run(async () => {
      setError(undefined);
      try {
        changed(
          await call(
            api.POST("/api/projects/{slug}/integrations/{integration_id}/validate", {
              params: { path: { slug: connection.project, integration_id: connection.id } },
            }),
          ),
        );
      } catch (error) {
        setError(failure(error));
      }
    });
  }

  return (
    <tr>
      <th scope="row">
        {connection.name}
        <div className="muted small">
          {connection.kind === "mcp" ? "MCP" : "Kubernetes"} · {connection.project}
        </div>
      </th>
      <td>
        {typeof target === "string" && <span>{target}</span>}
        {connection.kind === "kubernetes" && typeof namespace === "string" && (
          <div>Target namespace: {namespace}</div>
        )}
        {policy && <div className="small">{policy}</div>}
      </td>
      <td>{connection.access === "read_only" ? "Read only" : "Read and write"}</td>
      <td>
        <span className={`badge ${active ? "state-ready" : "state-cancelled"}`}>
          {connection.state}
        </span>
        <div>{connection.has_secret ? "Secret present" : "Secret absent"}</div>
        <div>Validation: {connection.validation}</div>
        {connection.validation_detail && (
          <div className="small">{connection.validation_detail}</div>
        )}
        {connection.expires_at && (
          <div>
            Expires: <DateChip at={connection.expires_at} />
          </div>
        )}
        {connection.validated_at && (
          <div>
            Last validated: <DateChip at={connection.validated_at} />
          </div>
        )}
        <div className="muted small">
          {connection.kind === "kubernetes"
            ? "Delivered only to attempts of tasks that select it"
            : "Not delivered to workers"}
        </div>
      </td>
      <td>
        {active && (
          <div className="actions left">
            <ActionButton
              pending={action.busy}
              disabled={action.busy || !connection.has_secret}
              action={validate}
              aria-label={`Validate ${connection.name}`}
            >
              Validate
            </ActionButton>
            <button
              type="button"
              disabled={action.busy}
              aria-label={`Edit ${connection.name}`}
              onClick={() => {
                setError(undefined);
                setDialog("edit");
              }}
            >
              Edit
            </button>
            <button
              type="button"
              disabled={action.busy}
              aria-label={`Rotate ${connection.name}`}
              onClick={() => {
                setError(undefined);
                setDialog("rotate");
              }}
            >
              Rotate
            </button>
            <button
              type="button"
              disabled={action.busy}
              aria-label={`Revoke ${connection.name}`}
              onClick={() => {
                setError(undefined);
                setDialog("revoke");
              }}
            >
              Revoke
            </button>
          </div>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {active && dialog === "edit" && (
          <EditForm
            integration={connection}
            onCancel={() => setDialog(undefined)}
            onSaved={changed}
          />
        )}
        {active && dialog === "rotate" && (
          <CredentialForm
            project={connection.project}
            integration={connection}
            onCancel={() => setDialog(undefined)}
            onSaved={changed}
          />
        )}
        {active && dialog === "revoke" && (
          <RevokeForm
            integration={connection}
            onCancel={() => setDialog(undefined)}
            onSaved={changed}
          />
        )}
      </td>
    </tr>
  );
}

/** Keep keyboard focus in the overlay and restore it to the action on close. */
function FormDialog({
  title,
  busy,
  onCancel,
  onSubmit,
  children,
}: {
  title: string;
  busy: boolean;
  onCancel: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    ref.current?.focus();
    return () => {
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);

  return createPortal(
    <div className="overlay">
      <form
        className="dialog"
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onSubmit={onSubmit}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            if (!busy) onCancel();
          }
          if (event.key !== "Tab") return;
          const controls = ref.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
          );
          const first = controls?.[0];
          const last = controls?.[controls.length - 1];
          if (
            event.shiftKey &&
            (document.activeElement === first || document.activeElement === ref.current)
          ) {
            event.preventDefault();
            last?.focus();
          } else if (
            !event.shiftKey &&
            (document.activeElement === last || document.activeElement === ref.current)
          ) {
            event.preventDefault();
            first?.focus();
          }
        }}
      >
        <h2>{title}</h2>
        {children}
      </form>
    </div>,
    document.body,
  );
}

function CredentialForm({
  project,
  integration,
  onCancel,
  onSaved,
}: {
  project: string;
  integration?: Integration;
  onCancel: () => void;
  onSaved: (connection: Integration) => void;
}) {
  const [kind, setKind] = useState<Integration["kind"]>(integration?.kind ?? "mcp");
  const [access, setAccess] = useState<Integration["access"]>("read_only");
  const [error, setError] = useState<string>();
  const token = useRef<HTMLInputElement>(null);
  const action = useAction();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (action.busy || !token.current) return;
    const form = event.currentTarget;
    const field = (name: string) => (form.elements.namedItem(name) as HTMLInputElement).value;
    const secret = { token: token.current.value };
    token.current.value = "";
    const expiry = field("expires_at");
    // A datetime-local value has no zone: read it as the browser's local time.
    const expires_at = expiry ? new Date(expiry).toISOString() : undefined;
    const config: IntegrationCreate["config"] = integration
      ? {}
      : kind === "mcp"
        ? { url: field("url") }
        : {
            server: field("server"),
            namespace: field("namespace"),
            ...(field("ca_data") ? { ca_data: field("ca_data") } : {}),
          };
    // Rotation sends only the secret; its target and access remain unchanged.
    void action.run(async () => {
      setError(undefined);
      try {
        const result = integration
          ? await call(
              api.POST("/api/projects/{slug}/integrations/{integration_id}/rotate", {
                params: { path: { slug: project, integration_id: integration.id } },
                body: { secret, expires_at },
              }),
            )
          : await call(
              api.POST("/api/projects/{slug}/integrations", {
                params: { path: { slug: project } },
                body: { kind, name: field("name"), access, config, secret, expires_at },
              }),
            );
        onSaved(result);
      } catch (error) {
        setError(failure(error));
      }
    });
  }

  return (
    <FormDialog
      title={integration ? `Rotate ${integration.name}` : "Add integration"}
      busy={action.busy}
      onCancel={onCancel}
      onSubmit={submit}
    >
      <p className="muted small">
        Project: {project}. Tokens are encrypted in SOPS and never shown again. Re-enter the token
        after a failed request. Workers get a Kubernetes connection only for tasks that select it.
      </p>
      <fieldset className="integration-fields" disabled={action.busy}>
        {!integration && (
          <>
            <label>
              Kind
              <select
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value as Integration["kind"]);
                  if (token.current) token.current.value = "";
                }}
              >
                <option value="mcp">MCP</option>
                <option value="kubernetes">Kubernetes</option>
              </select>
            </label>
            <label>
              Name
              <input
                name="name"
                required
                maxLength={63}
                pattern="[a-z0-9][a-z0-9\-]{0,62}"
                autoComplete="off"
              />
            </label>
            <label>
              Access
              <select
                value={access}
                onChange={(event) => setAccess(event.target.value as Integration["access"])}
              >
                <option value="read_only">Read only</option>
                <option value="read_write">Read and write</option>
              </select>
            </label>
            {kind === "mcp" ? (
              <label>
                MCP server URL
                <input
                  name="url"
                  type="url"
                  required
                  maxLength={2048}
                  autoComplete="off"
                  placeholder="https://mcp.example/api"
                />
              </label>
            ) : (
              <>
                <label>
                  API server URL
                  <input
                    name="server"
                    type="url"
                    required
                    maxLength={2048}
                    autoComplete="off"
                    placeholder="https://cluster.example"
                  />
                </label>
                <label>
                  Namespace
                  <input
                    name="namespace"
                    required
                    maxLength={63}
                    pattern="[a-z0-9]([\-a-z0-9]{0,61}[a-z0-9])?"
                    autoComplete="off"
                  />
                </label>
                <label>
                  CA bundle (base64 PEM, optional)
                  <textarea
                    name="ca_data"
                    maxLength={65536}
                    autoComplete="off"
                    spellCheck={false}
                  />
                </label>
                <p className="muted small">
                  Omit the CA bundle to use the server's system trust store. Use a dedicated service
                  account bound to a namespaced Role.
                </p>
              </>
            )}
            <p className="muted small">
              Use HTTPS without credentials, query parameters or fragments in the URL. MCP access is
              an intent label; configure permissions at the service.
            </p>
          </>
        )}
        <label>
          Bearer token
          <input
            ref={token}
            type="password"
            required
            maxLength={8192}
            pattern="[\x21-\x7e]+"
            autoComplete="off"
            spellCheck={false}
            autoCapitalize="none"
          />
        </label>
        <p className="muted small">
          {kind === "kubernetes" ? "Service-account bearer token. " : "MCP bearer token. "}1–8192
          printable characters without whitespace.
        </p>
        <label>
          Expires (optional, your local time)
          <input name="expires_at" type="datetime-local" />
        </label>
        <p className="muted small">
          When the token stops working. Argus stops delivering it then; a token with its own expiry
          claim uses the earlier of the two. Rotating replaces the expiry.
        </p>
      </fieldset>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="button" disabled={action.busy} onClick={onCancel}>
          Cancel
        </button>
        <ActionButton
          type="submit"
          className="primary"
          pending={action.busy}
          disabled={action.busy}
        >
          {integration ? "Rotate token" : "Save integration"}
        </ActionButton>
      </div>
    </FormDialog>
  );
}

function EditForm({
  integration,
  onCancel,
  onSaved,
}: {
  integration: Integration;
  onCancel: () => void;
  onSaved: (connection: Integration) => void;
}) {
  const config = integration.config;
  const text = (key: string) => (typeof config[key] === "string" ? (config[key] as string) : "");
  const [access, setAccess] = useState(integration.access);
  const [error, setError] = useState<string>();
  const action = useAction();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (action.busy) return;
    const form = event.currentTarget;
    const field = (name: string) => (form.elements.namedItem(name) as HTMLInputElement).value;
    // The config is replaced whole, so every kept key is sent again.
    let next: IntegrationCreate["config"];
    if (integration.kind === "mcp") {
      const tools = field("tools")
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      next = { url: field("url"), ...(tools.length ? { tools } : {}) };
    } else {
      next = {
        server: field("server"),
        namespace: field("namespace"),
        ...(field("ca_data") ? { ca_data: field("ca_data") } : {}),
      };
    }
    void action.run(async () => {
      setError(undefined);
      try {
        onSaved(
          await call(
            api.PATCH("/api/projects/{slug}/integrations/{integration_id}", {
              params: { path: { slug: integration.project, integration_id: integration.id } },
              body: { expected_version: integration.version, config: next, access },
            }),
          ),
        );
      } catch (error) {
        setError(editFailure(error));
      }
    });
  }

  return (
    <FormDialog
      title={`Edit ${integration.name}`}
      busy={action.busy}
      onCancel={onCancel}
      onSubmit={submit}
    >
      <p className="muted small">
        Project: {integration.project}. The stored token is kept and not needed. Saving a change
        resets validation: validate again before workers can use it.
      </p>
      <fieldset className="integration-fields" disabled={action.busy}>
        <label>
          Access
          <select
            value={access}
            onChange={(event) => setAccess(event.target.value as Integration["access"])}
          >
            <option value="read_only">Read only</option>
            <option value="read_write">Read and write</option>
          </select>
        </label>
        {integration.kind === "mcp" ? (
          <>
            <label>
              MCP server URL
              <input
                name="url"
                type="url"
                required
                maxLength={2048}
                autoComplete="off"
                defaultValue={text("url")}
              />
            </label>
            <label>
              Tool allowlist (one tool name per line)
              <textarea
                name="tools"
                autoComplete="off"
                spellCheck={false}
                defaultValue={Array.isArray(config.tools) ? config.tools.join("\n") : ""}
              />
            </label>
            <p className="muted small">
              Workers may call only the listed tools. Argus does not check what a tool does: list
              only tools you trust for this access. Without a list, a read-only connection is
              unusable by workers; a read-and-write one allows every tool.
            </p>
          </>
        ) : (
          <>
            <label>
              API server URL
              <input
                name="server"
                type="url"
                required
                maxLength={2048}
                autoComplete="off"
                defaultValue={text("server")}
              />
            </label>
            <label>
              Namespace
              <input
                name="namespace"
                required
                maxLength={63}
                pattern="[a-z0-9]([\-a-z0-9]{0,61}[a-z0-9])?"
                autoComplete="off"
                defaultValue={text("namespace")}
              />
            </label>
            <label>
              CA bundle (base64 PEM, optional)
              <textarea
                name="ca_data"
                maxLength={65536}
                autoComplete="off"
                spellCheck={false}
                defaultValue={text("ca_data")}
              />
            </label>
          </>
        )}
      </fieldset>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="button" disabled={action.busy} onClick={onCancel}>
          Cancel
        </button>
        <ActionButton
          type="submit"
          className="primary"
          pending={action.busy}
          disabled={action.busy}
        >
          Save changes
        </ActionButton>
      </div>
    </FormDialog>
  );
}

function RevokeForm({
  integration,
  onCancel,
  onSaved,
}: {
  integration: Integration;
  onCancel: () => void;
  onSaved: (connection: Integration) => void;
}) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (name !== integration.name) return;
    void action.run(async () => {
      setError(undefined);
      try {
        onSaved(
          await call(
            api.POST("/api/projects/{slug}/integrations/{integration_id}/revoke", {
              params: { path: { slug: integration.project, integration_id: integration.id } },
            }),
          ),
        );
      } catch (error) {
        setError(failure(error));
      }
    });
  }
  return (
    <FormDialog
      title={`Revoke ${integration.name}`}
      busy={action.busy}
      onCancel={onCancel}
      onSubmit={submit}
    >
      <p>
        This deletes the stored token and disables this connection in {integration.project}. Revoke
        the token at its provider too to invalidate other copies. To reconnect, add a new
        integration.
      </p>
      <label>
        Type {integration.name} to confirm
        <input
          value={name}
          disabled={action.busy}
          onChange={(event) => setName(event.target.value)}
          autoComplete="off"
        />
      </label>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="button" disabled={action.busy} onClick={onCancel}>
          Cancel
        </button>
        <ActionButton
          type="submit"
          className="danger"
          disabled={action.busy || name !== integration.name}
          pending={action.busy}
        >
          Revoke integration
        </ActionButton>
      </div>
    </FormDialog>
  );
}
