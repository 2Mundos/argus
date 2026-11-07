import type { Route } from "./App";
import { hashPath } from "./taskRoute";

/** A menu entry is current on its page, or on any tab of its group. */
export function isCurrent(href: string, route: Route): boolean {
  return route.tab === hashPath(href).split("/")[0];
}

const NAV_ICON: Record<string, string> = {
  "#chat": "M4 6h16v10a2 2 0 0 1-2 2H8l-4 4z",
  "#dashboard": "M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z",
  "#tasks": "M9 6h11M9 12h11M9 18h11M5 6h.01M5 12h.01M5 18h.01",
  "#inbox": "M4 5h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM4 5l8 6 8-6",
  "#projects": "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  "#knowledge": "M5 4h10a3 3 0 0 1 3 3v13H8a3 3 0 0 0-3 3zM5 4v16",
  "#routines": "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  "#workers":
    "M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6M20 19v-1a3.5 3.5 0 0 0-2.5-3.3M16.5 5.2a3 3 0 0 1 0 5.6",
  "#setup": "M4 7h16M4 12h16M4 17h16M8 7v.01M14 12v.01M10 17v.01",
};

export const SIGN_OUT_ICON = "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9";

function iconFor(href: string): string {
  return NAV_ICON[href] ?? "M4 10.5 12 4l8 6.5V20a1 1 0 0 1-1 1h-5v-6H10v6H5a1 1 0 0 1-1-1z";
}

export function Icon({ d }: { d: string }) {
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
      <span className="nav-text">{label}</span>
      {count !== undefined && count > 0 && <span className="nav-count">{count}</span>}
    </a>
  );
}

export function PrimaryNav({
  route,
  awaiting,
  openDecisions,
}: {
  route: Route;
  awaiting?: number;
  openDecisions?: number;
}) {
  return (
    <nav aria-label="Primary">
      <NavLink href="#chat" current={isCurrent("#chat", route)} label="Chat" />
      <NavLink href="#dashboard" current={isCurrent("#dashboard", route)} label="Dashboard" />
      <NavLink href="#tasks" current={isCurrent("#tasks", route)} label="Tasks" count={awaiting} />
      <NavLink
        href="#inbox"
        current={isCurrent("#inbox", route)}
        label="Inbox"
        count={openDecisions}
      />
      <NavLink href="#projects" current={isCurrent("#projects", route)} label="Projects" />
      <NavLink href="#knowledge" current={isCurrent("#knowledge", route)} label="Knowledge" />
      <NavLink href="#routines" current={isCurrent("#routines", route)} label="Routines" />
      <NavLink href="#workers" current={isCurrent("#workers", route)} label="Workers" />
      <NavLink href="#setup" current={isCurrent("#setup", route)} label="Setup" />
    </nav>
  );
}
