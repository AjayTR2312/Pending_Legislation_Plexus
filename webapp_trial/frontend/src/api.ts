import type {
  ApprovalStatus,
  Artifact,
  BatchSummary,
  CompareData,
  DownloadsInfo,
  Job,
  LlmConfig,
  LocateResult,
  PipelineSession,
  ReviewState,
  RulesOverview,
  SessionFile,
  SubmitAllResult,
  SubmitConfig,
  TagAuditResult,
  ValidationReport,
} from "./types";

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json() as Promise<T>;
}

export async function uploadPdf(file: File, stateCode: string): Promise<Job> {
  const form = new FormData();
  form.append("file", file);
  if (stateCode) form.append("state_code", stateCode);
  const res = await fetch("/api/jobs", { method: "POST", body: form });
  return asJson<Job>(res);
}

export async function getJob(id: string): Promise<Job> {
  const res = await fetch(`/api/jobs/${id}`);
  return asJson<Job>(res);
}

export async function getJobText(id: string): Promise<string> {
  const res = await fetch(`/api/jobs/${id}/text`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.text();
}

export async function approveJob(id: string): Promise<{ approved: boolean; saved_to: string }> {
  const res = await fetch(`/api/jobs/${id}/approve`, { method: "POST" });
  return asJson(res);
}

export function jobPdfUrl(id: string): string {
  return `/api/jobs/${id}/pdf`;
}

export function jobTextUrl(id: string): string {
  return `/api/jobs/${id}/text`;
}

export async function listJobs(): Promise<Job[]> {
  return asJson<Job[]>(await fetch("/api/jobs"));
}

export async function uploadBatch(files: File[], stateCode: string): Promise<BatchSummary> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  if (stateCode) form.append("state_code", stateCode);
  const res = await fetch("/api/batches", { method: "POST", body: form });
  const data = await asJson<{ batch_id: string; jobs: Job[] }>(res);
  return { batch_id: data.batch_id, total: data.jobs.length, completed: 0, failed: 0, jobs: data.jobs };
}

export async function getBatch(batchId: string): Promise<BatchSummary> {
  return asJson<BatchSummary>(await fetch(`/api/batches/${batchId}`));
}

export async function decideJob(id: string, approve: boolean) {
  return asJson(
    await fetch(`/api/jobs/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approve }),
    })
  );
}

export async function listArtifacts(id: string): Promise<Artifact[]> {
  return asJson<Artifact[]>(await fetch(`/api/jobs/${id}/artifacts`));
}

export function artifactUrl(id: string, name: string): string {
  return `/api/jobs/${id}/artifacts/${encodeURIComponent(name)}`;
}

export async function getCachedTagAudit(
  textPath: string,
  pdfPath: string
): Promise<TagAuditResult | null> {
  const params = new URLSearchParams({ text_path: textPath, pdf_path: pdfPath });
  return asJson<TagAuditResult | null>(await fetch(`/api/tag-audit?${params}`));
}

export async function runTagAudit(
  textPath: string,
  pdfPath: string,
  stateCode: string | null
): Promise<TagAuditResult> {
  return asJson<TagAuditResult>(
    await fetch("/api/tag-audit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text_path: textPath, pdf_path: pdfPath, state_code: stateCode }),
    })
  );
}

export async function listSessions(): Promise<PipelineSession[]> {
  return asJson<PipelineSession[]>(await fetch("/api/sessions"));
}

export async function listSessionFiles(sessionId: string): Promise<SessionFile[]> {
  return asJson<SessionFile[]>(await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/files`));
}

export function sessionTextUrl(path: string): string {
  return `/api/sessions/file/text?path=${encodeURIComponent(path)}`;
}

export function sessionPdfUrl(path: string): string {
  return `/api/sessions/file/pdf?path=${encodeURIComponent(path)}`;
}

export async function fetchText(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.text();
}

export async function getValidation(
  textPath: string,
  pdfPath: string | null,
  stateCode: string | null,
  jobId?: string
): Promise<ValidationReport> {
  const params = new URLSearchParams({ text_path: textPath });
  if (pdfPath) params.set("pdf_path", pdfPath);
  if (stateCode) params.set("state_code", stateCode);
  if (jobId) params.set("job_id", jobId);
  return asJson<ValidationReport>(await fetch(`/api/validation?${params}`));
}

