import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api, call, handleUnauthorized, type SystemSummary, setToken, token } from "./api";
import { Brand } from "./Brand";
import { LiveProvider, useLiveData } from "./live";
import { onboardingEvent, overviewEvent, systemEvent } from "./liveEvents";
import { verdict } from "./overview";
import { Icon, isCurrent, PrimaryNav, SIGN_OUT_ICON } from "./PrimaryNav";
import { hashPath, parseTaskFilter, type TaskFilter } from "./taskRoute";
import { confirmDiscard, subscribeHash } from "./unsaved";
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
const Projects = lazy(() => import("./views/Projects").then((m) => ({ default: m.Projects })));
const Routines = lazy(() => import("./views/Routines").then((m) => ({ default: m.Routines })));
const Settings = lazy(() => import("./views/Settings").then((m) => ({ default: m.Settings })));
const Tasks = lazy(() => import("./views/Tasks").then((m) => ({ default: m.Tasks })));

const TABS = {
  chat: "Chat",
  dashboard: "Dashboard",
  inbox: "Inbox",
  tasks: "Tasks",
  projects: "Projects",
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
  accounts: "Accounts",
  engines: "Engines",
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

export type Route =
  | { tab: "chat" | "dashboard" | "inbox" | "knowledge" | "projects" | "routines" }
  | { tab: "tasks"; taskId: string | null; filter: TaskFilter }
  | { tab: "workers"; sub: WorkersSub }
  | { tab: "setup"; sub: SetupSub };

function canonicalizeHash(): void {
  const raw = window.location.hash.slice(1);
  if (!raw) return;
  const at = raw.indexOf("?");
  const query = at < 0 ? "" : raw.slice(at);
  const parts = hashPath(raw).split("/");
  const head = parts[0] ?? "";
  const mapped = LEGACY[head];
  if (!mapped) return;
  const rest = parts.slice(1);
  const next = rest.length ? `${mapped}/${rest.join("/")}` : mapped;
  history.replaceState(null, "", `#${next}${query}`);
}

export function parseRoute(): Route {
  canonicalizeHash();
  const hash = window.location.hash;
  const parts = hashPath(hash).split("/").filter(Boolean);
  const head = parts[0] ?? "";
  const leaf = parts[1];
  if (head === "tasks") {
    return { tab: "tasks", taskId: leaf ?? null, filter: parseTaskFilter(hash) };
  }
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
    head === "projects" ||
    head === "routines" ||
    head === "dashboard" ||
    head === "chat"
  ) {
    return { tab: head };
  }
  // Empty/invalid: Shell rewrites it with `defaultHash`.
  return { tab: "dashboard" };
}

const ROUTE_HEADS = new Set(["tasks", "workers", "setup", ...Object.keys(TABS)]);

/** Where the app lands: Chat with the orchestrator on, the Dashboard without it (onboarding 3.4). */
export function homeHash(orchestrator: boolean): string {
  return orchestrator ? "#chat" : "#dashboard";
}

/** The hash to rewrite an empty or invalid one to, else null. */
export function defaultHash(hash: string, orchestrator: boolean): string | null {
  const head = hashPath(hash).split("/")[0] ?? "";
  if (ROUTE_HEADS.has(head) || head in LEGACY) return null;
  return homeHash(orchestrator);
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

export { isCurrent };

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
  const { data: overview } = useLiveData(
    (signal) => call(api.GET("/api/overview", { signal })),
    [],
    { accepts: overviewEvent },
  );
  const { data: system } = useLiveData((signal) => call(api.GET("/api/system", { signal })), [], {
    accepts: systemEvent,
  });
  const { data: onboarding, reload: reloadOnboarding } = useLiveData(
    (signal) => call(api.GET("/api/onboarding", { signal })),
    [],
    { accepts: onboardingEvent },
  );
  // Hash changes pass the unsaved-edits guard first (unsaved.ts).
  useEffect(
    () =>
      subscribeHash(() => {
        setRoute(parseRoute());
        setSelectedWorker(null);
      }),
    [],
  );
  // The worker panel replaces the page, so it asks before dropping unsaved edits too.
  const openWorker = useCallback((id: string | null) => {
    if (id === null || confirmDiscard()) setSelectedWorker(id);
  }, []);
  useEffect(() => {
    if (!onboarding) return;
    const next = defaultHash(window.location.hash, onboarding.orchestrator);
    if (next) window.location.hash = next;
  }, [onboarding]);

  const openDecisions = overview?.decisions.by_state.open;
  const awaiting = overview?.tasks.awaiting_approval;
  const openSetup = useCallback((step: 1 | 2 | 3 = 1) => setOnboardingStep(step), []);

  return (
    <WorkerPanelProvider selectedId={selectedWorker} openWorker={openWorker}>
      <div className={`shell${selectedWorker ? " shell-worker" : ""}`}>
        <aside className="nav-side">
          <Brand as="a" href={homeHash(onboarding?.orchestrator ?? false)} />
          <PrimaryNav route={route} awaiting={awaiting} openDecisions={openDecisions} />
          <ServerBox system={system} />
          <button type="button" className="nav-signout" onClick={onSignOut}>
            <Icon d={SIGN_OUT_ICON} />
            <span className="nav-text">Sign out</span>
          </button>
        </aside>
        <div className="workspace">
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
          status={onboarding}
          reload={reloadOnboarding}
          requestOpen={onboardingStep}
          onRequestHandled={() => setOnboardingStep(null)}
        />
      </div>
    </WorkerPanelProvider>
  );
}

/** Tabs for the pages of one menu entry; links, so the hash keeps back/forward working. */
function SubTabs({
  group,
  label,
  items,
  active,
}: {
  group: string;
  label: string;
  items: Record<string, string>;
  active: string;
}) {
  return (
    <nav className="subtabs" aria-label={label}>
      {Object.entries(items).map(([id, text]) => (
        <a key={id} href={`#${group}/${id}`} aria-current={id === active ? "page" : undefined}>
          {text}
        </a>
      ))}
    </nav>
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
      return <Tasks filter={route.filter} />;
    case "knowledge":
      return <Knowledge />;
    case "projects":
      return <Projects />;
    case "routines":
      return <Routines />;
    case "workers":
      return (
        <>
          <SubTabs group="workers" label="Workers" items={WORKERS_SUB} active={route.sub} />
          {route.sub === "pools" ? <Pools /> : route.sub === "fleet" ? <Fleet /> : <Agents />}
        </>
      );
    case "setup":
      return (
        <>
          <SubTabs group="setup" label="Setup" items={SETUP_SUB} active={route.sub} />
          {route.sub === "engines" ? (
            <Engines />
          ) : route.sub === "accounts" ? (
            <Accounts />
          ) : route.sub === "cost" ? (
            <Cost />
          ) : route.sub === "events" ? (
            <Events />
          ) : (
            <Settings />
          )}
        </>
      );
  }
}
