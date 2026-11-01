import { useEffect, useMemo, useState } from "react";
import {
  TaskChanges,
  TaskChangesStatus,
  TaskChangesTabCounts,
  useTaskChangesSnapshot,
} from "./TaskChanges";
import { TaskLogBarActions, TaskLogContent, type TaskLogSteer, useTaskLog } from "./TaskLog";
import { type TaskTerminalTab, taskTerminalVisibleTabs } from "./taskTerminalTabs";

export type { TaskTerminalTab };

const TABS: { id: TaskTerminalTab; label: string }[] = [
  { id: "all", label: "All" },
  { id: "worker", label: "Harness" },
  { id: "delegate", label: "Delegate" },
  { id: "changes", label: "Changes" },
];

export function TaskTerminal({
  taskId,
  attempt,
  steer,
  showChanges = true,
  changesAttempt,
  changesSelected,
}: {
  taskId: string;
  attempt: number;
  steer?: TaskLogSteer;
  showChanges?: boolean;
  changesAttempt?: number;
  changesSelected?: number;
}) {
  const [tab, setTab] = useState<TaskTerminalTab>("all");
  const log = useTaskLog(taskId, attempt);
  const changesSnap = useTaskChangesSnapshot({
    taskId,
    attempt: changesAttempt ?? attempt,
    selected: changesSelected,
  });

  useEffect(() => {
    setTab("all");
  }, [taskId, attempt]);

  const visibleTabIds = useMemo(
    () => taskTerminalVisibleTabs(log, showChanges, changesSnap.changes != null),
    [log.ready, log.lines, showChanges, changesSnap.changes],
  );
  const visibleTabs = TABS.filter((t) => visibleTabIds.includes(t.id));

  useEffect(() => {
    if (visibleTabIds.includes(tab)) return;
    setTab(visibleTabIds[0] ?? "all");
  }, [visibleTabIds, tab]);

  useEffect(() => {
    if (tab === "changes" && !showChanges) setTab("all");
  }, [showChanges, tab]);

  const onOutputTab = tab !== "changes";
  const filter = onOutputTab ? tab : "all";
  const showChangesPanel = showChanges && changesSnap.changes != null;

  return (
    <section
      className={`term task-terminal task-log${log.paused ? " term-paused" : ""}`}
      aria-label="Live"
    >
      <div className="term-bar">
        <span className="term-traffic" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        {visibleTabs.length > 0 && (
          <div className="term-tabs" role="tablist" aria-label="Output">
            {visibleTabs.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                role="tab"
                className={`term-tab${id === "changes" ? " term-tab-changes" : ""}`}
                aria-selected={tab === id}
                onClick={() => setTab(id)}
              >
                {id === "changes" ? <TaskChangesTabCounts snap={changesSnap} /> : label}
              </button>
            ))}
          </div>
        )}
        <div className="term-bar-end">
          {onOutputTab ? (
            <TaskLogBarActions
              log={log}
              steer={steer}
              hideFilter
              filter={filter}
              onFilterChange={() => {}}
            />
          ) : (
            <TaskChangesStatus snap={changesSnap} />
          )}
        </div>
      </div>
      <div className="term-stack">
        <div className="term-panel" hidden={!onOutputTab}>
          <TaskLogContent
            taskId={taskId}
            attempt={attempt}
            steer={steer}
            log={log}
            filter={filter}
          />
        </div>
        {showChangesPanel && (
          <div className="term-panel" hidden={onOutputTab}>
            <TaskChanges
              taskId={taskId}
              attempt={changesAttempt ?? attempt}
              embedded
              snap={changesSnap}
            />
          </div>
        )}
      </div>
    </section>
  );
}
