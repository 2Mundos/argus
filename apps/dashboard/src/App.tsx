import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { handleUnauthorized, setToken, token } from "./api";
import { Brand } from "./Brand";
import { LiveProvider, useLive } from "./live";
import { Home } from "./views/Home";
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
  tasks: "Tasks",
  agents: "Agents",
  pools: "Pools",
  fleet: "Fleet",
  inbox: "Inbox",
  knowledge: "Knowledge",
  accounts: "Accounts",
  cost: "Cost",
  engines: "Engines",
  settings: "Settings",
  events: "Events",
} as const;
type Tab = keyof typeof TABS;

function currentTab(): Tab {
  const hash = window.location.hash.slice(1).split("/")[0] ?? "";
  // `#frota` was the old name of the Fleet tab.
  if (hash === "frota") return "fleet";
  return hash in TABS ? (hash as Tab) : "home";
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
  const [tab, setTab] = useState<Tab>(currentTab);
  const [selectedWorker, setSelectedWorker] = useState<string | null>(null);
  const { status } = useLive();
  useEffect(() => {
    const onHash = () => {
      setTab(currentTab());
      setSelectedWorker(null);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className={`shell${selectedWorker ? " shell-worker" : ""}`}>
      <header>
        <Brand as="strong" />
        <nav>
          {(Object.keys(TABS) as Tab[]).map((key) => (
            <a key={key} href={`#${key}`} aria-current={tab === key ? "page" : undefined}>
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
        <Suspense fallback={<p className="muted">Loading…</p>}>
          {selectedWorker ? (
            <AgentDetail
              key={selectedWorker}
              agentId={selectedWorker}
              onClose={() => setSelectedWorker(null)}
              variant="panel"
            />
          ) : (
            <>
              {tab === "home" && <Home />}
              {tab === "tasks" && <Tasks />}
              {tab === "agents" && <Agents />}
              {tab === "pools" && <Pools />}
              {tab === "fleet" && <Fleet />}
              {tab === "inbox" && <Inbox />}
              {tab === "knowledge" && <Knowledge />}
              {tab === "accounts" && <Accounts />}
              {tab === "cost" && <Cost />}
              {tab === "engines" && <Engines />}
              {tab === "settings" && <Settings />}
              {tab === "events" && <Events />}
            </>
          )}
        </Suspense>
      </main>
      <WorkersSidebar selectedId={selectedWorker} onSelect={setSelectedWorker} />
    </div>
  );
}
