import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { handleUnauthorized, setToken, token } from "./api";
import { Brand } from "./Brand";
import { LiveProvider, useLive } from "./live";
import { Home } from "./views/Home";
import { LiveStatusBar } from "./views/LiveStatusBar";
import { Login } from "./views/Login";
import { WorkersSidebar } from "./WorkersSidebar";

const Accounts = lazy(() => import("./views/Accounts").then((m) => ({ default: m.Accounts })));
const AgentDetail = lazy(() =>
  import("./views/AgentDetail").then((m) => ({ default: m.AgentDetail })),
);
const Agents = lazy(() => import("./views/Agents").then((m) => ({ default: m.Agents })));
const Cost = lazy(() => import("./views/Cost").then((m) => ({ default: m.Cost })));
const Engines = lazy(() => import("./views/Engines").then((m) => ({ default: m.Engines })));
const Events = lazy(() => import("./views/Events").then((m) => ({ default: m.Events })));
const Fleet = lazy(() => import("./views/Fleet").then((m) => ({ default: m.Fleet })));
const Inbox = lazy(() => import("./views/Inbox").then((m) => ({ default: m.Inbox })));
const Knowledge = lazy(() => import("./views/Knowledge").then((m) => ({ default: m.Knowledge })));
const Pools = lazy(() => import("./views/Pools").then((m) => ({ default: m.Pools })));
const Settings = lazy(() => import("./views/Settings").then((m) => ({ default: m.Settings })));
const Tasks = lazy(() => import("./views/Tasks").then((m) => ({ default: m.Tasks })));

const TABS = {
  home: "Home",
  inbox: "Inbox",
  tasks: "Tasks",
  workers: "Workers",
  knowledge: "Knowledge",
  setup: "Setup",
} as const;
type Tab = keyof typeof TABS;

const WORKERS_SUB = {
  agents: "Agents",
  pools: "Pools",
  fleet: "Fleet",
} as const;
type WorkersSub = keyof typeof WORKERS_SUB;

const SETUP_SUB = {
  settings: "Settings",
  engines: "Engines",
  accounts: "Accounts",
  cost: "Cost",
  events: "Events",
} as const;
type SetupSub = keyof typeof SETUP_SUB;

/** Old top-level hashes → nested path under Workers / Setup. */
const LEGACY: Record<string, string> = {
  frota: "workers/fleet",
  agents: "workers/agents",
  pools: "workers/pools",
  fleet: "workers/fleet",
  settings: "setup/settings",
  engines: "setup/engines",
  accounts: "setup/accounts",
  cost: "setup/cost",
  events: "setup/events",
};

type Route =
  | { tab: "home" | "inbox" | "knowledge" }
  | { tab: "tasks"; taskId: string | null }
  | { tab: "workers"; sub: WorkersSub }
  | { tab: "setup"; sub: SetupSub };

function canonicalizeHash(): void {
  const raw = window.location.hash.slice(1);
  if (!raw) return;
  const parts = raw.split("/");
  const head = parts[0] ?? "";
  const mapped = LEGACY[head];
  if (!mapped) return;
  const rest = parts.slice(1);
  const next = rest.length ? `${mapped}/${rest.join("/")}` : mapped;
  history.replaceState(null, "", `#${next}`);
}

function parseRoute(): Route {
  canonicalizeHash();
  const parts = window.location.hash.slice(1).split("/").filter(Boolean);
  const head = parts[0] ?? "";
  const leaf = parts[1];
  if (head === "tasks") return { tab: "tasks", taskId: leaf ?? null };
  if (head === "workers") {
    const sub = leaf && leaf in WORKERS_SUB ? (leaf as WorkersSub) : "agents";
    return { tab: "workers", sub };
  }
  if (head === "setup") {
    const sub = leaf && leaf in SETUP_SUB ? (leaf as SetupSub) : "settings";
    return { tab: "setup", sub };
  }
  if (head === "inbox" || head === "knowledge" || head === "home") return { tab: head };
  return { tab: "home" };
}

export function App() {
  const [signedIn, setSignedIn] = useState(() => token() !== null);
  const signOut = useCallback(() => {
    setToken(null);
    setSignedIn(false);
  }, []);
  useEffect(() => handleUnauthorized(signOut), [signOut]);

  if (!signedIn) return <Login onLogin={() => setSignedIn(true)} />;
  return (
    <LiveProvider token={token} onUnauthorized={signOut}>
      <Shell onSignOut={signOut} />
    </LiveProvider>
  );
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const [route, setRoute] = useState<Route>(parseRoute);
  const [selectedWorker, setSelectedWorker] = useState<string | null>(null);
  const { status } = useLive();
  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute());
      setSelectedWorker(null);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const tab = route.tab;

  return (
    <div className={`shell${selectedWorker ? " shell-worker" : ""}`}>
      <header>
        <Brand as="strong" />
        <nav aria-label="Primary">
          {(Object.keys(TABS) as Tab[]).map((key) => (
            <a
              key={key}
              href={
                key === "workers"
                  ? "#workers/agents"
                  : key === "setup"
                    ? "#setup/settings"
                    : `#${key}`
              }
              aria-current={tab === key ? "page" : undefined}
            >
              {TABS[key]}
            </a>
          ))}
        </nav>
        <div className="header-end">
          <span className={`status status-${status}`} title={`Event stream: ${status}`}>
            {status}
          </span>
          <button type="button" className="link" onClick={onSignOut}>
            Sign out
          </button>
        </div>
      </header>
      <main>
        {!selectedWorker && route.tab === "workers" && (
          <SubNav label="Workers" base="workers" items={WORKERS_SUB} current={route.sub} />
        )}
        {!selectedWorker && route.tab === "setup" && (
          <SubNav label="Setup" base="setup" items={SETUP_SUB} current={route.sub} />
        )}
        <Suspense fallback={<p className="muted">Loading…</p>}>
          {selectedWorker ? (
            <AgentDetail
              key={selectedWorker}
              agentId={selectedWorker}
              onClose={() => setSelectedWorker(null)}
              variant="panel"
            />
          ) : (
            <RouteView route={route} />
          )}
        </Suspense>
      </main>
      <WorkersSidebar selectedId={selectedWorker} onSelect={setSelectedWorker} />
      <LiveStatusBar />
    </div>
  );
}

function SubNav<K extends string>({
  label,
  base,
  items,
  current,
}: {
  label: string;
  base: string;
  items: Record<K, string>;
  current: K;
}) {
  return (
    <nav className="subnav" aria-label={label}>
      {(Object.keys(items) as K[]).map((key) => (
        <a key={key} href={`#${base}/${key}`} aria-current={current === key ? "page" : undefined}>
          {items[key]}
        </a>
      ))}
    </nav>
  );
}

function RouteView({ route }: { route: Route }) {
  switch (route.tab) {
    case "home":
      return <Home />;
    case "inbox":
      return <Inbox />;
    case "tasks":
      return <Tasks />;
    case "knowledge":
      return <Knowledge />;
    case "workers":
      if (route.sub === "pools") return <Pools />;
      if (route.sub === "fleet") return <Fleet />;
      return <Agents />;
    case "setup":
      if (route.sub === "engines") return <Engines />;
      if (route.sub === "accounts") return <Accounts />;
      if (route.sub === "cost") return <Cost />;
      if (route.sub === "events") return <Events />;
      return <Settings />;
  }
}
