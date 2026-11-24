---
status: implemented
created: 2026-11-24
updated: 2026-11-24
---

# Dashboard PWA

# 1. Purpose and Scope

The dashboard is installable as a Progressive Web App. On a mobile browser it offers
to add Argus to the home screen; once installed it opens standalone.

# 2. Non-Goals

- No offline data. Tasks, agents and fleet health are live; offline the dashboard
  shows only a notice.
- No push notifications, background sync or new dependency.

# 3. Decisions Made

- **Manifest.** `public/site.webmanifest` (served as `application/manifest+json`)
  declares name, short name, `start_url`, `display: standalone`, theme and
  background colours (the dark `--bg`), and 192px, 512px and maskable 512px icons
  rendered from `assets/logo.svg`. `index.html` links it.
- **Service worker.** `public/sw.js`, served from the root scope and revalidated on
  every load. It caches only the static shell: `/offline.html`, the manifest, the
  icons and content-hashed `/assets/*`. It never handles non-GET, cross-origin,
  `/api/`, `/healthz`, `/auth`, `/login` or `/oauth` requests. Navigations always go
  to the network; when it fails the cached `/offline.html` notice is shown.
  Registered in production builds only.
- **Install banner.** `InstallPrompt` shows on mobile user agents that are not
  standalone (`display-mode: standalone` or iOS `navigator.standalone`). On
  Android/Chromium it captures `beforeinstallprompt` and the banner's Install
  button triggers it. On iOS Safari it shows "Tap Share, then Add to Home Screen".
  Other browsers show nothing.
- **Dismissal.** "Not now" stores a timestamp in `localStorage` and hides the banner
  for 14 days. An accepted install or `appinstalled` hides it permanently.

# 4. Open Questions

None.

# 5. Constraints

Dashboard static files only; no API, schema or wire change. All engines behave the
same. English UI strings.

# 6. Implementation Consequences

`apps/dashboard/public/` (`site.webmanifest`, `sw.js`, `offline.html`, `pwa-*.png`);
`src/pwaInstall.ts` (detection, dismissal, registration); `src/InstallPrompt.tsx`
mounted in `main.tsx`; `.install-banner` styles.

## Verification boundary

- Commands: `pnpm --filter @argus/dashboard test`, `pnpm lint`, `pnpm typecheck`,
  `uv run pytest apps/api/tests/test_dashboard_files.py`.
- Verified in a browser: manifest and worker fetched from the served build with
  HTTP status, Lighthouse installability.
- Not verified: the iOS Share sheet and a real Android install flow.
