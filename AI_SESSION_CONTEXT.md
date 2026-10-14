# AI Session Context — Pending Legislation

> **Purpose:** Context handoff so a parallel AI session can work on this repository
> without re-discovering the codebase. Written 2026-10-06.
>
> **Read this first**, then `STREAMLIT_FEATURE_REFERENCE.md` for the full feature map.

---

## 0. Standing User Instructions

These constraints were set by the repository owner and apply to all sessions:

1. **Do not change anything without explicit permission.** Investigate, report, and propose — then wait for approval before editing production code.
2. **Flag bugs rather than silently fixing them.**
3. The React prototype is a **trial only**. It must never interfere with the production Streamlit app or the SFTP pipeline.

---

## 1. What This System Does

Converts US legislative bill PDFs into plain text with markup showing added and deleted
legal language, then uploads approved output for downstream ingestion.

```text
SFTP /ftp/out (bill ZIPs)
  -> download + unzip
  -> detect state from filename
  -> PyMuPDF extraction (detects underline / strikethrough)
  -> Claude Opus 4.6 cleaning with state-specific prompt
  -> deterministic post-processing
  -> 5-layer validation
  -> human review (Streamlit)
  -> SFTP /ftp/keyed  -> NetScan consumes
```

### The output contract (cannot be changed)

```text
[A>new or added language<A]
[D>old or deleted language<D]
```

Defined by the NetScan rulebook. The system is a **transcription and formatting-preservation
system, not a summarisation system**. Every printed word, capitalisation, punctuation and
document order must be preserved.

### Upstream / downstream ownership

| Zone | Owned by | Notes |
|---|---|---|
| Bill sourcing, ZIP naming, PDF quality | Not this team | State detection depends on the filename convention |
| Download → convert → validate → review → upload | **This team** | Entire middle of the pipeline |
| NetScan ingestion | Not this team | **No feedback channel** — once uploaded, no signal returns |

Because there is no downstream feedback loop, the human approval gate before upload is the
last checkpoint. This is why the validation stack is so heavy.

---

## 2. Repository Map

| Path | Role | Size |
|---|---|---|
| `app/streamlit_app.py` | Main UI, all processing orchestration | ~8.9k lines |
| `app/pymupdf_extractor.py` | PDF geometry → tagged text. The hard core | ~9k lines |
| `Opus4.6/Claude_Opus_4.6_Test.py` | Conversion engine (visual + hybrid modes) | ~4.8k lines |
| `app/validation_review_inline.py` | HITL review: Apply Fix / Ignore / Undo | large |
| `app/pipeline_runner.py` | **Production** SFTP automation runner | — |
| `pipeline_runner.py` (root) | Drifted duplicate — NOT used in production | — |
| `data/Prompts/*.txt` | 20 state-specific cleaning prompts | — |
| `Opus4.6/netscan_state_template.json` | 45+ state rule configs | — |
| `tests/` | 37 passing tests (AR + DE fixtures only) | — |
| `webapp_trial/` | React + FastAPI prototype (isolated) | — |

### Which runner is production

The `Dockerfile` copies only `app/`, so **`app/pipeline_runner.py` is production**.
The root-level `pipeline_runner.py` has diverged and is not deployed. Confirm before editing either.

### Models

| Use | Model |
|---|---|
| Conversion, state detection | `claude-opus-4-6` |
| `gpt_compare.py`, `pymupdf_compare.py`, `llm_diff_verifier.py` audits | `claude-opus-4-7` |

Auth is **not** via `ANTHROPIC_API_KEY`. A token is fetched at runtime from the
Thomson Reuters AI Platform gateway (`TOKEN_URL` in `Claude_Opus_4.6_Test.py`).

---

## 3. Verified Facts (do not re-investigate)

These were confirmed by reading code or running commands in this session.

