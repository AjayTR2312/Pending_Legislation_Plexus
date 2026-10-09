import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyFix,
  fetchText,
  getCachedTagAudit,
  getValidation,
  resetEdits,
  runTagAudit,
  saveText,
  undoFix,
  workingTextUrl,
} from "../api";
import PdfViewer from "../components/PdfViewer";
import CompareViewer from "../components/CompareViewer";
import ScoreCard from "../components/ScoreCard";
import type { ReviewTarget, TagAuditResult, ValidationIssue, ValidationReport } from "../types";

interface Props {
  target: ReviewTarget;
  onBack: () => void;
}

export default function ValidationPage({ target, onBack }: Props) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [text, setText] = useState<string>("");
  const [selected, setSelected] = useState<ValidationIssue | null>(null);
  const [draft, setDraft] = useState("");
  const [severityFilter, setSeverityFilter] = useState("all");
  const [audit, setAudit] = useState<TagAuditResult | null>(null);
  const [auditRunning, setAuditRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState("");
  const [view, setView] = useState<"issues" | "compare">("issues");
  const editorRef = useRef<HTMLTextAreaElement | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const r = await getValidation(target.textPath, target.pdfPath, target.stateCode);
      setReport(r);
      setText(await fetchText(workingTextUrl(r.key)));
      if (target.pdfPath) {
        setAudit(await getCachedTagAudit(target.textPath, target.pdfPath));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [target.textPath, target.pdfPath, target.stateCode]);

  useEffect(() => {
    load();
  }, [load]);

  const visibleIssues = useMemo(() => {
    if (!report) return [];
    return severityFilter === "all"
      ? report.issues
      : report.issues.filter((i) => i.severity === severityFilter);
  }, [report, severityFilter]);

  const selectIssue = useCallback((issue: ValidationIssue | null) => {
    setSelected(issue);
    setDraft(issue ? issue.expected || issue.output || "" : "");
    setNotice(null);
  }, []);

  const step = useCallback(
    (delta: number) => {
      if (visibleIssues.length === 0) return;
      const idx = selected ? visibleIssues.findIndex((i) => i.id === selected.id) : -1;
      const next = Math.min(Math.max(idx + delta, 0), visibleIssues.length - 1);
      selectIssue(visibleIssues[next]);
    },
    [visibleIssues, selected, selectIssue]
  );

  const handleApply = useCallback(async () => {
    if (!report || !selected) return;
    const original = selected.output || selected.expected;
    if (!original) return;
    setBusy(true);
    setError(null);
    try {
      await applyFix(report.key, original, draft);
      setNotice(`Applied fix for issue #${selected.id}`);
      selectIssue(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [report, selected, draft, load, selectIssue]);

  const handleUndo = useCallback(async () => {
    if (!report) return;
    setBusy(true);
    try {
      await undoFix(report.key);
      setNotice("Last fix reverted");
      await load();
    } finally {
      setBusy(false);
    }
  }, [report, load]);

  // Keyboard review loop: j/k to move, a to apply, u to undo, Esc to deselect.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT") return;
      if (e.key === "j") step(1);
      else if (e.key === "k") step(-1);
      else if (e.key === "a" && selected) handleApply();
      else if (e.key === "u") handleUndo();
      else if (e.key === "Escape") selectIssue(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, selected, handleApply, handleUndo, selectIssue]);

  async function handleAudit() {
    if (!target.pdfPath) return;
    setAuditRunning(true);
    setError(null);
    try {
      setAudit(await runTagAudit(target.textPath, target.pdfPath, target.stateCode));
      setNotice("Tag audit complete");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAuditRunning(false);
    }
  }

  async function handleReset() {
    if (!report) return;
    setBusy(true);
    try {
      await resetEdits(report.key);
      setNotice("All edits discarded");
      setEditing(false);
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveText() {
    if (!report) return;
    setBusy(true);
    setError(null);
    try {
      await saveText(report.key, editDraft);
      setNotice("Inline edits saved");
      setEditing(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (error && !report) return <p className="error">{error}</p>;
  if (!report) return <p className="muted">Running validation…</p>;

  const integrityIssues = report.integrity.issues ?? [];
  const severities = Array.from(new Set(report.issues.map((i) => i.severity)));

  return (
    <div className="review-page">
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← Back
        </button>
        <h2>{report.name}</h2>
        {target.stateCode && <span className="pill">{target.stateCode}</span>}
        {report.edited && <span className="pill pill-edit">edited</span>}
        <span className="muted kbd-hint">j/k navigate · a apply · u undo</span>
      </div>

      {error && <p className="error">{error}</p>}
      {notice && <p className="notice">{notice}</p>}

      <div className="score-row">
        <ScoreCard label="Content Accuracy" pct={report.content_accuracy_pct} />
        <ScoreCard label="Layout Accuracy" pct={report.layout_accuracy_pct} />
        <ScoreCard label="Tag Accuracy" pct={audit?.tag_accuracy_pct ?? null} />
        <div className={`score-card ${integrityIssues.length ? "score-bad" : "score-good"}`}>
          <span className="score-label">Tag Integrity</span>
          <span className="score-value">
            {integrityIssues.length ? `${integrityIssues.length} issue(s)` : "pass"}
          </span>
        </div>
        <div className="score-card score-info">
          <span className="score-label">Flagged</span>
          <span className="score-value">
            {report.issues.length + report.layout_issues.length}
          </span>
        </div>
      </div>

      {!audit && target.pdfPath && (
        <div className="card">
          <strong>LLM tag audit</strong>
          <p className="muted">
            Sends highlighted page images to Claude to verify every detected
            tag. This costs tokens, so it runs only when you ask for it.
          </p>
          <button onClick={handleAudit} disabled={auditRunning}>
            {auditRunning ? "Auditing… (this can take minutes)" : "Run tag audit"}
          </button>
        </div>
      )}

      {audit && (
        <div className="card">
          <strong>Tag audit</strong>
          <p className="muted">
            {audit.passes} pass · {audit.warnings} warning · {audit.errors} error ·{" "}
            {audit.total} checked across {audit.page_images} page image(s)
          </p>
        </div>
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

      <nav className="tab-bar">
        <button
          className={view === "issues" ? "tab active" : "tab"}
          onClick={() => setView("issues")}
        >
          Issues &amp; corrections
        </button>
        <button
          className={view === "compare" ? "tab active" : "tab"}
          onClick={() => setView("compare")}
          disabled={!target.pdfPath}
        >
          Word-level compare
        </button>
      </nav>

      {view === "compare" && target.pdfPath && (
        <CompareViewer
          textPath={target.textPath}
          pdfPath={target.pdfPath}
          editKey={report.key}
        />
      )}

      {report.layout_issues.length > 0 && (
        <div className="card">
          <strong>Layout issues ({report.layout_issues.length})</strong>
          <div className="issues-table">
            {report.layout_issues.slice(0, 20).map((li, i) => (
              <div key={i} className="issue-row static">
                <span className="issue-sev sev-warning">{li.kind}</span>
                <span className="issue-cat">{li.line != null ? `line ${li.line}` : "—"}</span>
                <span className="issue-text">{li.description || li.context}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {view === "issues" && (
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
          <h3>Content issues &amp; corrections</h3>

          {severities.length > 1 && (
            <div className="filter-row">
              <button
                className={severityFilter === "all" ? "chip active" : "chip"}
                onClick={() => setSeverityFilter("all")}
              >
                All {report.issues.length}
              </button>
              {severities.map((s) => (
                <button
                  key={s}
                  className={severityFilter === s ? "chip active" : "chip"}
                  onClick={() => setSeverityFilter(s)}
                >
                  {s} {report.issues.filter((i) => i.severity === s).length}
                </button>
              ))}
            </div>
          )}

          {visibleIssues.length === 0 ? (
            <p className="muted">No content issues flagged.</p>
          ) : (
            <div className="issues-table">
              {visibleIssues.map((issue) => (
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
              <textarea
                ref={editorRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={3}
              />
              <p className="muted">
                Verify against the source PDF before applying. Edits go to a trial copy —
                the original file is never modified.
              </p>
              <div className="approve-row">
                <button onClick={handleApply} disabled={busy}>
                  Apply Fix
                </button>
                <button className="link-button" onClick={() => selectIssue(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          <h3 className="section-gap">
            Converted text
            {!editing && (
              <button
                className="link-button inline-edit-btn"
                onClick={() => {
                  setEditDraft(text);
                  setEditing(true);
                }}
              >
                Edit inline
              </button>
            )}
          </h3>

          {editing ? (
            <>
              <textarea
                className="inline-editor"
                value={editDraft}
                onChange={(e) => setEditDraft(e.target.value)}
              />
              <div className="approve-row">
                <button onClick={handleSaveText} disabled={busy}>
                  Save changes
                </button>
                <button className="link-button" onClick={() => setEditing(false)}>
                  Cancel
                </button>
                <span className="muted">Saved to the trial copy only.</span>
              </div>
            </>
          ) : (
            <pre className="output-text">{text}</pre>
          )}

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
      )}
    </div>
  );
}
