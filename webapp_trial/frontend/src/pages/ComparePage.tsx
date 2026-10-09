import { useEffect, useState } from "react";
import { fetchText } from "../api";
import PdfViewer from "../components/PdfViewer";
import type { ReviewTarget } from "../types";

interface Props {
  target: ReviewTarget;
  onBack: () => void;
  footer?: React.ReactNode;
}

/** Side-by-side source PDF and converted text. Shared by uploads and pipeline files. */
export default function ComparePage({ target, onBack, footer }: Props) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setText(null);
    setError(null);
    fetchText(target.textUrl)
      .then(setText)
      .catch((err) => setError(String(err)));
  }, [target.textUrl]);

  return (
    <div className="review-page">
      <div className="review-header">
        <button className="link-button" onClick={onBack}>
          ← Back
        </button>
        <h2>{target.title}</h2>
        {target.stateCode && <span className="pill">{target.stateCode}</span>}
      </div>

      {error && <p className="error">{error}</p>}

      <div className="compare-grid">
        <div className="compare-pane">
          <h3>Source PDF</h3>
          {target.pdfUrl ? (
            <PdfViewer src={target.pdfUrl} />
          ) : (
            <p className="muted">No source PDF found alongside this output.</p>
          )}
        </div>
        <div className="compare-pane">
          <h3>Converted text</h3>
          <pre className="output-text">{text ?? "Loading…"}</pre>
          {footer}
        </div>
      </div>
    </div>
  );
}
