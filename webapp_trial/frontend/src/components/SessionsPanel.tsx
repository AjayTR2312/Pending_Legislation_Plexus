import { useEffect, useState } from "react";
import { listSessionFiles, listSessions, sessionPdfUrl, sessionTextUrl } from "../api";
import type { PipelineSession, ReviewTarget, SessionFile } from "../types";

interface Props {
  onOpen: (target: ReviewTarget) => void;
}

export default function SessionsPanel({ onOpen }: Props) {
  const [sessions, setSessions] = useState<PipelineSession[] | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [files, setFiles] = useState<Record<string, SessionFile[]>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSessions()
      .then(setSessions)
      .catch((err) => setError(String(err)));
  }, []);

  async function toggle(session: PipelineSession) {
    const id = session.session_id;
    if (expanded === id) {
      setExpanded(null);
      return;
    }
    setExpanded(id);
    if (!files[id]) {
      try {
        const list = await listSessionFiles(id);
        setFiles((prev) => ({ ...prev, [id]: list }));
      } catch (err) {
        setError(String(err));
      }
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (sessions === null) return <p className="muted">Loading sessions…</p>;
  if (sessions.length === 0) {
    return (
      <p className="muted">
        No pipeline sessions found. These appear once the SFTP pipeline has converted bills
        into <code>tmp/pending_processing/</code>.
      </p>
    );
  }

  return (
    <div className="sessions-list">
      <p className="muted tab-help">
        <b>Pipeline Sessions</b> are files the SFTP pipeline already converted (one folder per run, under <code>tmp/pending_processing/</code>). Open one to review it. The pipeline output is never changed; your edits go to a separate working copy. <b>Jobs</b> are PDFs you uploaded here.
      </p>
      {sessions.map((s) => (
        <div key={s.session_id} className="session-item">
          <button className="session-header" onClick={() => toggle(s)}>
            <span className="session-caret">{expanded === s.session_id ? "▾" : "▸"}</span>
            {s.date_folder && <span className="session-date">[{s.date_folder}]</span>}
            <span className="pill">{s.state}</span>
            <span className="session-counts">
              {s.pdf_count} PDF · {s.converted_count} converted
            </span>
          </button>

          {expanded === s.session_id && (
            <div className="session-files">
              {(files[s.session_id] ?? []).length === 0 ? (
                <p className="muted">No converted files in this session.</p>
              ) : (
                files[s.session_id].map((f) => (
                  <div key={f.text_path} className="session-file">
                    <span className="session-file-name" title={f.name}>
                      {f.name}
                    </span>
                    <button
                      onClick={() =>
                        onOpen({
                          title: f.name,
                          stateCode: s.state,
                          textUrl: sessionTextUrl(f.text_path),
                          pdfUrl: f.pdf_path ? sessionPdfUrl(f.pdf_path) : null,
                          textPath: f.text_path,
                          pdfPath: f.pdf_path,
                        })
                      }
                    >
                      Review
                    </button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