| Fact | Evidence |
|---|---|
| 37 tests pass | `pytest` run locally |
| CI exists | `.github/workflows/test.yml`, pytest on Python 3.11 |
| `.streamlit/config.toml` and `.dockerignore` exist | Docker build inputs are present |
| No `.env` or `.env.example` in repo | Searched including ignored files |
| Test fixtures cover **AR and DE only** | `tests/fixtures/` |
| Batch limit is 20 files | `MAX_BATCH` in `streamlit_app.py` ~line 5637 |
| Batch total size limit 50 MB | `BATCH_MAX_TOTAL_SIZE_MB`, default 50 |
| Page limits: 15 standard, 50 vision | `MAX_PAGES`, `MAX_PAGES_VISION` |
| Batch processing is **serial, single daemon thread** | `BatchWorker._run()` ~line 7094 |
| `check_content_accuracy()` returns `accuracy_pct` | `content_accuracy_checker.py` line 820 |
| SSL verification disabled on Claude auth | `verify=False` in `Claude_Opus_4.6_Test.py` lines 314/323/326, `gpt_compare.py` line 77 |
| Live deployment path is `pending_legislationv2` | Provided by repo owner |

---

## 4. Open Issues — Found, Reported, NOT Fixed

Nothing below has been changed. All require owner approval before fixing.

### Priority 1 — blocking Florida go-live

**I1. Accuracy always logs 0% in the production pipeline**
`app/pipeline_runner.py` line ~305:
```python
c_acc = content_report.get("accuracy", 0)   # wrong key
```
`check_content_accuracy()` returns `accuracy_pct`. The `.get()` fallback silently yields `0`,
so every automated run logs `Content accuracy: 0.0%`. The saved JSON is correct; only the
log line and the derived icon are wrong. Ops are effectively blind to conversion quality.

**I2. Wrong input passed to the accuracy check**
Same call passes `raw_text` (raw PyMuPDF output, still containing line numbers, headers,
footers) where the function expects **pre-cleaned** text. This inflates missing/extra word
counts with artifacts that were legitimately stripped. Fixing I1 without I2 produces a
number that is still wrong.

**I3. Production runner skips integrity steps that the UI path runs**
`app/pipeline_runner.py:convert_pdf()` never calls:
- `strip_protected_tags()`
- `rejoin_mid_sentence_line_breaks()`
- `enforce_tag_integrity_gate()` — auto-repairs malformed `[A>` / `[D>` delimiters
- `build_tag_integrity_report()` — produces the documented `*_integrity_validation.json`

Compare with `process_with_hybrid_mode()` in `streamlit_app.py` (~lines 3869–3998), which
does call all of them. Bills processed by the automated pipeline can therefore reach review
with malformed tags that the UI path would have repaired, and the documented integrity
artifact is never produced.

**I4. SFTP host may be UAT while deploying to prod**
`SFTP_HOST = "10.17.38.130"` is hardcoded in five places, all with
`"SFTP ... Script - LCPR UAT LandingStrips"` docstrings:
`ftp_download.py`, `ftp_upload.py`, `app/ftp_download.py`, `app/ftp_upload.py`, and inline in
`_submit_to_keyed_sftp()` in `streamlit_app.py` (~line 138). The image deploys to a `-prod`
ECR repository. There is **no environment override** for host or remote path.
**Needs a human answer — cannot be resolved from code.**

**I5. No Florida regression tests**
`tests/fixtures/` has Arkansas and Delaware only. FL-specific logic exists
(`_is_fl` terminal-punctuation guard in `pymupdf_extractor.py` ~line 7423, column-aligned
table rules in `FL_Clean_Prompt.txt`) with zero snapshot coverage.

### Priority 2 — security

**I6. Live credential committed to the repository**
`config/pdfservices-api-credentials.json` contains a real Adobe PDF Services
`client_id` and `client_secret`. The Dockerfile does `COPY config/ /app/config/`, baking it
into the production image. **Recommend rotating the credential**, then moving it to a secret
store. Rotation must be done by the owner in Adobe's console.

**I7. SSL verification disabled on the Claude auth path**
```python
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
resp = requests.post(TOKEN_URL, headers=None, json=payload, verify=False)
http_client = httpx.Client(verify=False)
```
The connection carrying the auth token does not verify the server identity. Likely a
workaround for a TLS-inspection proxy — but it should be a signed-off decision using the
internal CA certificate, not an inherited default.

