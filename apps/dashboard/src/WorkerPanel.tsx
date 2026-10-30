import { createContext, type ReactNode, useContext } from "react";
import { type Agent, api, call } from "./api";
import { shortId } from "./format";
import { useLiveData } from "./live";

export type WorkerKind = "busy" | "idle" | "draining" | "quiet" | "ended" | "unknown";

export function workerKind(agent: Agent | undefined): WorkerKind {
  if (!agent) return "unknown";
  if (agent.state === "lost" || agent.state === "gone") return "ended";
  if (agent.state === "draining") return "draining";
  if (agent.quiet_since) return "quiet";
  return agent.claimed_tasks > 0 ? "busy" : "idle";
}

/** nanoclaw-socket-d6f8e4c0 → d6f8e4c0; otherwise keep a short label. */
export function workerShortName(name: string): string {
  const tail = name.split("-").pop();
  return tail && /^[a-f0-9]{6,}$/i.test(tail) ? tail.slice(0, 8) : name.slice(0, 14);
}

export function agentIdFromActor(actor: string): string | null {
  const [kind, id] = actor.split(":", 2);
  return kind === "agent" && id ? id : null;
}

type WorkerPanelContextValue = {
  openWorker: (agentId: string) => void;
  selectedId: string | null;
  agents: Agent[] | undefined;
};

const WorkerPanelContext = createContext<WorkerPanelContextValue | null>(null);

export function WorkerPanelProvider({
  selectedId,
  openWorker,
  children,
}: {
  selectedId: string | null;
  openWorker: (agentId: string) => void;
  children: ReactNode;
}) {
  const { data: agents } = useLiveData(
    (signal) => call(api.GET("/api/agents", { signal })),
    [],
  );
  return (
    <WorkerPanelContext.Provider value={{ openWorker, selectedId, agents }}>
      {children}
    </WorkerPanelContext.Provider>
  );
}

export function useWorkerPanel(): WorkerPanelContextValue {
  const ctx = useContext(WorkerPanelContext);
  if (!ctx) throw new Error("useWorkerPanel must be used within WorkerPanelProvider");
  return ctx;
}

export function workerLabel(agent: Agent | undefined, agentId: string): string {
  return agent ? workerShortName(agent.name) : shortId(agentId);
}
