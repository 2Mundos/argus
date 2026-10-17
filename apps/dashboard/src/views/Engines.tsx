import { type EngineEntry, fetchEngineCatalog, readinessLabel } from "../engines";
import { useLiveData } from "../live";
import { ModelPreferences } from "./ModelPreferences";

/** Engines, executors and the local model — from GET /api/engines (read-only). */
export function Engines() {
  const { data: catalog, error, reload } = useLiveData(() => fetchEngineCatalog(), []);

  return (
    <section>
      <div className="toolbar">
        <h2>Engines</h2>
        <button type="button" onClick={() => reload()}>
          Re-check
        </button>
      </div>
      <p className="muted small">
        Model provider, harness, executor CLI and account auth are separate. Installed does not mean
        authenticated; the host login does not prove a container is ready.
      </p>
      {error && <p className="error">{error.message}</p>}
      {catalog?.local && (
        <p className="small">
          Local probe:{" "}
          {catalog.local.reachable
            ? `${catalog.local.model ?? "?"} · n_ctx ${catalog.local.n_ctx ?? "?"}`
            : `unreachable (${catalog.local.error ?? "?"})`}
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>Engine</th>
            <th>Role</th>
            <th>Model provider</th>
            <th>Executor</th>
            <th>Harness</th>
            <th>Installed</th>
            <th>Auth</th>
            <th>Validated</th>
            <th>Usable</th>
          </tr>
        </thead>
        <tbody>
          {(catalog?.engines ?? []).map((e) => (
            <EngineRow key={e.id} entry={e} />
          ))}
          {!catalog && !error && (
            <tr>
              <td colSpan={9} className="muted">
                Loading…
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <ModelPreferences />
    </section>
  );
}

function EngineRow({ entry }: { entry: EngineEntry }) {
  const r = entry.readiness;
  return (
    <tr>
      <td>
        <strong>{entry.label}</strong>
        <span className="muted small"> {entry.id}</span>
        {entry.version && <div className="muted small">{entry.version}</div>}
      </td>
      <td className="small">{entry.role.replaceAll("_", " ")}</td>
      <td className="small" title={entry.axes.model ?? ""}>
        {entry.axes.model_provider}
        {entry.axes.model ? ` / ${entry.axes.model}` : ""}
      </td>
      <td className="small">{entry.axes.executor ?? "—"}</td>
      <td className="small">{entry.axes.harness}</td>
      <td className="small">{readinessLabel(r.installed)}</td>
      <td className="small" title={r.authenticated.detail}>
        {readinessLabel(r.authenticated)}
      </td>
      <td className="small">{readinessLabel(r.validated)}</td>
      <td>
        <span className={`badge ${entry.usable ? "account-enabled" : "account-disabled"}`}>
          {entry.usable ? "yes" : "no"}
        </span>
      </td>
    </tr>
  );
}
