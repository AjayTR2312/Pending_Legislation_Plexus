import { useEffect, useState } from "react";
import { getApprovalStates, listSessionFiles, listSessions, sessionPdfUrl, sessionTextUrl } from "../api";
import type { PipelineSession, ReviewState, ReviewStateName, ReviewTarget, SessionFile } from "../types";
import SubmitAllButton from "./SubmitAllButton";

interface Props {
  onOpen: (target: ReviewTarget) => void;
}

const STATE_LABEL: Record<ReviewStateName, string> = {
  not_reviewed: "Not reviewed",
  not_approved: "Not approved",
  approved: "Approved",
  edited: "Edited since approval",
  submitted: "Submitted",
  missing: "File missing",
};

export default function SessionsPanel({ onOpen }: Props) {
  const [sessions, setSessions] = useState<PipelineSession[] | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [files, setFiles] = useState<Record<string, SessionFile[]>>({});
  const [states, setStates] = useState<Record<string, ReviewState>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSessions()
      .then(setSessions)
      .catch((err) => setError(String(err)));
  }, []);

  async function refreshStates(paths: string[]) {
    try {
      const fresh = await getApprovalStates(paths);
      setStates((prev) => ({ ...prev, ...fresh }));
      // a file that was submitted (or edited) is no longer a valid tick
      setPicked((prev) => new Set([...prev].filter((p) => (fresh[p] ?? { state: "approved" }).state === "approved")));
    } catch {
      /* the list still works without the status labels */
    }
  }

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
        refreshStates(list.map((f) => f.text_path));
      } catch (err) {
        setError(String(err));
      }
    } else {
      refreshStates(files[id].map((f) => f.text_path)); // it may have been approved since
    }
  }

  const canTick = (path: string) => states[path]?.state === "approved";
  const flip = (path: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

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
      <div className="sessions-top">
        <p className="muted tab-help">
          <b>Pipeline Sessions</b> are files the SFTP pipeline already converted (one folder per run, under <code>tmp/pending_processing/</code>). Open one to review it. The pipeline output is never changed; your edits go to a separate working copy. <b>Jobs</b> are PDFs you uploaded here.
        </p>
        <div className="sessions-submit">
          <SubmitAllButton
            textPaths={[...picked]}
            label={picked.size ? `Submit selected (${picked.size})` : "Submit selected"}
            disabled={picked.size === 0}
            onDone={() => refreshStates(Object.values(files).flat().map((f) => f.text_path))}
          />
          <span className="muted sessions-submit-hint">
            {picked.size ? `${picked.size} approved file(s) ticked` : "Tick approved files below, then submit them to Keyed."}
          </span>
        </div>
      </div>
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
                <>
                  <div className="session-tickall">
                    <button
                      className="link-button"
                      onClick={() => {
                        const ok = files[s.session_id].map((f) => f.text_path).filter(canTick);
                        const all = ok.length > 0 && ok.every((p) => picked.has(p));
                        setPicked((prev) => {
                          const next = new Set(prev);
                          ok.forEach((p) => (all ? next.delete(p) : next.add(p)));
                          return next;
                        });
                      }}
                    >
                      Tick / untick all approved in this session
                    </button>
                  </div>
                  {files[s.session_id].map((f) => {
                    const st = states[f.text_path]?.state;
                    return (
                      <div key={f.text_path} className="session-file">
                        <input
                          type="checkbox"
                          className="session-tick"
                          checked={picked.has(f.text_path)}
                          disabled={!canTick(f.text_path)}
                          onChange={() => flip(f.text_path)}
                          aria-label={`Select ${f.name} to submit`}
                          title={canTick(f.text_path) ? "Tick to include in Submit selected" : "Only approved files can be ticked: open it with Review and approve it first"}
                        />
                        <span className="session-file-name" title={f.name}>
                          {f.name}
                        </span>
                        {st && <span className={`review-state review-${st}`}>{STATE_LABEL[st]}</span>}
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
                    );
                  })}
                </>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
