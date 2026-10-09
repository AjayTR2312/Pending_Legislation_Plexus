import { useState } from "react";
import { decideJob } from "../api";
import type { Job } from "../types";

interface Props {
  job: Job;
  onDecided: () => void;
}

/** Mirrors the Streamlit scanned-PDF gate: conversion waits for a human. */
export default function ApprovalGate({ job, onDecided }: Props) {
  const [busy, setBusy] = useState(false);

  async function decide(approve: boolean) {
    setBusy(true);
    try {
      await decideJob(job.id, approve);
      onDecided();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card gate-card">
      <h3>Scanned PDF — human review required</h3>
      <p>
        <strong>{job.filename}</strong> appears to be a scanned (image-based) PDF
        {job.page_count ? ` with ${job.page_count} page(s)` : ""}.
      </p>
      <p className="muted">
        Processing will send each page to the LLM as an image. Please confirm the file
        contains legislative text (not forms or maps), and that the higher token cost
        is acceptable.
      </p>
      <div className="approve-row">
        <button onClick={() => decide(true)} disabled={busy}>
          Approve &amp; Process
        </button>
        <button className="link-button" onClick={() => decide(false)} disabled={busy}>
          Skip this file
        </button>
      </div>
    </div>
  );
}
