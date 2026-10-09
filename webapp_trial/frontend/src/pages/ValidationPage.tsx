import { useEffect, useState } from "react";
import {
  applyFix,
  fetchText,
  getValidation,
  resetEdits,
  undoFix,
  workingTextUrl,
} from "../api";
import PdfViewer from "../components/PdfViewer";
import type { ReviewTarget, ValidationIssue, ValidationReport } from "../types";

interface Props {
  target: ReviewTarget;
  onBack: () => void;
}

function scoreClass(pct: number | null): string {
  if (pct === null) return "score-unknown";
  if (pct >= 99) return "score-good";
  if (pct >= 95) return "score-warn";
  return "score-bad";
}

export default function ValidationPage({ target, onBack }: Props) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [text, setText] = useState<string>("");
  const [selected, setSelected] = useState<ValidationIssue | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function load() {
    setError(null);
    try {
      const r = await getValidation(target.textPath, target.pdfPath, target.stateCode);
      setReport(r);
      setText(await fetchText(workingTextUrl(r.key)));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.textPath]);

  function selectIssue(issue: ValidationIssue) {
    setSelected(issue);
    setDraft(issue.expected || issue.output || "");
    setNotice(null);
  }

  async function handleApply() {
    if (!report || !selected) return;
    const original = selected.output || selected.expected;
    if (!original) return;
    setBusy(true);
    setError(null);
    try {
      await applyFix(report.key, original, draft);
      setNotice(`Applied fix for issue #${selected.id}`);
      setSelected(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleUndo() {
    if (!report) return;
    setBusy(true);
    try {
      await undoFix(report.key);
      setNotice("Last fix reverted");
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (!report) return;
    setBusy(true);
    try {
      await resetEdits(report.key);
      setNotice("All edits discarded");
      await load();
    } finally {
      setBusy(false);
    }
  }

  if (error && !report) return <p className="error">{error}</p>;
  if (!report) return <p className="muted">Running validation…</p>;

  const acc = report.content_accuracy_pct;
  const integrityIssues = report.integrity.issues ?? [];

  return (
    <div className="review-page">
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← Back
        </button>
        <h2>{report.name}</h2>
        {target.stateCode && <span className="pill">{target.stateCode}</span>}
        {report.edited && <span className="pill pill-edit">edited</span>}
      </div>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      <div className="score-row">
        <div className={`score-card ${scoreClass(acc)}`}>
          <span className="score-label">Content Accuracy</span>
          <span className="score-value">{acc === null ? "n/a" : `${acc.toFixed(2)}%`}</span>
        </div>
        <div className={`score-card ${integrityIssues.length ? "score-bad" : "score-good"}`}>
          <span className="score-label">Tag Integrity</span>
          <span className="score-value">
            {integrityIssues.length ? `${integrityIssues.length} issue(s)` : "pass"}
          </span>
        </div>
        <div className="score-card score-info">
          <span className="score-label">Flagged Issues</span>
          <span className="score-value">{report.issues.length}</span>
        </div>
      </div>

      {!report.has_precleaned_baseline && (
        <p className="muted">
          No pre-cleaned baseline found for this file, so content accuracy could not be computed.
          Tag integrity is still checked.
        </p>
      )}

      {integrityIssues.length > 0 && (
        <div className="card">
          <strong>Tag integrity</strong>
          <ul className="integrity-list">
            {integrityIssues.map((msg, i) => (
              <li key={i}>{msg}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="compare-grid">
        <div className="compare-pane">
          <h3>Source PDF</h3>
          {target.pdfUrl ? (
            <PdfViewer src={target.pdfUrl} />
          ) : (
            <p className="muted">No source PDF available.</p>
          )}
        </div>

        <div className="compare-pane">
          <h3>Issues &amp; corrections</h3>

          {report.issues.length === 0 ? (
            <p className="muted">No content issues flagged.</p>
          ) : (
            <div className="issues-table">
              {report.issues.map((issue) => (
                <button
                  key={issue.id}
                  className={selected?.id === issue.id ? "issue-row selected" : "issue-row"}
                  onClick={() => selectIssue(issue)}
                >
                  <span className={`issue-sev sev-${issue.severity}`}>{issue.severity}</span>
                  <span className="issue-cat">{issue.category}</span>
                  <span className="issue-text">
                    {(issue.expected || issue.output || "").slice(0, 90)}
                  </span>
                </button>
              ))}
            </div>
          )}

          {selected && (
            <div className="fix-panel">
              <strong>Issue #{selected.id}</strong>
              <p className="muted">{selected.explanation}</p>
              {selected.expected && (
                <>
                  <span className="fix-label">Expected (from PDF)</span>
                  <pre className="fix-box">{selected.expected}</pre>
                </>
              )}
              {selected.output && (
                <>
                  <span className="fix-label">Current output</span>
                  <pre className="fix-box">{selected.output}</pre>
                </>
              )}
              <span className="fix-label">Replacement</span>
              <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={3} />
              <p className="muted">
                Always verify against the source PDF before applying. Edits are written to a
                trial copy — the original pipeline file is never modified.
              </p>
              <div className="approve-row">
                <button onClick={handleApply} disabled={busy}>
                  Apply Fix
                </button>
                <button className="link-button" onClick={() => setSelected(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          <h3 className="section-gap">Converted text</h3>
          <pre className="output-text">{text}</pre>

          <div className="approve-row">
            <button onClick={handleUndo} disabled={busy || report.history.length === 0}>
              Undo last ({report.history.length})
            </button>
            <button
              className="link-button"
              onClick={handleReset}
              disabled={busy || report.history.length === 0}
            >
              Discard all edits
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
