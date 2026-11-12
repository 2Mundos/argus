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
          Credentials are securely stored and managed in the encrypted SOPS secret store, but are
          not yet injected into task workers. Saving or validating a connection does not grant any
          engine access to it.
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
        Access records your intent; MCP permissions are enforced by the server, and Kubernetes
        permissions by its RBAC bindings. Revoked records remain visible. To reconnect, add a new
        integration with a new token.
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
  const [dialog, setDialog] = useState<"rotate" | "revoke">();
  const [error, setError] = useState<string>();
  const action = useAction();
  const active = connection.state === "active";
  const target = connection.config[connection.kind === "mcp" ? "url" : "server"];
  const namespace = connection.config.namespace;

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
        {connection.validated_at && (
          <div>
            Last validated: <DateChip at={connection.validated_at} />
          </div>
        )}
        <div className="muted small">Not delivered to workers</div>
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
                body: { secret },
              }),
            )
          : await call(
              api.POST("/api/projects/{slug}/integrations", {
                params: { path: { slug: project } },
                body: { kind, name: field("name"), access, config, secret },
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
        after a failed request. No worker access is enabled.
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
