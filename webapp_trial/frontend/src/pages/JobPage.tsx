import { useEffect, useState } from "react";
import { artifactUrl, getJob, jobPdfUrl, jobTextUrl, listArtifacts } from "../api";
import ActivityLog from "../components/ActivityLog";
import ApprovalGate from "../components/ApprovalGate";
import ReviewWorkspace from "../components/ReviewWorkspace";
import type { Artifact, Job } from "../types";

interface Props {
  job: Job;
  onBack: () => void;
}

const TERMINAL = new Set(["done", "error", "skipped"]);

export default function JobPage({ job: initial, onBack }: Props) {
  const [job, setJob] = useState<Job>(initial);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  // null = still asking the server; string = the request failed
  const [artifactsState, setArtifactsState] = useState<"loading" | "loaded" | string>("loading");

  useEffect(() => {
    if (TERMINAL.has(job.status)) return;
    const id = setInterval(async () => {
      try {
        setJob(await getJob(job.id));
      } catch {
        /* keep polling through transient errors */
      }
    }, 1500);
    return () => clearInterval(id);
  }, [job.id, job.status]);

  useEffect(() => {
    if (job.status === "done" && job.has_output) {
      setArtifactsState("loading");
      listArtifacts(job.id)
        .then((list) => {
          setArtifacts(list);
          setArtifactsState("loaded");
        })
        .catch((err) => {
          setArtifacts([]);
          setArtifactsState(String(err?.message ?? err ?? "request failed"));
        });
    } else if (job.status === "done") {
      setArtifactsState("loaded");
    }
  }, [job.status, job.has_output, job.id]);

  const refresh = async () => setJob(await getJob(job.id));

  // A finished job whose converted file is gone has nothing to show: return to the list quietly.
  const missingOutput = job.status === "done" && artifactsState !== "loading" &&
    !artifacts.some((a) => a.name === `${job.filename.replace(/\.pdf$/i, "")}.txt`);
  useEffect(() => {
    if (missingOutput) onBack();
  }, [missingOutput]); // eslint-disable-line react-hooks/exhaustive-deps

  // Match the real output by stem — a loose ".txt" match can pick up the
  // _hybrid_raw / _hybrid_precleaned diagnostics written alongside it.
  const stem = job.filename.replace(/\.pdf$/i, "");
  const outputArtifact = artifacts.find((a) => a.name === `${stem}.txt`);
  const pdfArtifact = artifacts.find((a) => a.name === job.filename);

  // Finished jobs open into the same review workspace as pipeline sessions, so
  // Save / Approve / Submit behave identically wherever the file came from.
  if (job.status === "done") {
    if (!outputArtifact) return <div aria-busy="true"><div className="skeleton" style={{ width: "40%" }} /><div className="skeleton" /><div className="skeleton" style={{ width: "75%" }} /></div>;
    return (
      <ReviewWorkspace
        job={job}
        onBack={onBack}
        target={{
          title: job.filename,
          stateCode: job.state_code,
          textUrl: jobTextUrl(job.id),
          pdfUrl: jobPdfUrl(job.id),
          textPath: outputArtifact.path,
          pdfPath: pdfArtifact?.path ?? null,
          jobId: job.id,
        }}
        extra={
          artifacts.length > 0 && (
            <div className="card">
              <strong>Artifacts</strong>
              <div className="artifact-list">
                {artifacts.map((a) => (
                  <a key={a.name} href={artifactUrl(job.id, a.name)} download>
                    {a.name}
                    <span className="muted"> ({(a.size_bytes / 1024).toFixed(0)} KB)</span>
                  </a>
                ))}
              </div>
            </div>
          )
        }
      />
    );
  }

  return (
    <div className="review-page">
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← Back
        </button>
        <h2>{job.filename}</h2>
        {job.state_code && <span className="pill">{job.state_code}</span>}
        {job.display_type && <span className="pill pill-type">{job.display_type}</span>}
        {job.page_count != null && <span className="muted">{job.page_count} pages</span>}
        <span className={`status-badge status-${job.status}`}>{job.status}</span>
      </div>

      {job.mode_reason && <p className="muted">Routing: {job.mode_reason}</p>}

      {job.status === "awaiting_approval" && <ApprovalGate job={job} onDecided={refresh} />}

      {job.status === "error" && <p className="error">Conversion failed: {job.error}</p>}

      <div className="card">
        <strong>Activity</strong>
        <ActivityLog steps={job.steps_log} />
      </div>
    </div>
  );
}
