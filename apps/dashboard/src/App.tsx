import { useCallback, useEffect, useState } from "react";
import { handleUnauthorized, setToken, token } from "./api";
import { LiveProvider, useLive } from "./live";
import { Accounts } from "./views/Accounts";
import { Agents } from "./views/Agents";
import { Engines } from "./views/Engines";
import { Events } from "./views/Events";
import { Home } from "./views/Home";
import { Inbox } from "./views/Inbox";
import { Knowledge } from "./views/Knowledge";
import { Login } from "./views/Login";
import { Pools } from "./views/Pools";
import { System } from "./views/System";
import { Tasks } from "./views/Tasks";

const TABS = {
  home: "Home",
  tasks: "Tasks",
  agents: "Agents",
  pools: "Pools",
  inbox: "Inbox",
  knowledge: "Knowledge",
  accounts: "Accounts",
  engines: "Engines",
  events: "Events",
  system: "System",
} as const;
type Tab = keyof typeof TABS;

function currentTab(): Tab {
  const hash = window.location.hash.slice(1);
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
  const { status } = useLive();
  useEffect(() => {
    const onHash = () => setTab(currentTab());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="shell">
      <header>
        <strong className="brand">Argus</strong>
        <nav>
          {(Object.keys(TABS) as Tab[]).map((key) => (
            <a key={key} href={`#${key}`} aria-current={tab === key ? "page" : undefined}>
              {TABS[key]}
            </a>
          ))}
        </nav>
        <span className={`status status-${status}`} title={`Event stream: ${status}`}>
          {status}
        </span>
        <button type="button" className="link" onClick={onSignOut}>
          Sign out
        </button>
      </header>
      <main>
        {tab === "home" && <Home />}
        {tab === "tasks" && <Tasks />}
        {tab === "agents" && <Agents />}
        {tab === "pools" && <Pools />}
        {tab === "inbox" && <Inbox />}
        {tab === "knowledge" && <Knowledge />}
        {tab === "accounts" && <Accounts />}
        {tab === "engines" && <Engines />}
        {tab === "events" && <Events />}
        {tab === "system" && <System />}
      </main>
    </div>
  );
}
