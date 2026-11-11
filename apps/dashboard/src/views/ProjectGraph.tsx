import { useState } from "react";
import { api, call } from "../api";
import { useLiveData } from "../live";
import { type GraphEdge, layers, lineage } from "../taskGraph";

const NODE_W = 190;
const NODE_H = 46;
const GAP_X = 70;
const GAP_Y = 14;

function loadGraph(project: string, includeDone: boolean, signal: AbortSignal) {
  return call(
    api.GET("/api/projects/{name}/task-graph", {
      params: { path: { name: project }, query: { include_done: includeDone } },
      signal,
    }),
  );
}

/** A project's task graph, read-only (task graph 3.2); the live task events refresh it. */
export function ProjectGraph({ project }: { project: string }) {
  const [includeDone, setIncludeDone] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const { data, error, reload } = useLiveData(
    (signal) => loadGraph(project, includeDone, signal),
    [project, includeDone],
  );

  if (error && !data) {
    return (
      <p className="error" role="alert">
        Could not load the task graph: {error.message}{" "}
        <button type="button" className="link" onClick={reload}>
          Retry
        </button>
      </p>
    );
  }
  if (!data) return <p className="muted">Loading task graph…</p>;

  const { columns, column } = layers(
    data.nodes.map((n) => n.id),
    data.edges,
  );
  const nodes = new Map(data.nodes.map((n) => [n.id, n]));
  const position = new Map<string, { x: number; y: number }>();
  columns.forEach((ids, c) => {
    ids.forEach((id, r) => {
      position.set(id, { x: c * (NODE_W + GAP_X), y: r * (NODE_H + GAP_Y) });
    });
  });
  const width = Math.max(1, columns.length) * (NODE_W + GAP_X) - GAP_X;
  const height = Math.max(1, ...columns.map((c) => c.length)) * (NODE_H + GAP_Y) - GAP_Y;
  const related = hovered ? lineage(hovered, data.edges) : null;
  const edgeOn = (e: GraphEdge) => related?.has(e.from) && related.has(e.to);

  return (
    <div className="task-graph">
      <div className="task-graph-bar">
        <label>
          <input
            type="checkbox"
            checked={includeDone}
            onChange={(e) => setIncludeDone(e.target.checked)}
          />{" "}
          Show tasks finished over 7 days ago
        </label>
        <span className="muted small">
          {data.nodes.length} tasks · ready tasks are outlined · a dashed border is another
          project's task · red marks a task stuck on a failed dependency
        </span>
      </div>
      {data.truncated && (
        <p className="notice" role="status">
          Only the newest tasks (and what they depend on) are shown; older ones are left out.
        </p>
      )}
      {data.nodes.length === 0 ? (
        <p className="muted">This project has no tasks to draw.</p>
      ) : (
        <div className="task-graph-scroll">
          <svg
            role="img"
            aria-label={`Task graph of ${project}`}
            width={width}
            height={height}
            viewBox={`0 0 ${width} ${height}`}
          >
            {data.edges.map((e) => {
              const a = position.get(e.from);
              const b = position.get(e.to);
              if (!a || !b) return null;
              const x1 = a.x + NODE_W;
              const y1 = a.y + NODE_H / 2;
              const x2 = b.x;
              const y2 = b.y + NODE_H / 2;
              const mid = (x1 + x2) / 2;
              return (
                <path
                  key={`${e.from}>${e.to}`}
                  className={`tg-edge${related ? (edgeOn(e) ? " is-on" : " is-dim") : ""}`}
                  d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`}
                />
              );
            })}
            {data.nodes.map((n) => {
              const at = position.get(n.id);
              if (!at) return null;
              const stuck = n.state === "pending" && n.failed_dependencies.length > 0;
              const cls = [
                "tg-node",
                `tg-${n.state}`,
                stuck && "is-stuck",
                n.project !== project && "is-foreign",
                related && (related.has(n.id) ? "is-on" : "is-dim"),
              ]
                .filter(Boolean)
                .join(" ");
              return (
                <a
                  key={n.id}
                  href={`#tasks/${n.id}`}
                  className={cls}
                  data-column={column.get(n.id)}
                  onMouseEnter={() => setHovered(n.id)}
                  onMouseLeave={() => setHovered(null)}
                  onFocus={() => setHovered(n.id)}
                  onBlur={() => setHovered(null)}
                >
                  <title>
                    {n.title} — {n.state.replace("_", " ")}
                    {stuck ? " (a dependency failed or was cancelled)" : ""}
                    {n.project !== project ? ` (project ${n.project})` : ""}
                  </title>
                  <rect x={at.x} y={at.y} width={NODE_W} height={NODE_H} rx={8} />
                  <text x={at.x + 10} y={at.y + 19}>
                    {n.title.length > 26 ? `${n.title.slice(0, 25)}…` : n.title}
                  </text>
                  <text className="tg-sub" x={at.x + 10} y={at.y + 36}>
                    {stuck ? "stuck · " : ""}
                    {n.state.replace("_", " ")}
                  </text>
                </a>
              );
            })}
          </svg>
        </div>
      )}
    </div>
  );
}
