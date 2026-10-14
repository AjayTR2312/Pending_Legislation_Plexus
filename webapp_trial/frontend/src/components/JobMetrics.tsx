import type { Job, TagAuditResult } from "../types";
import InfoTip from "./InfoTip";

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

const COST_INFO =
  "Conversion is the cost of turning the PDF into text. The validation checks (paragraphs, tags, state rules, " +
  "content comparison) are free. The tag audit is the only validation that uses the model: it costs tokens, and its " +
  "cost is added to this total only if you run it.";

/** Time, size and LLM cost of the run. Scores are in the tiles below, so nothing appears twice. */
export default function JobMetrics({ job, audit }: { job: Job; audit?: TagAuditResult | null }) {
  const total = (job.conversion_secs ?? 0) + (job.validation_secs ?? 0);
  const u = job.metrics?.usage;
  const conv = u?.cost_usd;
  const val = audit?.usage?.cost_usd;
  const sum = conv !== undefined ? conv + (val ?? 0) : undefined;

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
      {u && sum !== undefined && (
        <div className="score-card score-info">
          <span className="score-label">LLM cost<InfoTip text={COST_INFO} /></span>
          <span className="score-value">${sum.toFixed(4)}</span>
          <span className="score-sub">
            Conversion ${conv!.toFixed(4)} · {(u.input_tokens ?? 0).toLocaleString()} in / {(u.output_tokens ?? 0).toLocaleString()} out
          </span>
          {audit && (
            <span className="score-sub">
              Validation (tag audit) {val !== undefined ? `$${val.toFixed(4)}` : "cost not recorded for this earlier run"}
              {audit.usage?.api_calls === 0 ? " · no model call" : ""}
            </span>
          )}
          {((u.cache_read_tokens ?? 0) > 0 || (u.cache_write_tokens ?? 0) > 0) && (
            <span className="score-sub" title="Prompt caching: the state prompt is written to the cache once, then read at ~5% of the input price">
              cache {(u.cache_read_tokens ?? 0) > 0
                ? `hit ${(u.cache_read_tokens ?? 0).toLocaleString()} tok`
                : `write ${(u.cache_write_tokens ?? 0).toLocaleString()} tok`}
              {u.cache_saving_usd !== undefined
                ? ` · ${u.cache_saving_usd >= 0 ? "saved" : "cost"} $${Math.abs(u.cache_saving_usd).toFixed(4)}`
                : ""}
            </span>
          )}
          {u.model && (
            <span className={u.fallback_used ? "score-sub model-fallback" : "score-sub"}
              title={u.fallback_used ? "The configured model could not serve the request, so the fallback answered" : undefined}>
              {u.fallback_used ? "↪ fallback: " : ""}{u.model}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
