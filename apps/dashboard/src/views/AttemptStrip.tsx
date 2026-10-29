/** The shared attempt selection for Live and Changes (task changes spec 3.7). */
export function AttemptStrip({
  attempts,
  current,
  selected,
  onSelect,
}: {
  /** Attempt numbers from `GET /api/tasks/{id}/attempts`, oldest first. */
  attempts: number[];
  current: number;
  selected: number;
  onSelect: (attempt: number) => void;
}) {
  if (attempts.length < 2) return null;
  return (
    <div className="attempt-strip" role="group" aria-label="Attempts">
      {attempts.map((a) => (
        <button
          key={a}
          type="button"
          className="term-action"
          aria-pressed={a === selected}
          onClick={() => onSelect(a)}
        >
          {a === current ? `Attempt ${a} (current)` : `Attempt ${a}`}
        </button>
      ))}
    </div>
  );
}
