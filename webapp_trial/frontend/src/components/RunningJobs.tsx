import { useEffect, useState } from "react";
import { listJobs } from "../api";
import type { Job } from "../types";

const ACTIVE = new Set(["queued", "processing", "awaiting_approval"]);

const LABEL: Record<string, string> = {
  queued: "waiting for a worker",
  processing: "converting",
  awaiting_approval: "needs your decision",
};

/** Conversion keeps running on the server when you switch tabs; this makes that visible everywhere. */
export default function RunningJobs({ onOpen }: { onOpen: (job: Job) => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const all = await listJobs();
        if (alive) setJobs(all.filter((j) => ACTIVE.has(j.status)));
      } catch {
        /* the next tick will try again */
      }
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (jobs.length === 0) return null;
  return (
    <div className="running-jobs" role="status">
      <strong>{jobs.length} in progress</strong>
      {jobs.slice(0, 4).map((j) => (
        <button key={j.id} className="running-chip" onClick={() => onOpen(j)} title="Open this job">
          <span className="running-dot" />
          {j.filename.length > 38 ? `${j.filename.slice(0, 36)}…` : j.filename}
          <span className="muted"> · {j.current_step && j.status === "processing" ? j.current_step : LABEL[j.status]}</span>
        </button>
      ))}
      {jobs.length > 4 && <span className="muted">+{jobs.length - 4} more in Jobs</span>}
    </div>
  );
}