export function workingTextUrl(key: string): string {
  return `/api/validation/text?key=${encodeURIComponent(key)}`;
}

export async function applyFix(key: string, original: string, replacement: string) {
  return asJson(
    await fetch("/api/validation/fix", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, original, replacement }),
    })
  );
}

export async function undoFix(key: string) {
  return asJson(
    await fetch("/api/validation/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    })
  );
}

export async function resetEdits(key: string) {
  return asJson(
    await fetch("/api/validation/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    })
  );
}

export async function saveText(key: string, text: string) {
  return asJson(
    await fetch("/api/validation/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, text }),
    })
  );
}

export async function getCompare(
  textPath: string,
  pdfPath: string,
  editKey?: string
): Promise<CompareData> {
  const params = new URLSearchParams({ text_path: textPath, pdf_path: pdfPath });
  if (editKey) params.set("key", editKey);
  return asJson<CompareData>(await fetch(`/api/compare?${params}`));
}

export async function getApproval(key: string): Promise<ApprovalStatus> {
  return asJson<ApprovalStatus>(await fetch(`/api/validation/approval?key=${encodeURIComponent(key)}`));
}

export async function approveReview(
  key: string,
  jobId?: string
): Promise<ApprovalStatus & { saved_to: string }> {
  return asJson(
    await fetch("/api/validation/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, job_id: jobId ?? null }),
    })
  );
}

export async function getLlmConfig(): Promise<LlmConfig> {
  return asJson<LlmConfig>(await fetch("/api/llm/config"));
}

export async function getSubmitConfig(): Promise<SubmitConfig> {
  return asJson<SubmitConfig>(await fetch("/api/submit/config"));
}

export async function submitToKeyed(
  key: string
): Promise<ApprovalStatus & { host: string; remote_path: string; bytes: number }> {
  return asJson(
    await fetch("/api/validation/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    })
  );
}

export async function getLocations(
  key: string,
  textPath: string,
  pdfPath: string
): Promise<LocateResult> {
  const params = new URLSearchParams({ key, text_path: textPath, pdf_path: pdfPath });
  return asJson<LocateResult>(await fetch(`/api/validation/locate?${params}`));
}

export function comparePageUrl(key: string, page: number): string {
  return `/api/compare/page?key=${encodeURIComponent(key)}&page=${page}`;
}

export async function getDownloads(): Promise<DownloadsInfo> {
  return asJson<DownloadsInfo>(await fetch("/api/downloads"));
}

/** Fetch new ZIPs from the SFTP server (read-only on the server). */
export async function checkSftp(): Promise<{ downloaded: string[]; total: number }> {
  return asJson(await fetch("/api/downloads/check", { method: "POST" }));
}

export async function queueZips(names: string[]): Promise<BatchSummary & { skipped: { zip: string; reason: string }[] }> {
  const res = await fetch("/api/downloads/queue", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
  const data = await asJson<{ batch_id: string; jobs: Job[]; skipped: { zip: string; reason: string }[] }>(res);
  return { batch_id: data.batch_id, total: data.jobs.length, completed: 0, failed: 0, jobs: data.jobs, skipped: data.skipped };
}

/** Submit every approved file in the list to Keyed; anything not approved is skipped. dryRun only reports. */
export async function submitAllApproved(
  items: { keys?: string[]; textPaths?: string[] },
  dryRun: boolean
): Promise<SubmitAllResult> {
  const res = await fetch("/api/submit-all", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys: items.keys ?? [], text_paths: items.textPaths ?? [], dry_run: dryRun }),
  });
  return asJson<SubmitAllResult>(res);
}

/** Where each file stands in Approve -> Submit (so the list can show what a tick would send). */
export async function getApprovalStates(textPaths: string[]): Promise<Record<string, ReviewState>> {
  const res = await fetch("/api/approval-states", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text_paths: textPaths }),
  });
  return (await asJson<{ states: Record<string, ReviewState> }>(res)).states;
}

export async function getRulesOverview(): Promise<RulesOverview> {
  return asJson<RulesOverview>(await fetch("/api/rules"));
}
