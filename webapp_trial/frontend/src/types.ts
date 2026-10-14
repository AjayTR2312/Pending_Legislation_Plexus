export interface StepLogEntry {
  step: string;
  status: "running" | "done" | "error" | "info" | "warning";
  message: string;
  at?: string;
  /** True for lines captured from the conversion modules' own logging. */
  detail?: boolean;
}

export interface TokenUsage {
  /** Model(s) that actually answered; differs from the configured one after a fallback. */
  model?: string;
  fallback_used?: boolean;
  /** Prompt-cache tokens: written once (1.25x price), read on later calls (~5% of price). */
  cache_write_tokens?: number;
  cache_read_tokens?: number;
  /** What caching saved (negative on the call that wrote the cache). */
  cache_saving_usd?: number;
  input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number;
  api_calls?: number;
}

export interface JobMetrics {
  content_accuracy_pct: number | null;
  layout_accuracy_pct?: number | null;
  integrity_issues: number;
  flagged_issues: number;
  validation_key: string | null;
  usage?: TokenUsage;
}

export type JobStatus =
  | "queued"
  | "processing"
  | "done"
  | "error"
  | "awaiting_approval"
  | "skipped";

export interface Job {
  id: string;
  filename: string;
  state_code: string | null;
  status: JobStatus;
  current_step: string | null;
  steps_log: StepLogEntry[];
  error: string | null;
  has_output: boolean;
  approved: boolean;
  created_at: string;
  worker_id?: string | null;
  attempts?: number;
  batch_id?: string | null;
  pdf_type?: string | null;
  display_type?: string | null;
  page_count?: number | null;
  mode?: string | null;
  mode_reason?: string | null;
  requires_approval?: boolean;
  conversion_secs?: number | null;
  validation_secs?: number | null;
  output_bytes?: number | null;
  metrics?: JobMetrics | null;
}

export interface BatchSummary {
  batch_id: string;
  total: number;
  completed: number;
  failed: number;
  jobs: Job[];
}

export interface Artifact {
  name: string;
  size_bytes: number;
  path: string;
}

export interface CompareBox {
  page: number;
  x: number;
  y: number;
  w: number;
  h: number;
  /** Index into the filtered PDF word list (server side). */
  i?: number;
}

/** A validation finding pinned to a text range and/or PDF boxes. */
export interface Marker {
  id: string;
  /** content = vs source text; tags = tag structure/drift; paragraph = merge/split/one-tag;
   *  rules = global format + jurisdiction rules; layout = PDF paragraph geometry. */
  source: "content" | "layout" | "tags" | "paragraph" | "rules" | "revisions";
  /** Rule id (rules.py) and the jurisdiction it came from ("ALL" or a state code). */
  rule: string | null;
  scope: string | null;
  kind: string;
  severity: "error" | "warning" | "info";
  title: string;
  detail: string;
  /** Id of the content issue (for Apply Fix), when this marker comes from one. */
  issue_id: number | null;
  /** 0-based text range; w0/w1 null means "whole line". */
  line: number;
  w0: number | null;
  end_line: number | null;
  w1: number | null;
  /** True when the content only exists in the PDF; line/w0 is where it belongs. */
  insertion: boolean;
  page: number | null;
  boxes: Omit<CompareBox, "i">[];
  expected: string;
  output: string;
  parent: string | null;
  children: number;
}

export interface LlmConfig {
  primary: string;
  fallback: string | null;
  pricing: { input: number; output: number; cache_write: number; cache_read: number };
}

/** Save -> Approve -> Submit. Approval is tied to the exact text that was approved. */
export interface ApprovalStatus {
  approved: boolean;
  /** Approved once, then edited — must be approved again before submitting. */
  stale: boolean;
  approved_at: string | null;
  submitted: boolean;
  submitted_at: string | null;
  submitted_to: string | null;
}

export interface SubmitConfig {
  host: string;
  port: number;
  user: string;
  remote_dir: string;
  password_set: boolean;
}

export interface RuleCatalogEntry {
  id: string;
  title: string;
  requirement: string;
  scope: string;
  source: string;
  findings: number;
}

/** What ran for this jurisdiction, and what still needs a human against the PDF. */
export interface RuleCatalog {
  state: string | null;
  spec: {
    name: string;
    add: string[];
    delete: string[];
    remove: string;
    file_type: string;
    all_caps_condition?: string;
    no_tag_clauses?: string[];
    tag_scope?: string;
  } | null;
  rules: RuleCatalogEntry[];
  manual: { kind: "add" | "delete"; text: string }[];
  formatting_note: string;
}

