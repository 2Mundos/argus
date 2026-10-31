import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api, call, handleUnauthorized, type SystemSummary, setToken, token } from "./api";
import { Brand } from "./Brand";
import { LiveProvider, useLive, useLiveData } from "./live";
import { verdict } from "./overview";
import { Home } from "./views/Home";
import { LiveStatusBar } from "./views/LiveStatusBar";
import { Login } from "./views/Login";
import { OnboardingHost } from "./views/Onboarding";
import { WorkerPanelProvider } from "./WorkerPanel";
import { WorkersSidebar } from "./WorkersSidebar";

const Accounts = lazy(() => import("./views/Accounts").then((m) => ({ default: m.Accounts })));
const AgentDetail = lazy(() =>
  import("./views/AgentDetail").then((m) => ({ default: m.AgentDetail })),
);
const Agents = lazy(() => import("./views/Agents").then((m) => ({ default: m.Agents })));
const Chat = lazy(() => import("./views/Chat").then((m) => ({ default: m.Chat })));
const Cost = lazy(() => import("./views/Cost").then((m) => ({ default: m.Cost })));
const Engines = lazy(() => import("./views/Engines").then((m) => ({ default: m.Engines })));
const Events = lazy(() => import("./views/Events").then((m) => ({ default: m.Events })));
const Fleet = lazy(() => import("./views/Fleet").then((m) => ({ default: m.Fleet })));
const Inbox = lazy(() => import("./views/Inbox").then((m) => ({ default: m.Inbox })));
const Knowledge = lazy(() => import("./views/Knowledge").then((m) => ({ default: m.Knowledge })));
const Pools = lazy(() => import("./views/Pools").then((m) => ({ default: m.Pools })));
const Routines = lazy(() => import("./views/Routines").then((m) => ({ default: m.Routines })));
const Settings = lazy(() => import("./views/Settings").then((m) => ({ default: m.Settings })));
const Tasks = lazy(() => import("./views/Tasks").then((m) => ({ default: m.Tasks })));