### Priority 3 — correctness and robustness

**I8. FL has no protected clauses**
`app/protected_clauses.py` defines `PROTECTED_CLAUSES` for KS, DE, IN, CA only. Florida
boilerplate has no guard against being incorrectly tagged. Combined with I3, FL has the least
protection of any state currently in the template.

**I9. Batch limit is checked last, after all files are read**
In `streamlit_app.py` (~lines 5553–5638) the validation loop runs `uf.read()` and
`pypdfium2.PdfDocument(...)` for **every** uploaded file, then checks `MAX_BATCH` at the end.
A 1000-file upload is fully read and parsed before being rejected. Moving the count check
before the loop is a small, low-risk fix.

**I10. Deployment documentation is stale**
Live URL is `https://aideployment-prod.plexus.gcs.int.thomsonreuters.com/208321/pending_legislationv2/`
but `DEPLOYMENT_COMMANDS_2026-06-11.md` records `.../pending_legislation/` and
`Current latest version: 3.1.1`. Unknown: which image tag v2 is, whether v1 is still live
(risk of two deployments consuming the same `/ftp/out` queue), and the rollback target.

**I11. `submit_to_keyed()` skips `ensure_remote_dir()`**
Both `pipeline_runner.py` copies upload directly, unlike `ftp_upload.upload_files()` which
creates the remote directory first. Minor, but inconsistent.

**I12. `enableXsrfProtection=false`** in the Dockerfile CMD and `.streamlit/config.toml`.
Disables Streamlit CSRF protection app-wide. Probably a proxy workaround; worth a conscious
sign-off.

---

## 5. Scalability Analysis (completed)

**Question asked:** can the system handle a 1000-file upload?
**Answer: no.**

| Gate | Limit | Order checked |
|---|---|---|
| Per-file upload size | 50 MB | 1st (Streamlit) |
| Pages per file | 15 / 50 vision | 2nd |
| Total batch size | 50 MB | 3rd |
| File count | **20** | **last** |

Beyond the limits, processing is **serial in a single daemon thread** (`BatchWorker`).
Estimated throughput at roughly 1–2 minutes per file end-to-end:

| Files | Serial time |
|---|---|
| 20 | 20–40 min |
| 100 | 1.7–3.3 hrs |
| 1000 | **17–33 hrs** |

The daemon thread has no persistence — a pod restart loses the entire batch.
The SFTP `pipeline_runner.py` path has no file-count or size limits and resumes via
`.processed_files.json`, but is still serial.

**Conclusion:** the bottleneck is the serial architecture, not the limits. Swapping Streamlit
for React does **not** fix it; the React trial currently has the same flaw in a different form
(in-memory `JOBS` dict, thread per job). The real fix is a durable job queue plus N concurrent
workers bounded by the Claude API rate limit.

---

## 6. React + FastAPI Trial

Location: `webapp_trial/` — **completely isolated**, touches no production code.

```text
webapp_trial/
├── backend/
│   ├── main.py                  FastAPI routes
│   ├── processing.py            wraps existing Opus4.6 / app modules unchanged
│   └── requirements-trial.txt   fastapi, uvicorn, python-multipart
└── frontend/                    Vite + React + TypeScript
    └── src/pages/{UploadPage,ReviewPage}.tsx
```

### Running it

```powershell
# Backend
.\.venv\Scripts\Activate.ps1
cd webapp_trial\backend
..\..\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8800

# Frontend (separate terminal; needs Set-ExecutionPolicy for npm)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
cd webapp_trial\frontend
npm install
npm run dev
```

UI at `http://localhost:5173`, API at `http://localhost:8800` (docs at `/docs`).

### Safety properties

- Imports existing modules read-only via `importlib`, same pattern as `pipeline_runner.py`
- **Approve writes only to `webapp_trial/backend/storage/approved/`** — never uploads to
  `/ftp/keyed`. This is deliberate; do not "fix" it without discussion.
