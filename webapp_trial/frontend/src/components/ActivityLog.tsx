import { useEffect, useMemo, useRef, useState } from "react";
import type { StepLogEntry } from "../types";

const LEVEL_CLASS: Record<string, string> = {
  done: "af-ok",
  running: "af-info",
  info: "af-info",
  warning: "af-warn",
  error: "af-err",
};

function timeOf(entry: StepLogEntry): string {
  if (!entry.at) return "";
  const d = new Date(entry.at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
}

export default function ActivityLog({ steps }: { steps: StepLogEntry[] }) {
  const [filter, setFilter] = useState<"all" | "steps" | "warning">("all");
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const endRef = useRef<HTMLDivElement | null>(null);

  const visible = useMemo(() => {
    let rows = steps;
    if (filter === "steps") rows = rows.filter((s) => !s.detail);
    if (filter === "warning") {
      rows = rows.filter((s) => s.status === "warning" || s.status === "error");
    }
    const q = query.trim().toLowerCase();
    if (q) rows = rows.filter((s) => s.message.toLowerCase().includes(q));
    return rows;
  }, [steps, filter, query]);

  const counts = useMemo(
    () => ({
      all: steps.length,
      steps: steps.filter((s) => !s.detail).length,
      warning: steps.filter((s) => s.status === "warning" || s.status === "error").length,
    }),
    [steps]
  );

  useEffect(() => {
    if (follow) endRef.current?.scrollIntoView({ block: "nearest" });
  }, [visible.length, follow]);

  if (steps.length === 0) return <p className="muted">No activity yet.</p>;

  return (
    <>
      <div className="filter-row log-toolbar">
        {(["all", "steps", "warning"] as const).map((f) => (
          <button
            key={f}
            className={filter === f ? "chip active" : "chip"}
            onClick={() => setFilter(f)}
          >
            {f === "all" ? "All" : f === "steps" ? "Steps only" : "Warnings"} {counts[f]}
          </button>
        ))}
        <input
          className="log-search"
          placeholder="Filter messages…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label className="log-follow">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
          />
          Follow
        </label>
        <button
          className="link-button"
          onClick={() =>
            navigator.clipboard.writeText(
              steps.map((s) => `${timeOf(s)} [${s.step}] ${s.message}`).join("\n")
            )
          }
        >
          Copy
        </button>
      </div>

      <div className="activity-feed">
        {visible.map((entry, i) => (
          <div key={i} className={entry.detail ? "af-row af-detail" : "af-row"}>
            <span className={`af-dot ${LEVEL_CLASS[entry.status] ?? "af-info"}`} />
            <span className="af-time">{timeOf(entry)}</span>
            <span className="af-step">{entry.step}</span>
            <span className="af-msg">{entry.message}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </>
  );
}