const TABS = {
  chat: "Chat",
  dashboard: "Dashboard",
  inbox: "Inbox",
  tasks: "Tasks",
  workers: "Workers",
  knowledge: "Knowledge",
  routines: "Routines",
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

/** Old top-level hashes → nested path under Workers / Setup, or the Dashboard rename. */
const LEGACY: Record<string, string> = {
  home: "dashboard",
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
  | { tab: "chat" | "dashboard" | "inbox" | "knowledge" | "routines" }
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

export function parseRoute(): Route {
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
  if (
    head === "inbox" ||
    head === "knowledge" ||
    head === "routines" ||
    head === "dashboard" ||
    head === "chat"
  ) {
    return { tab: head };
  }
  // Empty/invalid: Shell may rewrite to #chat when the orchestrator is on.
  return { tab: "dashboard" };
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

const PAGES: { href: string; label: string }[] = [
  { href: "#chat", label: "Chat" },
  { href: "#dashboard", label: "Dashboard" },
  { href: "#tasks", label: "Tasks" },
  { href: "#inbox", label: "Inbox" },
  { href: "#workers/agents", label: "Agents" },
  { href: "#workers/pools", label: "Pools" },
  { href: "#setup/accounts", label: "Accounts" },
  { href: "#knowledge", label: "Knowledge" },
  { href: "#routines", label: "Routines" },
  { href: "#workers/fleet", label: "Fleet" },
  { href: "#setup/engines", label: "Engines" },
  { href: "#setup/cost", label: "Cost" },
  { href: "#setup/events", label: "Events" },
  { href: "#setup/settings", label: "Settings" },
];

function PageSearch() {
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const hits = PAGES.filter((page) =>
    page.label.toLowerCase().includes(query.trim().toLowerCase()),
  );
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        input.current?.focus();
        setOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <form
      className="page-search"
      onSubmit={(event) => {
        event.preventDefault();
        const target = hits[0];
        if (!target) return;
        window.location.hash = target.href.slice(1);
        setQuery("");
        setOpen(false);
        input.current?.blur();
      }}
    >
      <input
        ref={input}
        type="search"
        placeholder="Search tasks, agents, inbox…"
        aria-label="Go to a page"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      />
      <kbd>Ctrl K</kbd>
      {open && query.trim() !== "" && (
        <ul className="page-hits" onMouseDown={(event) => event.preventDefault()}>
          {hits.length === 0 ? (
            <li className="muted small">No page</li>
          ) : (
            hits.map((page) => (
              <li key={page.href}>
                <a
                  href={page.href}
                  onClick={() => {
                    setQuery("");
                    setOpen(false);
                  }}
                >
                  {page.label}
                </a>
              </li>
            ))
          )}
        </ul>
      )}
    </form>
  );
}

function isCurrent(href: string, route: Route): boolean {
  if (href === "#chat") return route.tab === "chat";
  if (href === "#dashboard") return route.tab === "dashboard";
  if (href === "#tasks") return route.tab === "tasks";
  if (href === "#inbox") return route.tab === "inbox";
  if (href === "#knowledge") return route.tab === "knowledge";
  if (href === "#routines") return route.tab === "routines";
  if (href === "#workers/agents") return route.tab === "workers" && route.sub === "agents";
  if (href === "#workers/pools") return route.tab === "workers" && route.sub === "pools";
  if (href === "#workers/fleet") return route.tab === "workers" && route.sub === "fleet";
  if (href === "#setup/accounts") return route.tab === "setup" && route.sub === "accounts";
  if (href === "#setup/settings") return route.tab === "setup" && route.sub === "settings";
  if (href === "#setup/engines") return route.tab === "setup" && route.sub === "engines";
  if (href === "#setup/cost") return route.tab === "setup" && route.sub === "cost";
  if (href === "#setup/events") return route.tab === "setup" && route.sub === "events";
  return false;
}

function uptime(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function checkBadge(status: string): string {
  if (status === "ok") return "state-succeeded";
  if (status === "degraded") return "state-blocked";
  return "state-failed";
}

function ServerBox({ system }: { system: SystemSummary | undefined }) {
  const [open, setOpen] = useState(false);
  const tone = verdict(system);
  const label =
    tone === "ok"
      ? "Server ready"
      : tone === "degraded"
        ? "Server degraded"
        : tone === "failing"
          ? "Server failing"
          : "Server status unknown";
  const dot =
    tone === "ok"
      ? "worker-state-busy"
      : tone === "failing"
        ? "server-dot-fail"
        : tone === "degraded"
          ? "worker-state-draining"
          : "";
  const checks = system ? Object.entries(system.readiness.checks) : [];
  return (
    <section className={`server-box${open ? " is-open" : ""}`} aria-label="Server status">
      <header>
        <button
          type="button"
          aria-expanded={open}
          aria-controls="server-checks"
          onClick={() => setOpen((value) => !value)}
        >
          <span className={`worker-dot ${dot}`} />
          <strong>{label}</strong>
          <svg className="server-caret" viewBox="0 0 24 24" aria-hidden="true">
            <path
              d="M9 6l6 6-6 6"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </header>
      {open && (
        <div id="server-checks">
          {system === undefined ? (
            <p className="muted small">Loading…</p>
          ) : checks.length === 0 ? (
            <p className="muted small">No checks.</p>
          ) : (
            <ul>
              {checks.map(([name, check]) => (
                <li key={name}>
                  <span>{name}</span>
                  <span className={`badge ${checkBadge(check.status)}`}>{check.status}</span>
                  {check.detail ? <span className="muted small">{check.detail}</span> : null}
                </li>
              ))}
            </ul>
          )}
          {system !== undefined && (
            <p className="muted small">
              v{system.version} · up {uptime(system.uptime_s)}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const [route, setRoute] = useState<Route>(parseRoute);
  const [selectedWorker, setSelectedWorker] = useState<string | null>(null);
  const [onboardingStep, setOnboardingStep] = useState<1 | 2 | 3 | null>(null);
  const { status } = useLive();
  const { data: overview } = useLiveData(
    (signal) => call(api.GET("/api/overview", { signal })),
    [],
  );
  const { data: system } = useLiveData((signal) => call(api.GET("/api/system", { signal })), []);
  const { data: onboarding } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
  );
  useEffect(() => {
    const onHash = () => {
      setRoute(parseRoute());
      setSelectedWorker(null);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (!onboarding) return;
    const raw = window.location.hash.slice(1);
    if (raw) return;
    window.location.hash = onboarding.orchestrator ? "#chat" : "#dashboard";
  }, [onboarding]);

  const openDecisions = overview?.decisions.by_state.open;
  const awaiting = overview?.tasks.awaiting_approval;
  const openSetup = useCallback((step: 1 | 2 | 3 = 1) => setOnboardingStep(step), []);

  return (
    <WorkerPanelProvider selectedId={selectedWorker} openWorker={setSelectedWorker}>
      <div className={`shell${selectedWorker ? " shell-worker" : ""}`}>
        <aside className="nav-side">
          <Brand as="strong" />
          <nav aria-label="Primary">
            <NavLink href="#chat" current={isCurrent("#chat", route)} label="Chat" />
            <NavLink href="#dashboard" current={isCurrent("#dashboard", route)} label="Dashboard" />
            <NavLink
              href="#tasks"
              current={isCurrent("#tasks", route)}
              label="Tasks"
              count={awaiting}
            />
            <NavLink
              href="#inbox"
              current={isCurrent("#inbox", route)}
              label="Inbox"
              count={openDecisions}
            />
            <NavLink
              href="#workers/agents"
              current={isCurrent("#workers/agents", route)}
              label="Agents"
            />
            <NavLink
              href="#workers/pools"
              current={isCurrent("#workers/pools", route)}
              label="Pools"
            />
            <NavLink
              href="#setup/accounts"
              current={isCurrent("#setup/accounts", route)}
              label="Accounts"
            />
          </nav>
          <p className="nav-label">More</p>
          <nav aria-label="More">
            <NavLink href="#knowledge" current={isCurrent("#knowledge", route)} label="Knowledge" />
            <NavLink href="#routines" current={isCurrent("#routines", route)} label="Routines" />
            <NavLink
              href="#workers/fleet"
              current={isCurrent("#workers/fleet", route)}
              label="Fleet"
            />
            <NavLink
              href="#setup/engines"
              current={isCurrent("#setup/engines", route)}
              label="Engines"
            />
            <NavLink href="#setup/cost" current={isCurrent("#setup/cost", route)} label="Cost" />
            <NavLink
              href="#setup/events"
              current={isCurrent("#setup/events", route)}
              label="Events"
            />
            <NavLink
              href="#setup/settings"
              current={isCurrent("#setup/settings", route)}
              label="Settings"
            />
          </nav>
          <ServerBox system={system} />
        </aside>
        <div className="workspace">
          <header className="topbar">
            <PageSearch />
            <div className="topbar-end">
              <span className={`status status-${status}`} title={`Event stream: ${status}`}>
                {status === "live" ? "Live" : status}
              </span>
              <a className="bell" href="#inbox" aria-label="Inbox">
                <Icon d={iconFor("#inbox")} />
                {openDecisions !== undefined && openDecisions > 0 && (
                  <span className="nav-count">{openDecisions}</span>
                )}
              </a>
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
                <RouteView route={route} onOpenSetup={openSetup} />
              )}
            </Suspense>
          </main>
        </div>
        <WorkersSidebar selectedId={selectedWorker} />
        <LiveStatusBar />
        <OnboardingHost
          requestOpen={onboardingStep}
          onRequestHandled={() => setOnboardingStep(null)}
        />
      </div>
    </WorkerPanelProvider>
  );
}

const NAV_ICON: Record<string, string> = {
  "#chat": "M4 6h16v10a2 2 0 0 1-2 2H8l-4 4z",
  "#dashboard": "M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z",
  "#tasks": "M9 6h11M9 12h11M9 18h11M5 6h.01M5 12h.01M5 18h.01",
  "#inbox": "M4 5h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM4 5l8 6 8-6",
  "#workers/agents":
    "M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6M20 19v-1a3.5 3.5 0 0 0-2.5-3.3M16.5 5.2a3 3 0 0 1 0 5.6",
  "#workers/pools": "M12 3 4 7l8 4 8-4zM4 12l8 4 8-4M4 17l8 4 8-4",
  "#setup/accounts": "M15 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0M5 19a6 6 0 0 1 14 0",
  "#knowledge": "M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 0-3 3zM5 4v16",
  "#workers/fleet": "M4 5h7v7H4zM13 5h7v7h-7zM4 14h7v5H4zM13 14h7v5h-7z",
  "#setup/engines": "M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3M7 7h10v10H7z",
  "#setup/cost": "M4 19V5M4 19h16M8 16v-5M12 16V8M16 16v-3",
  "#setup/events": "M4 6h16M4 12h10M4 18h13",
  "#setup/settings": "M4 7h16M4 12h16M4 17h16M8 7v.01M14 12v.01M10 17v.01",
};

function iconFor(href: string): string {
  return NAV_ICON[href] ?? "M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z";
}

function Icon({ d }: { d: string }) {
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function NavLink({
  href,
  label,
  current,
  count,
}: {
  href: string;
  label: string;
  current: boolean;
  count?: number;
}) {
  return (
    <a href={href} aria-current={current ? "page" : undefined}>
      <Icon d={iconFor(href)} />
      <span>{label}</span>
      {count !== undefined && count > 0 && <span className="nav-count">{count}</span>}
    </a>
  );
}

function RouteView({
  route,
  onOpenSetup,
}: {
  route: Route;
  onOpenSetup: (step?: 1 | 2 | 3) => void;
}) {
  switch (route.tab) {
    case "chat":
      return <Chat onOpenSetup={onOpenSetup} />;
    case "dashboard":
      return <Home onOpenSetup={() => onOpenSetup(1)} />;
    case "inbox":
      return <Inbox />;
    case "tasks":
      return <Tasks />;
    case "knowledge":
      return <Knowledge />;
    case "routines":
      return <Routines />;
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
