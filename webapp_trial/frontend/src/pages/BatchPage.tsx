import { useEffect, useState } from "react";
import { getBatch } from "../api";
import type { BatchSummary, Job } from "../types";

interface Props {
  batchId: string;
  onBack: () => void;
  onOpen: (job: Job) => void;
}

export default function BatchPage({ batchId, onBack, onOpen }: Props) {
  const [batch, setBatch] = useState<BatchSummary | null>(null);

  useEffect(() => {
    let active = true;
    const tick = async () => {
      try {
        const data = await getBatch(batchId);
        if (active) setBatch(data);
      } catch {
        /* transient */
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [batchId]);

  if (!batch) return <p className="muted">Loading batch…</p>;

  const pending = batch.total - batch.completed - batch.failed;
  const pct = batch.total ? ((batch.completed + batch.failed) / batch.total) * 100 : 0;

  return (
    <div>
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← Back
        </button>
        <h2>Batch {batch.batch_id}</h2>
      </div>

      <div className="score-row">
        <div className="score-card score-info">
          <span className="score-label">Files</span>
          <span className="score-value">{batch.total}</span>
        </div>
        <div className="score-card score-good">
          <span className="score-label">Completed</span>
          <span className="score-value">{batch.completed}</span>
        </div>
        <div className={batch.failed ? "score-card score-bad" : "score-card score-info"}>
          <span className="score-label">Failed</span>
          <span className="score-value">{batch.failed}</span>
        </div>
        <div className="score-card score-warn">
          <span className="score-label">Pending</span>
          <span className="score-value">{pending}</span>
        </div>
      </div>

      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${pct}%` }} />
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
        {batch.jobs.map((job) => (
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
            <span className={`status-badge status-${job.status}`}>{job.status}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
