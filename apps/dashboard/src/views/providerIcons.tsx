// Provider marks and account-state icons as inline SVG (account usage spec 3.6).
// Simplified monochrome marks; a letter badge for custom providers.

const MARKS: Record<string, { label: string; path: string }> = {
  openai: {
    label: "OpenAI",
    // A six-petal knot, simplified.
    path: "M12 2.5a4.5 4.5 0 0 1 4.3 3.1 4.5 4.5 0 0 1 4.4 6.8 4.5 4.5 0 0 1-3 7.1 4.5 4.5 0 0 1-7.4 1.9 4.5 4.5 0 0 1-6.9-3.4 4.5 4.5 0 0 1-1.5-7.6A4.5 4.5 0 0 1 8 3.6a4.5 4.5 0 0 1 4-1.1Zm0 5.5-3.5 2v4L12 16l3.5-2v-4Z",
  },
  anthropic: {
    label: "Anthropic",
    // The "A" spark: two strokes and a bar.
    path: "M13.6 4h3.1L22 20h-3.1ZM7.3 4h3.2L15.8 20h-3.2l-1.1-3.4H6.1L5 20H1.9Zm-.3 9.9h3.5L8.8 8.4Z",
  },
  cursor: {
    label: "Cursor",
    // A cube seen from a corner.
    path: "M12 2 21 7v10l-9 5-9-5V7Zm0 2.3L5.2 8 12 11.8 18.8 8Zm-7 5.4v6.2l6 3.4v-6.3Zm14 0-6 3.3v6.3l6-3.4Z",
  },
  gitea: {
    label: "Gitea",
    // A cup with a branch.
    path: "M3 7h15a3 3 0 0 1 0 6h-1.2A6 6 0 0 1 11 18H9a6 6 0 0 1-6-6Zm14.8 2v2H18a1 1 0 0 0 0-2ZM9 9.5v5l4-2.5Z",
  },
};

export function ProviderMark(props: { provider: string }) {
  const mark = MARKS[props.provider];
  if (!mark) {
    return (
      <span className="provider-mark letter" role="img" aria-label={props.provider}>
        {props.provider.charAt(0).toUpperCase()}
      </span>
    );
  }
  return (
    <svg className="provider-mark" viewBox="0 0 24 24" role="img" aria-label={mark.label}>
      <path d={mark.path} fill="currentColor" fillRule="evenodd" />
    </svg>
  );
}

const STATE_PATHS: Record<string, string> = {
  // check, hourglass, slashed circle
  available: "M9.5 16.2 5.3 12l-1.4 1.4 5.6 5.6 11-11-1.4-1.4Z",
  exhausted: "M6 2h12v5l-4 5 4 5v5H6v-5l4-5-4-5Zm2 2v2.3L12 11l4-4.7V4Zm4 9-4 4.7V20h8v-2.3Z",
  disabled:
    "M12 2a10 10 0 1 1 0 20 10 10 0 0 1 0-20Zm0 2a8 8 0 0 0-6.3 12.9L16.9 5.7A8 8 0 0 0 12 4Zm6.3 3.1L7.1 18.3A8 8 0 0 0 18.3 7.1Z",
};

export function StateIcon(props: { state: string }) {
  const d = STATE_PATHS[props.state];
  if (!d) return null;
  return (
    <svg className={`state-icon account-${props.state}`} viewBox="0 0 24 24" aria-hidden="true">
      <path d={d} fill="currentColor" fillRule="evenodd" />
    </svg>
  );
}
