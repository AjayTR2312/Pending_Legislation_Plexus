import { FormEvent, useState } from "react";
import { uploadBatch, uploadPdf } from "../api";
import type { Job } from "../types";

const STATE_CODES = [
  "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID", "IL",
  "IN", "KS", "LA", "ME", "MD", "MN", "NC", "ND", "NE", "NY", "OH", "OK",
  "PA", "SD", "TN", "TX", "VT", "WI", "WY",
];

interface Props {
  onJobCreated: (job: Job) => void;
  onBatchCreated: (batchId: string) => void;
}

export default function UploadPage({ onJobCreated, onBatchCreated }: Props) {
  const [files, setFiles] = useState<File[]>([]);
  const [stateCode, setStateCode] = useState("");
  const [dragging, setDragging] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addFiles(incoming: FileList | null) {
    if (!incoming) return;
    const pdfs = Array.from(incoming).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    setFiles((prev) => {
      const seen = new Set(prev.map((f) => `${f.name}_${f.size}`));
      return [...prev, ...pdfs.filter((f) => !seen.has(`${f.name}_${f.size}`))];
    });
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (files.length === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      if (files.length === 1) {
        onJobCreated(await uploadPdf(files[0], stateCode));
      } else {
        const batch = await uploadBatch(files, stateCode);
        onBatchCreated(batch.batch_id);
      }
      setFiles([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const totalMb = files.reduce((n, f) => n + f.size, 0) / 1024 / 1024;

  return (
    <div className="card">
      <h2>Upload bill PDFs</h2>
      <p className="muted">
        Single or multiple files. Workers convert them in parallel — add more workers to
        increase throughput. Approvals are saved locally (no production SFTP upload).
      </p>

      <div
        className={dragging ? "dropzone dragging" : "dropzone"}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        <p>Drag PDFs here, or</p>
        <input
          type="file"
          accept="application/pdf"
          multiple
          onChange={(e) => addFiles(e.target.files)}
        />
      </div>

      {files.length > 0 && (
        <div className="file-list">
          <div className="file-list-head">
            <span>
              {files.length} file(s) · {totalMb.toFixed(1)} MB
            </span>
            <button className="link-button" onClick={() => setFiles([])}>
              Clear
            </button>
          </div>
          {files.map((f, i) => (
            <div key={`${f.name}_${f.size}`} className="file-row">
              <span className="file-name">{f.name}</span>
              <span className="muted">{(f.size / 1024).toFixed(0)} KB</span>
              <button
                className="link-button"
                onClick={() => setFiles(files.filter((_, idx) => idx !== i))}
              >
                remove
              </button>
            </div>
          ))}
        </div>
      )}

      <form onSubmit={handleSubmit} className="upload-form">
        <select value={stateCode} onChange={(e) => setStateCode(e.target.value)}>
          <option value="">Auto-detect state</option>
          {STATE_CODES.map((code) => (
            <option key={code} value={code}>
              {code}
            </option>
          ))}
        </select>
        <button type="submit" disabled={files.length === 0 || submitting}>
          {submitting
            ? "Uploading…"
            : files.length > 1
            ? `Convert ${files.length} files`
            : "Convert"}
        </button>
      </form>

      {error && <p className="error">{error}</p>}
    </div>
  );
}
