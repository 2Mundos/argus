import { useCallback, useEffect, useState } from "react";
import { handleUnauthorized, setToken, token } from "./api";
import { Brand } from "./Brand";
import { LiveProvider, useLive } from "./live";
import { Accounts } from "./views/Accounts";
import { AgentDetail } from "./views/AgentDetail";
import { Agents } from "./views/Agents";
import { Cost } from "./views/Cost";
import { Engines } from "./views/Engines";
import { Events } from "./views/Events";
import { Fleet } from "./views/Fleet";
import { Home } from "./views/Home";
import { Inbox } from "./views/Inbox";
import { Knowledge } from "./views/Knowledge";
import { Login } from "./views/Login";
import { Pools } from "./views/Pools";
import { System } from "./views/System";
import { Tasks } from "./views/Tasks";
import { WorkersSidebar } from "./WorkersSidebar";

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
  events: "Events",
  system: "System",
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
            {tab === "events" && <Events />}
            {tab === "system" && <System />}
          </>
        )}
      </main>
      <WorkersSidebar selectedId={selectedWorker} onSelect={setSelectedWorker} />
    </div>
  );
}
