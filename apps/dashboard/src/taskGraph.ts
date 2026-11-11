export interface GraphEdge {
  from: string;
  to: string;
}

export interface Layers {
  /** Node ids per column, in row order. */
  columns: string[][];
  column: Map<string, number>;
}

/**
 * Layered left-to-right layout (task graph 3.2): a node's column is the length of its
 * longest dependency chain; rows within a column follow the barycenter of the
 * neighbours in the previous column, then the next, for a few sweeps.
 */
export function layers(ids: string[], edges: GraphEdge[]): Layers {
  const known = new Set(ids);
  const into = new Map<string, string[]>(ids.map((id) => [id, []]));
  const out = new Map<string, string[]>(ids.map((id) => [id, []]));
  for (const { from, to } of edges) {
    if (!known.has(from) || !known.has(to) || from === to) continue;
    into.get(to)?.push(from);
    out.get(from)?.push(to);
  }
  const column = new Map<string, number>();
  const waiting = new Map(ids.map((id) => [id, into.get(id)?.length ?? 0]));
  const queue = ids.filter((id) => waiting.get(id) === 0);
  for (const id of queue) column.set(id, 0);
  for (let i = 0; i < queue.length; i++) {
    const id = queue[i] as string;
    for (const next of out.get(id) ?? []) {
      column.set(next, Math.max(column.get(next) ?? 0, (column.get(id) ?? 0) + 1));
      const left = (waiting.get(next) ?? 0) - 1;
      waiting.set(next, left);
      if (left === 0) queue.push(next);
    }
  }
  for (const id of ids) if (!column.has(id)) column.set(id, 0); // a cycle: never expected

  const columns: string[][] = [];
  for (const id of ids) (columns[column.get(id) as number] ??= []).push(id);
  const row = new Map<string, number>();
  const place = (list: string[]) => list.forEach((id, i) => row.set(id, i));
  for (const list of columns) place(list);
  const mean = (id: string, neighbours: Map<string, string[]>) => {
    const rows = (neighbours.get(id) ?? []).map((n) => row.get(n) as number);
    return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : (row.get(id) as number);
  };
  const sweep = (index: number, neighbours: Map<string, string[]>) => {
    const list = columns[index];
    if (!list) return;
    const key = new Map(list.map((id) => [id, mean(id, neighbours)]));
    list.sort((a, b) => (key.get(a) as number) - (key.get(b) as number));
    place(list);
  };
  for (let pass = 0; pass < 3; pass++) {
    for (let i = 1; i < columns.length; i++) sweep(i, into);
    for (let i = columns.length - 2; i >= 0; i--) sweep(i, out);
  }
  return { columns: columns.map((c) => c ?? []), column };
}

/** The node with all its ancestors (dependencies) and descendants (dependents). */
export function lineage(id: string, edges: GraphEdge[]): Set<string> {
  const walk = (start: string, step: (e: GraphEdge) => [string, string]) => {
    const seen = new Set([start]);
    const todo = [start];
    while (todo.length) {
      const at = todo.pop() as string;
      for (const e of edges) {
        const [here, there] = step(e);
        if (here === at && !seen.has(there)) {
          seen.add(there);
          todo.push(there);
        }
      }
    }
    return seen;
  };
  return new Set([...walk(id, (e) => [e.to, e.from]), ...walk(id, (e) => [e.from, e.to])]);
}