- Uploading a PDF **does** make a real Claude API call and costs tokens.

### Known trial limitations

- In-memory `JOBS` dict — jobs lost on restart, cannot run multiple uvicorn workers
- Re-executes the ~4.8k-line Opus module on **every job** via `importlib` (no caching)
- Covers roughly **15%** of Streamlit's feature surface

### Bug already fixed in the trial

Uploads were being saved as `input.pdf`, discarding the original filename. State detection
reads the NetScan numeric ID from the **filename** (3rd underscore segment), so auto-detect
always returned `None` and bills were converted with the generic prompt instead of the
state-specific one. Fixed by preserving the original name plus a `safe_filename()` guard
against path traversal. Verified: `2026_2026_9_...` → `FL`, `2025_2026_15_...` → `IA`.

### Trial roadmap (agreed order, not started)

1. SQLite job store + separate worker process — proves the scalability premise
2. Pipeline Sessions sidebar — small, reuses existing `list_pending_sessions()`
3. Validation Review screen — the large piece; accuracy scores, issues table, Apply Fix / Undo

---

## 7. Working in Parallel — Guidance

### Safe to touch without coordination

- `webapp_trial/**` — isolated prototype
- `tests/**` — adding tests is additive and low risk
- Documentation `.md` files

### Coordinate before touching

- `app/streamlit_app.py` — huge, central, easy to conflict on
- `app/pymupdf_extractor.py` — state-specific logic is fragile; changes need fixtures
- `app/pipeline_runner.py` — production automation path
- `data/Prompts/*.txt` and `netscan_state_template.json` — changing a prompt changes output
  for every bill of that state

### Never change without explicit approval

- The `[A>` / `[D>` tag format — fixed by the NetScan rulebook
- The L5 demote-only safety policy in `llm_diff_verifier.py`. An LLM may only demote a flagged
  diff to "expected" when **all** hold: verdict `artifact`, confidence `high`, category in
  {header, footer, decorative_rule, page_number}, word count ≤ 15. It may **never** promote.
  This asymmetry is intentional.
- SFTP hosts and remote paths

### Before any extractor or prompt change

Run the test suite — it is the only regression safety net:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expect **37 passing**. Snapshot tests cover Arkansas and Delaware only, so changes affecting
other states will not be caught automatically.

---

## 8. Recommended Next Actions

Agreed priority order from this session:

1. **Fix I1 + I2** — accuracy logging and the wrong input. Small, contained.
2. **Add a Florida fixture and snapshot test** (I5) — harness already exists; cheapest
   high-value item before go-live.
3. **Port the missing integrity steps into `app/pipeline_runner.py`** (I3).
4. **Confirm the SFTP host** (I4) — needs a human answer.
5. **Rotate the Adobe credential** (I6) — owner action in Adobe's console.
6. Optionally: move the `MAX_BATCH` check before the read loop (I9).

React trial work (Section 6 roadmap) sits **behind** all of the above, because the Florida
go-live affects live legal data on a dated deadline.

---

## 9. Open Questions Needing Human Answers

From `PENDING_LEGISLATION_HANDOFF_GUIDE.md` section 13, still unanswered:

1. Which pipeline runner actually runs in production?
2. What schedules or triggers it?
3. Which Docker image tag and Git commit are deployed as v2?
4. Is the v1 deployment path still live?
5. Who owns the SFTP account, and how is `FTP_PASSWORD` rotated?
6. What is the normal daily document volume?
7. What is the rollback procedure?
8. Is `10.17.38.130` production or UAT?

---

## 10. Related Documents

| Document | Purpose |
|---|---|
| `STREAMLIT_FEATURE_REFERENCE.md` | Complete feature inventory + architecture diagrams |
| `PENDING_LEGISLATION_HANDOFF_GUIDE.md` | Operational handoff, security notes, open questions |
| `PIPELINE_FLOW.md` | End-to-end SFTP pipeline description |
| `DEPLOYMENT_COMMANDS_2026-06-11.md` | Docker build / ECR push steps (URL now stale — see I10) |
| `README.md` | Project overview |
