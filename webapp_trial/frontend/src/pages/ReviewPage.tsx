import { useEffect, useState } from "react";
import { approveJob, getJob, getJobText, jobPdfUrl } from "../api";
import ProgressSteps from "../components/ProgressSteps";
import PdfViewer from "../components/PdfViewer";
import type { Job } from "../types";

export default function ReviewPage({ job: initialJob, onBack }: { job: Job; onBack: () => void }) {
  const [job, setJob] = useState<Job>(initialJob);
  const [text, setText] = useState<string | null>(null);
  const [approveMsg, setApproveMsg] = useState<string | null>(null);

  useEffect(() => {
    if (job.status === "done" || job.status === "error") return;
    const interval = setInterval(async () => {
      const updated = await getJob(job.id);
      setJob(updated);
    }, 1200);
    return () => clearInterval(interval);
  }, [job.id, job.status]);

  useEffect(() => {
    if (job.status === "done" && job.has_output && text === null) {
      getJobText(job.id).then(setText).catch((err) => setText(`[Failed to load output: ${err}]`));
    }
  }, [job.status, job.has_output, job.id, text]);

  async function handleApprove() {
    const result = await approveJob(job.id);
    setApproveMsg(`Saved locally to ${result.saved_to} (trial only — not uploaded to SFTP)`);
    setJob((j) => ({ ...j, approved: true }));
  }

  return (
    <div className="review-page">
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← New upload
        </button>
        <h2>{job.filename}</h2>
        <span className="pill">{job.state_code ?? "detecting…"}</span>
      </div>

      {job.status !== "done" && (
        <div className="card">
          <ProgressSteps steps={job.steps_log} />
        </div>
      )}

      {job.status === "error" && <p className="error">Conversion failed: {job.error}</p>}

      {job.status === "done" && (
        <div className="compare-grid">
          <div className="compare-pane">
            <h3>Source PDF</h3>
            <PdfViewer src={jobPdfUrl(job.id)} />
          </div>
          <div className="compare-pane">
            <h3>Converted text</h3>
            <pre className="output-text">{text ?? "Loading…"}</pre>
            <div className="approve-row">
              <button onClick={handleApprove} disabled={job.approved}>
                {job.approved ? "Approved" : "Approve"}
              </button>
              {approveMsg && <span className="muted">{approveMsg}</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
