import type { Job } from "../types";

function fmtSecs(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

function fmtBytes(b: number | null | undefined): string {
  if (!b) return "—";
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${(b / 1024 / 1024).toFixed(2)} MB`;
}

function accClass(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return "score-unknown";
  if (pct >= 99) return "score-good";
  if (pct >= 95) return "score-warn";
  return "score-bad";
}

export default function JobMetrics({ job }: { job: Job }) {
  const m = job.metrics;
  const acc = m?.content_accuracy_pct ?? null;
  const total = (job.conversion_secs ?? 0) + (job.validation_secs ?? 0);
  // Absent metrics must not be rendered as a green "pass" — that would claim a
  // validation result that was never produced.
  const validated = m != null;

  return (
    <div className="score-row">
      <div className="score-card score-info">
        <span className="score-label">Conversion</span>
        <span className="score-value">{fmtSecs(job.conversion_secs)}</span>
      </div>
      <div className="score-card score-info">
        <span className="score-label">Validation</span>
        <span className="score-value">{fmtSecs(job.validation_secs)}</span>
      </div>
      <div className="score-card score-info">
        <span className="score-label">Total</span>
        <span className="score-value">{fmtSecs(total || null)}</span>
      </div>
      <div className="score-card score-info">
        <span className="score-label">Output Size</span>
        <span className="score-value">{fmtBytes(job.output_bytes)}</span>
      </div>
      <div className={`score-card ${accClass(acc)}`}>
        <span className="score-label">Content Accuracy</span>
        <span className="score-value">{acc === null ? "n/a" : `${acc.toFixed(2)}%`}</span>
      </div>
      <div
        className={`score-card ${
          !validated ? "score-unknown" : m.integrity_issues ? "score-bad" : "score-good"
        }`}
      >
        <span className="score-label">Tag Integrity</span>
        <span className="score-value">
          {!validated ? "n/a" : m.integrity_issues ? `${m.integrity_issues} issue(s)` : "pass"}
        </span>
      </div>
      {m?.usage?.cost_usd !== undefined && (
        <div className="score-card score-info">
          <span className="score-label">Conversion cost</span>
          <span className="score-value">${m.usage.cost_usd.toFixed(4)}</span>
          <span className="score-sub">
            {(m.usage.input_tokens ?? 0).toLocaleString()} in ·{" "}
            {(m.usage.output_tokens ?? 0).toLocaleString()} out
          </span>
          {((m.usage.cache_read_tokens ?? 0) > 0 || (m.usage.cache_write_tokens ?? 0) > 0) && (
            <span className="score-sub" title="Prompt caching: the state prompt is written to the cache once, then read at ~5% of the input price">
              cache {(m.usage.cache_read_tokens ?? 0) > 0
                ? `hit ${(m.usage.cache_read_tokens ?? 0).toLocaleString()} tok`
                : `write ${(m.usage.cache_write_tokens ?? 0).toLocaleString()} tok`}
              {m.usage.cache_saving_usd !== undefined
                ? ` · ${m.usage.cache_saving_usd >= 0 ? "saved" : "cost"} $${Math.abs(m.usage.cache_saving_usd).toFixed(4)}`
                : ""}
            </span>
          )}
          {m.usage.model && (
            <span className={m.usage.fallback_used ? "score-sub model-fallback" : "score-sub"}
              title={m.usage.fallback_used ? "The configured model could not serve the request, so the fallback answered" : undefined}>
              {m.usage.fallback_used ? "↪ fallback: " : ""}{m.usage.model}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
