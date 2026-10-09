import { useState } from "react";
import Icon from "../components/Icon";
import { useJobStream } from "../hooks/useJobStream";
import type { Job } from "../types";

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  processing: "Processing",
  done: "Done",
  error: "Failed",
  awaiting_approval: "Needs approval",
  skipped: "Skipped",
};

interface Props {
  onOpen: (job: Job) => void;
}

/** Job history with queue counts — the view Streamlit never had. */
export default function JobsDashboard({ onOpen }: Props) {
  const { jobs, live } = useJobStream();
  const [filter, setFilter] = useState<string>("all");

  if (jobs === null) return <div aria-busy="true"><div className="skeleton" style={{ width: "30%" }} /><div className="skeleton" /><div className="skeleton" /><div className="skeleton" style={{ width: "60%" }} /></div>;
  if (jobs.length === 0) return <div className="empty-state"><Icon name="file" />No jobs yet. Upload a PDF to begin.</div>;

  const counts = jobs.reduce<Record<string, number>>((acc, j) => {
    acc[j.status] = (acc[j.status] ?? 0) + 1;
    return acc;
  }, {});
  const shown = filter === "all" ? jobs : jobs.filter((j) => j.status === filter);

  return (
    <div>
      <p className="muted tab-help">
        <b>Jobs</b> are PDFs uploaded in this app. This app converts and validates them. Pipeline Sessions are files the SFTP pipeline already converted.
      </p>
      <div className="filter-row">
        <span className={live ? "live-dot live" : "live-dot"} title={live ? "Live stream" : "Polling fallback"} />
        <button
          className={filter === "all" ? "chip active" : "chip"}
          onClick={() => setFilter("all")}
        >
          All {jobs.length}
        </button>
        {Object.entries(counts).map(([status, n]) => (
          <button
            key={status}
            className={filter === status ? "chip active" : "chip"}
            onClick={() => setFilter(status)}
          >
            {STATUS_LABEL[status] ?? status} {n}
          </button>
        ))}
      </div>

      <div className="jobs-table">
        <div className="jobs-head">
          <span>File</span>
          <span>State</span>
          <span>Type</span>
          <span>Mode</span>
          <span>Accuracy</span>
          <span>Status</span>
        </div>
        {shown.map((job) => (
          <button key={job.id} className="jobs-row" onClick={() => onOpen(job)}>
            <span className="jobs-file" title={job.filename}>
              {job.filename}
            </span>
            <span>{job.state_code ?? "—"}</span>
            <span>{job.display_type ?? "—"}</span>
            <span className="jobs-mode">{job.mode ?? "—"}</span>
            <span>
              {job.metrics?.content_accuracy_pct != null
                ? `${job.metrics.content_accuracy_pct.toFixed(1)}%`
                : "—"}
            </span>
            <span className={`status-badge status-${job.status}`}>
              {STATUS_LABEL[job.status] ?? job.status}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