export interface LocateResult {
  markers: Marker[];
  compare_key: string;
  catalog: RuleCatalog;
}

export interface CompareData {
  key: string;
  pages: { index: number; width_pt: number; height_pt: number; aspect: number }[];
  map: Record<string, CompareBox>;
  lines: string[];
  mapped_words: number;
  pdf_words: number;
}

export interface PipelineSession {
  session_id: string;
  date_folder: string;
  state: string;
  path: string;
  pdf_count: number;
  converted_count: number;
  created: string;
}

export interface SessionFile {
  name: string;
  stem: string;
  text_path: string;
  pdf_path: string | null;
  size_bytes: number;
}

/** A document opened for review — either a fresh upload or a pipeline file. */
export interface ReviewTarget {
  title: string;
  stateCode: string | null;
  textUrl: string;
  pdfUrl: string | null;
  /** Raw server-side paths, required by the validation endpoints. */
  textPath: string;
  pdfPath: string | null;
  jobId?: string;
}

export interface ValidationIssue {
  id: number;
  type: string;
  category: string;
  severity: string;
  expected: string;
  output: string;
  words_affected: number;
  explanation: string;
}

export interface LayoutIssue {
  kind: string;
  /** Two short lines kept apart that are probably meant to be separate lines. */
  likely_ok?: boolean;
  line: number | null;
  related_line: number | null;
  description: string;
  context: string;
  page: number | null;
}

export interface TagAuditResult {
  key: string;
  passes: number;
  warnings: number;
  errors: number;
  total: number;
  tag_accuracy_pct: number | null;
  xlsx_path: string | null;
  page_images: number;
  /** What running this LLM audit cost. Absent for audits run before cost tracking. */
  usage?: TokenUsage;
  /** Formatted spans found in the PDF, and why the checklist may have been empty. */
  spans_in_pdf?: number;
  note?: string;
  /** False when the model never looked at the pages (empty checklist), so there is no real accuracy. */
  verified?: boolean;
}

export interface ValidationReport {
  key: string;
  name: string;
  original_path: string;
  working_path: string;
  pdf_path: string | null;
  edited: boolean;
  has_precleaned_baseline: boolean;
  content_accuracy_pct: number | null;
  /** Plain text similarity from the checker, before page footers and reordered text are discounted. */
  content_accuracy_raw_pct?: number | null;
  /** The checker's own layout score, before short line-by-line blocks (addresses, signatures) are discounted. */
  layout_accuracy_raw_pct?: number | null;
  layout_accuracy_pct: number | null;
  content_summary: {
    precleaned_words: number | null;
    output_words: number | null;
    missing_words: number | null;
    extra_words: number | null;
    changed_words: number | null;
    category_summary: Record<string, number>;
  };
  state_code?: string | null;
  /** Source-vs-output differences that are only page furniture (not counted as findings). */
  hidden_artifacts?: { id: number; expected: string; output: string; reason: string }[];
  integrity: {
    severity: string | null;
    issues: string[];
    /** Informational baseline-comparison notes (not defects). */
    notes?: string[];
    counts: Record<string, number>;
  };
  rules?: { errors: number; warnings: number; info: number };
  issues: ValidationIssue[];
  layout_issues: LayoutIssue[];
  history: { original: string; replacement: string }[];
}

export interface DownloadedZip {
  name: string;
  size_kb: number;
  modified: string;
  /** State read from the file name (NC_..._keying.zip); null when it cannot be read. */
  state: string | null;
  /** Already queued for conversion earlier. */
  queued: boolean;
}

export interface DownloadsInfo {
  folder: string;
  sftp_configured: boolean;
  files: DownloadedZip[];
}

export interface SubmitAllResult {
  results: { name: string; status: "ready" | "submitted" | "skipped" | "failed"; reason: string }[];
  ready: number;
  submitted: number;
  skipped: number;
  failed: number;
}

export type ReviewStateName = "not_reviewed" | "not_approved" | "approved" | "edited" | "submitted" | "missing";

export interface ReviewState {
  state: ReviewStateName;
  approved_at?: string | null;
  submitted_at?: string | null;
}

export interface StateRules {
  code: string;
  spec: RuleCatalog["spec"];
  /** The spec's own wording, in full (what the conversion model is told). */
  instructions: { add: string[]; delete: string[] };
  /** Checks that exist only for this state. */
  rules: RuleCatalogEntry[];
  /** Instructions nothing can check from text: a person verifies them against the PDF. */
  manual: RuleCatalog["manual"];
}

export interface RulesOverview {
  global: RuleCatalogEntry[];
  states: StateRules[];
}
